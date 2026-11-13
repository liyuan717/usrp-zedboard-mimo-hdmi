#!/usr/bin/env python3
"""Controlled production-W33 correlator BRAM mapping and paired synthesis.
No precision, schedule, logical history, waveform or FEC change; not a fullG0 result.
"""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from redundancy_diagnostic_audit import cost
from workspace_storage_guard import check,GIB
from correlation_bram_rewrite import transform
from fec_inverse_cost_probe import routed_measurements
NAME='differential_corr_cost_probe.sv'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def validate_replacement(name,baseline_sha256,current,proof_path,rrc_proof=None,window_proof=None,coarse_proof=None):
    actual=digest(current)
    if actual==baseline_sha256:return None
    if name=='rrc_cost_probe.sv':
        from rrc_implementation_probe import validate_rrc_replacement
        return validate_rrc_replacement(name,baseline_sha256,current,rrc_proof)
    if name=='link_fec_ingress_cost_probe.sv':
        from fec_window_implementation_probe import validate_window_replacement
        return validate_window_replacement(name,baseline_sha256,current,window_proof)
    if name=='differential_score_cost_probe.sv':
        from close_g0_coarse_gate_candidate import validate_coarse_replacement
        return validate_coarse_replacement(name,baseline_sha256,current,coarse_proof)
    assert name==NAME and proof_path is not None,'unproven source replacement: '+name
    p=json.loads(proof_path.read_text())
    assert p['baseline_sha256']==baseline_sha256 and p['optimized_sha256']==actual
    assert p['mapping_kind']=='two_TDP_BRAM_images' and p['arithmetic_scheduler_coefficients_unchanged'] and p['numeric_and_service_probe_passed']
    assert p['paired_island_net_synthesis_saving']['LUT']>0
    assert p['inferred_history_primitive_contract']['passed']
    n=p['independent_numeric_probe'];assert n['input_word_bits']==33 and n['output_component_bits']==49
    assert n['correlations_compared']>=3000 and n['single_bit_output_corruption_rejected']
    assert n['simulation_measured']['saturated_max_ii']<=9 and n['reset_during_warmup_sample_gap_epoch_wrap_and_backpressure']
    return {'name':name,'baseline_sha256':baseline_sha256,'compiled_sha256':actual,'proof':str(proof_path),'same_precision_schedule_logical_history':True,'physical_history_mapping':'two_TDP_BRAM_images'}

def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    before=check('before-paired-production-correlation-synthesis');(out/'disk-before.json').write_text(json.dumps(before,indent=2));assert before['disk']['available_bytes']>=5*GIB
    try:
        old=a.baseline.resolve();new=a.optimized.resolve();numeric=json.loads((a.numeric_probe.resolve()/'receipt.json').read_text())
        assert numeric['input_word_bits']==33 and numeric['correlations_compared']>=3000 and numeric['single_bit_output_corruption_rejected']
        assert (a.numeric_probe/NAME).read_bytes()==new.read_bytes()
        s0=old.read_text();s1=new.read_text();seq='\n always @(posedge clk) begin\n  if(rst)'
        assert s0[s0.index(seq):]==s1[s1.index(seq):]
        assert transform(s0)==s1,'source differs outside the exact history mapping replacement'
        for v,p in [('baseline',old),('optimized',new)]:shutil.copyfile(p,out/(v+'.sv'))
        t='set_param general.maxThreads 2\n'
        for v in ('baseline','optimized'):
            t+='create_project -in_memory -part xc7z020clg484-1\n'
            t+=f'read_verilog -sv {{{out/(v+".sv")}}}\nsynth_design -top differential_corr_cost_probe -part xc7z020clg484-1 -mode out_of_context -flatten_hierarchy rebuilt -generic W=33\n'
            t+='create_clock -period 20 [get_ports clk]\nset_input_delay 2 -clock [get_clocks clk] [get_ports -filter {DIRECTION == IN && NAME != clk}]\nset_output_delay 2 -clock [get_clocks clk] [all_outputs]\nopt_design\n'
            t+=f'report_utilization -file {{{out/(v+".rpt")}}}\nclose_project\n'
        (out/'paired.tcl').write_text(t)
        with (out/'synthesis.log').open('w') as f:subprocess.run([str(a.vivado_bin.resolve()/'vivado'),'-mode','batch','-source',str(out/'paired.tcl'),'-nolog','-nojournal'],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
        script=Path(__file__).resolve().parents[1]/'pl/g0/check_correlation_bram_mapping.tcl'
        with (out/'primitive-contract.log').open('w') as f:
            subprocess.run([str(a.vivado_bin.resolve()/'vivado'),'-mode','batch','-source',str(script),'-nolog','-nojournal','-tclargs',str(a.numeric_probe.resolve())],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
        primitive_log=(out/'primitive-contract.log').read_text()
        primitive_marker=next(x for x in primitive_log.splitlines() if x.startswith('CORRELATION_HISTORY_PRIMITIVES_PASS'))
        primitive={'passed':True,'marker':primitive_marker,'log_sha256':hashlib.sha256(primitive_log.encode()).hexdigest()}
        c0=cost(out/'baseline.rpt');c1=cost(out/'optimized.rpt')
        result={'scope':__doc__,'baseline_sha256':digest(old),'optimized_sha256':digest(new),'mapping_kind':'two_TDP_BRAM_images','arithmetic_scheduler_coefficients_unchanged':True,'numeric_and_service_probe_passed':True,'independent_numeric_probe':numeric,'optimized_island_routed':routed_measurements(a.numeric_probe),'inferred_history_primitive_contract':primitive,'baseline_synthesis':c0,'optimized_synthesis':c1,'paired_island_net_synthesis_saving':{k:c0[k]-c1[k] for k in c0},'whole_board_saving_not_assumed':True,'full_G0_passed':False}
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='independent_numeric_probe'}),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(check('after-paired-production-correlation-synthesis'),indent=2)+'\n')
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('baseline','optimized','numeric-probe','output','vivado-bin'):p.add_argument('--'+n,type=Path,required=True)
    main(p.parse_args())
