#!/usr/bin/env python3
"""Run canonical candidate implementation with persistent status and before/after disk checks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
from prepare_g0_source_platform import review
from workspace_storage_guard import check, GIB
from workspace_vivado_memory import memory, monitor, select_threads


def main(a):
    v=Path(__file__).resolve().parents[1];platform=a.platform.resolve()
    review(platform)
    assert not (platform/'route.log').exists(),'Inspect existing attempt; never replace a running or finished route'
    assert not (platform/'project/g0_source_review_routed.dcp').exists()
    out=platform/'route-run-r1';out.mkdir(exist_ok=False)
    before=check('before-canonical-SOURCE-route')
    (out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
    (platform/'disk-before-route.json').write_text(json.dumps(before,indent=2)+'\n')
    assert before['disk']['available_bytes']>=5*GIB
    script=v/'pl/g0/implement_g0_source.tcl'
    result=dict(scope=__doc__,started_unix=time.time(),
                host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                canonical_TCL_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
                source_inputs_sha256=hashlib.sha256((platform/'source-inputs.json').read_bytes()).hexdigest(),
                completed=False,route_passed=False,bitstream_emitted=False,
                hardware_or_SD_changed=False,full_production_IO_or_G0_passed=False)
    receipt=out/'receipt.json'
    reference_args=[]
    try:
        if a.incremental_reference:
            checkpoint=a.incremental_reference.resolve(strict=True)
            with checkpoint.open('rb') as src:
                digest=hashlib.file_digest(src,'sha256').hexdigest()
            if digest!=a.incremental_reference_sha256:
                raise ValueError('Incremental reference checkpoint differs from the expected SHA-256')
            result.update(incremental_reference=str(checkpoint),incremental_reference_sha256=digest)
            reference_args=[str(checkpoint)]
        reference=a.memory_reference or v/'build/g0-source-platform-r11-DC-bypass/route.log'
        reference_log=reference.read_text()
        if reference.suffix=='.jsonl':
            peak=max(json.loads(line)['peak_build_rss_plus_swap_mib']
                     for line in reference_log.splitlines())
            baseline=peak
        else:
            peak=max(float(p) for p in re.findall(r'Memory \(MB\): peak = ([0-9.]+)',reference_log))
            # Allow for the parent and child Vivado processes overlapping.
            baseline=int(peak)+1024
        result.update(memory_reference=str(reference),memory_reference_peak_mib=peak,attempts=[])
        reference_threads=a.reference_threads
        limit=min(8,len(os.sched_getaffinity(0)))
        while True:
            attempt=out/('attempt-'+str(len(result['attempts'])+1));attempt.mkdir()
            disk_before=check('before-canonical-SOURCE-route-attempt')
            (attempt/'disk-before.json').write_text(json.dumps(disk_before,indent=2)+'\n')
            assert disk_before['disk']['available_bytes']>=5*GIB
            initial=memory()
            threads,estimate=select_threads(initial['ram_mib'],baseline,reference_threads,limit)
            stats=dict(threads=threads,initial_memory=initial,estimated_peak_mib=estimate)
            if not result['attempts']:
                result['estimated_build_peak_mib']=estimate
            result['attempts'].append(stats)
            try:
                with (platform/'route.log').open('w') as log:
                    proc=subprocess.Popen([str(a.vivado_bin.resolve()/'vivado'),'-mode','batch','-source',
                                           str(script),'-nolog','-nojournal','-tclargs',str(platform),
                                           str(threads),str(int(len(result['attempts'])>1)),*reference_args],
                                          cwd=platform,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    result['launcher_PID']=proc.pid
                    receipt.write_text(json.dumps(result,indent=2)+'\n')
                    reason,measured=monitor(proc.pid,lambda:proc.poll() is None,attempt/'memory.jsonl')
                    try:result['returncode']=proc.wait(timeout=15 if reason else None)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid,signal.SIGKILL)
                        result['returncode']=proc.wait()
                stats.update(stop_reason=reason,observed_peak_group_rss_plus_swap_mib=measured,
                             returncode=result['returncode'])
                shutil.copyfile(platform/'route.log',attempt/'route.log')
            finally:
                receipt.write_text(json.dumps(result,indent=2)+'\n')
                (attempt/'disk-after.json').write_text(json.dumps(check('after-canonical-SOURCE-route-attempt'),indent=2)+'\n')
            if not reason or not result['returncode'] or threads==1:break
            child_log=platform/'project/receiver_v2.runs/impl_1/runme.log'
            if child_log.exists():shutil.copyfile(child_log,attempt/'impl-runme.log')
            # Allow the kernel to reclaim the first process tree before
            # evaluating the highest lower degree of parallelism.
            time.sleep(10)
            baseline=max(baseline,measured)
            reference_threads=threads
            limit=threads-1
        result['completed']=True
        result['route_passed']=result['returncode']==0 and 'G0_SOURCE_ROUTE_COMPLETE_REVIEW_FULL_REPORTS_BEFORE_BITSTREAM' in (platform/'route.log').read_text()
        assert result['route_passed'],'Inspect terminal implementation reports; no bitstream generated'
    finally:
        result['checkpoint_unix']=time.time()
        receipt.write_text(json.dumps(result,indent=2)+'\n')
        (out/'disk-after.json').write_text(json.dumps(check('after-canonical-SOURCE-route'),indent=2)+'\n')
    print(json.dumps(result),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--platform',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--memory-reference',type=Path,help='last completed same-stage route log or group memory JSONL')
    p.add_argument('--reference-threads',type=int,default=2)
    p.add_argument('--incremental-reference',type=Path,help='static routed DCP for project-mode TimingClosure reuse')
    p.add_argument('--incremental-reference-sha256',help='verified SHA-256 of the static routed DCP')
    a=p.parse_args()
    if bool(a.incremental_reference)!=bool(a.incremental_reference_sha256):
        p.error('--incremental-reference and --incremental-reference-sha256 are required together')
    main(a)
