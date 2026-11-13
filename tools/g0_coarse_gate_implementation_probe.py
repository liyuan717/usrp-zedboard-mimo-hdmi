#!/usr/bin/env python3
"""Measure the explicit coarse gate candidate locally; never infer whole-board cost."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from redundancy_diagnostic_audit import cost
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check, GIB


def gate10(original):
    old = "wire [101:0] five_n=({2'b0,chosen_n}<<2)+{2'b0,chosen_n};"
    new = "wire [102:0] five_n=({3'b0,chosen_n}<<3)+({3'b0,chosen_n}<<1);"
    assert original.count(old) == 1
    assert original.count("five_n>{{10{1'b0}},denominator}") == 1
    # Four signed49 correlation components imply chosen_n<=2^98; ten_n
    # therefore fits103bits. All ratio operands remain exact unsigned values.
    return original.replace(old, new).replace("five_n>{{10{1'b0}},denominator}",
                                               "five_n>{{11{1'b0}},denominator}")


def main(a):
    out = a.output.resolve()
    out.mkdir(exist_ok=False)
    before = check('before-coarse-gate-paired-implementation')
    (out/'disk-before.json').write_text(json.dumps(before, indent=2)+'\n')
    assert before['disk']['available_bytes'] >= 5*GIB
    try:
        assert gate10(a.baseline.read_text()) == a.candidate.read_text()
        tcl = 'set_param general.maxThreads 2\n'
        for label, source in [('baseline', a.baseline), ('candidate', a.candidate)]:
            dest = out/(label+'.sv')
            shutil.copyfile(source, dest)
            tcl += 'create_project -in_memory -part xc7z020clg484-1\n'
            tcl += f'read_verilog -sv {{{dest}}}\n'
            tcl += '''synth_design -top differential_score_cost_probe -part xc7z020clg484-1 -mode out_of_context -flatten_hierarchy rebuilt
create_clock -name processing -period 20 [get_ports clk]
set_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2 -clock processing [all_outputs]
opt_design
'''
            tcl += f'report_utilization -file {{{out/(label+"-synth.rpt")}}}\n'
            if label == 'candidate':
                tcl += '''place_design
route_design
report_utilization -file utilization.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force coarse_gate.dcp
'''
            tcl += 'close_project\n'
        (out/'paired.tcl').write_text(tcl)
        with (out/'ooc.log').open('w') as log:
            subprocess.run([str(a.vivado_bin.resolve()/'vivado'), '-mode', 'batch', '-source',
                            str(out/'paired.tcl'), '-nolog', '-nojournal'], cwd=out,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        old, new = cost(out/'baseline-synth.rpt'), cost(out/'candidate-synth.rpt')
        routed = routed_measurements(out)
        result = dict(scope=__doc__, baseline_sha256=hashlib.sha256(a.baseline.read_bytes()).hexdigest(),
                      candidate_sha256=hashlib.sha256(a.candidate.read_bytes()).hexdigest(),
                      exact_candidate_gate_only_transform=True, baseline_synthesis=old,
                      candidate_synthesis=new, same_flow_local_delta={k:new[k]-old[k] for k in old},
                      candidate_local_route=routed, current_production_RTL_changed=False,
                      whole_board_cost_or_timing_or_G0_passed=False)
        (out/'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
        assert routed['WNS_ns'] >= 0 and routed['WHS_ns'] >= 0 and routed['severe_DRC'] == 0
        print(json.dumps(result), flush=True)
    finally:
        (out/'disk-after.json').write_text(json.dumps(check('after-coarse-gate-paired-implementation'), indent=2)+'\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'candidate', 'output', 'vivado-bin'):
        p.add_argument('--'+name, type=Path, required=True)
    main(p.parse_args())
