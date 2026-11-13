#!/usr/bin/env python3
"""One fixed8 candidate retires the unused ADI IIR/offset while keeping its three-cycle bypass."""
import argparse,hashlib,json,os,shutil,signal,subprocess,time
from pathlib import Path
from prepare_g0_source_platform import main as prepare_base,review
from adi_dc_bypass_candidate import bypass
from workspace_storage_guard import check,GIB
from workspace_vivado_memory import group_memory,memory,monitor,select_threads


def monitored_synthesis(a,out,tcl):
    reference=a.synth_memory_reference.resolve()
    baseline=max(json.loads(line)['peak_build_rss_plus_swap_mib']
                 for line in reference.read_text().splitlines())
    if not baseline or not 1<=a.synth_reference_threads<=8 or not 1<=a.synth_thread_increment_mib<=16384:
        raise ValueError('same-stage Vivado synthesis memory and thread count required')
    receipt=out/'synthesis-memory.json'
    result=dict(reference=str(reference),reference_threads=a.synth_reference_threads,
                reference_peak_mib=baseline,attempts=[])
    limit=min(8,len(os.sched_getaffinity(0)))
    script=tcl
    prior_threads=a.synth_reference_threads
    increment=a.synth_thread_increment_mib
    margin=a.synth_memory_margin_percent
    if a.resume_synthesis:
        saved=json.loads(receipt.read_text())
        assert all(saved[k]==result[k] for k in ('reference','reference_threads','reference_peak_mib'))
        result=saved
        last=result['attempts'][-1]
        assert last['stop_reason'] and last['returncode']!=0 and last['threads']>1
        assert group_memory(last['launcher_PID'])[0]==0,'previous Vivado group still present'
        assert last['threads']>prior_threads,'no measured lower-thread reference'
        steps=last['threads']-prior_threads
        increment=max(increment,(last['peak_group_mib']-baseline+steps-1)//steps)
        limit=last['threads']-1
        margin=max(10,margin)
        saved_script=out/f'synth-attempt-{len(result["attempts"])}'/'resume.tcl'
        if saved_script.is_file():script=saved_script
        else:assert not (out/'project').exists(),'incomplete project must be preserved before restarting'
    while True:
        initial=memory()
        threads,estimate=select_threads(initial['ram_mib'],baseline,prior_threads,limit,increment,
                                        margin_percent=margin)
        attempt=out/('synth-attempt-'+str(len(result['attempts'])+1));attempt.mkdir()
        before=check('before-monitored-platform-synthesis')
        (attempt/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
        assert before['disk']['available_bytes']>=5*GIB
        stats=dict(threads=threads,initial_memory=initial,estimated_peak_mib=estimate,
                   thread_increment_mib=increment,memory_margin_percent=margin,
                   script_sha256=hashlib.sha256(script.read_bytes()).hexdigest())
        result['attempts'].append(stats)
        print(f'Vivado synthesis {threads} threads, {initial["ram_mib"]} MiB available, {estimate} MiB estimated peak',flush=True)
        try:
            with (out/'build.log').open('w') as log:
                proc=subprocess.Popen([str(a.vivado_bin.resolve()/'vivado'),'-mode','batch',
                                       '-source',str(script),'-nolog','-nojournal','-tclargs',str(threads)],
                                      cwd=out,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                stats['launcher_PID']=proc.pid
                receipt.write_text(json.dumps(result,indent=2)+'\n')
                reason,peak=monitor(proc.pid,lambda:proc.poll() is None,attempt/'memory.jsonl')
                try:rc=proc.wait(timeout=15 if reason else None)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGKILL)
                    rc=proc.wait()
            stats.update(stop_reason=reason,peak_group_mib=peak,returncode=rc)
            log_path=out/'build.log'
            shutil.copyfile(log_path,attempt/'build.log')
            if rc==0:return
            if not reason or threads==1:raise RuntimeError('Vivado synthesis failed; inspect saved attempt')
            # Keep generated IP when block design validation completed. A
            # pressure stop earlier in preparation needs a clean project.
            project=out/'project'
            if (project/'receiver_v2.xpr').exists() and 'G0_SOURCE_PLATFORM_VALIDATED' in log_path.read_text():
                runlog=project/'receiver_v2.runs/synth_1/runme.log'
                if runlog.exists():shutil.copyfile(runlog,attempt/'synth-runme.log')
                script=attempt/'resume.tcl'
                tail=tcl.read_text().split('launch_runs synth_1',1)[1]
                script.write_text('set_param general.maxThreads [lindex $argv 0]\n'
                                  'set rxv2_out [file normalize project]\n'
                                  'open_project [file join $rxv2_out receiver_v2.xpr]\n'
                                  'reset_run synth_1\nlaunch_runs synth_1'+tail)
            else:
                if project.exists():project.rename(attempt/'incomplete-project')
                script=tcl
            if threads>prior_threads:
                # Keep the measured lower-thread baseline. A stop can show
                # a steeper thread cost than the initial estimate.
                steps=threads-prior_threads
                increment=max(increment,(max(estimate,peak)-baseline+steps-1)//steps)
            else:
                baseline=max(estimate,peak);prior_threads=threads
            limit=threads-1
            margin=max(10,margin)
            time.sleep(10)
        finally:
            receipt.write_text(json.dumps(result,indent=2)+'\n')
            (attempt/'disk-after.json').write_text(json.dumps(check('after-monitored-platform-synthesis'),indent=2)+'\n')

def main(a):
    v=Path(__file__).resolve().parents[1];out=a.output.resolve()
    proof=json.loads(a.dc_proof.read_text())
    assert proof['completed'] and proof['zero_offset_filter_off_all_cycles_data_valid_equal_original']
    assert all(r['expected_result_observed'] for r in proof['actual_compiled_fault_controls'])
    before=check('before-static-ADI-DC-bypass-platform');assert before['disk']['available_bytes']>=5*GIB
    if a.resume_synthesis:
        tcl=out/'build.tcl'
        inputs=json.loads((out/'source-inputs.json').read_text())
        result=json.loads((out/'DC-bypass-receipt.json').read_text())
        assert a.synth_memory_reference and not result['synthesis_passed']
        assert hashlib.sha256(tcl.read_bytes()).hexdigest()==inputs['final_build_TCL_sha256']
        assert str(a.dc_proof.resolve())==inputs['ADI_static_DC_bypass']['proof']
        monitored_synthesis(a,out,tcl)
        review(out)
        result.update(completed=True,synthesis_passed=True,resumed_after_memory_pressure=True)
        (out/'DC-bypass-receipt.json').write_text(json.dumps(result,indent=2)+'\n')
        (out/'disk-after-ADI-DC-bypass.json').write_text(json.dumps(check('after-resumed-ADI-DC-bypass-platform'),indent=2)+'\n')
        print('ADI_STATIC_DC_PLATFORM_SYNTHESIS_COMPLETE_REVIEW_NET_COST',flush=True)
        return
    result={'completed':False,'synthesis_passed':False,'route_or_G0_passed':False,'physical_FEC_banks':8,
            'RF_started':False,'SD_written':False,'IQ_correction_and_PL_startup_DC_retained':True,
            'retired_capability':'ADI programmable DC IIR and nonzero ADC digital offset; current four channels filter-off/offset0',
            'reference_CE_mapping':'default, failedCE8/CE16 not carried into this candidate',
            'actual_net_resource_delta_measured':False}
    try:
        base=argparse.Namespace(**{k:val for k,val in vars(a).items() if k!='dc_proof'})
        base.synthesize=False;base.review_only=False;base.control_set_threshold=None
        prepare_base(base)
        (out/'disk-before-ADI-DC-bypass.json').write_text(json.dumps(before,indent=2)+'\n')
        f=out/'sources/vendor/library/xilinx/common/ad_dcfilter.v'
        old=f.read_text();assert hashlib.sha256(old.encode()).hexdigest()==proof['original_sha256']
        new=bypass(old);assert hashlib.sha256(new.encode()).hexdigest()==proof['candidate_sha256']
        f.write_text(new)
        t=out/'build.tcl';text=t.read_text()
        anchor='CONFIG.DELAY_REFCLK_FREQUENCY 200 CONFIG.DAC_DATAPATH_DISABLE 1'
        assert text.count(anchor)==1
        text=text.replace(anchor,'CONFIG.DELAY_REFCLK_FREQUENCY 200 CONFIG.ADC_DCFILTER_DISABLE 1 CONFIG.DAC_DATAPATH_DISABLE 1')
        if a.synth_memory_reference:
            assert text.count('set_param general.maxThreads 2')==text.count('launch_runs synth_1 -jobs 2')==1
            text=text.replace('set_param general.maxThreads 2','set_param general.maxThreads [lindex $argv 0]')
            text=text.replace('launch_runs synth_1 -jobs 2','launch_runs synth_1 -jobs 1')
        t.write_text(text)
        info=json.loads((out/'source-inputs.json').read_text())
        info['ADI_static_DC_bypass']={'proof':str(a.dc_proof.resolve()),'original_vendor_sha256':proof['original_sha256'],
          'compiled_vendor_sha256':proof['candidate_sha256'],'source':str(f.relative_to(out)),
          'ADC_DCFILTER_DISABLE':1,'retained_latency_clocks':3,'zero_offset_filter_off_only':True,
          'IQ_correction_and_PL_startup_DC_retained':True,'whole_board_net_savings_not_assumed':True}
        info['base_inputs_inventory_scope']+=' Also apply the separately proven V3 static ADI DC bypass; vendor file and ADC_DCFILTER_DISABLE tracked explicitly.'
        info['final_build_TCL_sha256']=hashlib.sha256(t.read_bytes()).hexdigest()
        (out/'source-inputs.json').write_text(json.dumps(info,indent=2)+'\n')
        result['source_inputs_sha256']=hashlib.sha256((out/'source-inputs.json').read_bytes()).hexdigest()
        (out/'DC-bypass-receipt.json').write_text(json.dumps(result,indent=2)+'\n')
        if a.synth_memory_reference:
            monitored_synthesis(a,out,t)
        else:
            with (out/'build.log').open('w') as log:
                subprocess.run([str(a.vivado_bin.resolve()/'vivado'),'-mode','batch','-source',str(t),'-nolog','-nojournal'],cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
        review(out)
        assert hashlib.sha256(f.read_bytes()).hexdigest()==proof['candidate_sha256']
        result.update(completed=True,synthesis_passed=True)
    finally:
        if out.exists():
            (out/'DC-bypass-receipt.json').write_text(json.dumps(result,indent=2)+'\n')
            (out/'disk-after-ADI-DC-bypass.json').write_text(json.dumps(check('after-static-ADI-DC-bypass-platform'),indent=2)+'\n')
    print('ADI_STATIC_DC_PLATFORM_SYNTHESIS_COMPLETE_REVIEW_NET_COST',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['archive','output','reference','vivado-bin','coarse-proof','window-proof','rrc-proof','correlation-proof','dc-proof']:
        p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--forward-phy-proof',type=Path)
    p.add_argument('--dma-batch-proof',type=Path)
    p.add_argument('--fec-deadline-proof',type=Path)
    p.add_argument('--synth-memory-reference',type=Path,help='last comparable integrated synthesis memory JSONL')
    p.add_argument('--synth-reference-threads',type=int,default=1)
    p.add_argument('--synth-thread-increment-mib',type=int,default=384,
                   metavar='MIB',help='estimated incremental memory per synthesis worker')
    p.add_argument('--synth-memory-margin-percent',type=int,choices=range(5,11),default=10,
                   help='extra allowance above the measured peak for an initial comparable build')
    p.add_argument('--resume-synthesis',action='store_true',help='resume a recorded memory stop with lower concurrency')
    main(p.parse_args())
