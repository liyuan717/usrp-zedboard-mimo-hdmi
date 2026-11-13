#!/usr/bin/env python3
"""Review finite G0 candidate routing and unchanged inherited IO/CDC gaps; not production signoff."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from production_redundancy_inventory import read_rows


def counts(text, name):
    values = set(map(int, re.findall(r'checking '+name+r' \((\d+)\)', text)))
    assert len(values) == 1, name
    return values.pop()


def critical_roots(text):
    rows = []
    for line in text.splitlines():
        if re.match(r'^\s*\d+\s+CDC-\d+\s+Critical\s+', line):
            cells = line.split()
            rows.append([cells[1], cells[-2], cells[-1]])
    return sorted(rows)


def scoped_cdc_equivalence(platform,baseline,cdc,old_cdc):
    """CDC rows select representative starts; compare the complete actual cone."""
    helper=Path('sources/pl/receiver_v2/integration/capture_reset_guard_contract.tcl')
    assert (platform/helper).read_bytes()==(baseline/helper).read_bytes()
    meta=json.loads((platform/'fault-cones.meta.json').read_text())
    expected={'candidate_DCP_sha256':platform/'project/g0_source_review_routed.dcp',
              'baseline_DCP_sha256':baseline/'project/g0_source_review_routed.dcp',
              'proof_TSV_sha256':platform/'fault-cones.tsv',
              'query_log_sha256':platform/'prebit-review.log',
              'helper_sha256':platform/helper}
    for key,path in expected.items():assert meta[key]==hashlib.sha256(path.read_bytes()).hexdigest(),key
    rows={}
    for line in (platform/'fault-cones.tsv').read_text().splitlines():
        design,kind,name,*ends=line.split('\t')
        assert design in ('candidate','baseline') and kind in ('fanin','fanout')
        key=(design,kind,name);assert key not in rows
        rows[key]=sorted(ends)
    root='receiver_v2_i/rf_capture_guard/inst'
    sinks=sorted(root+'/'+x+'_release_sync_reg['+str(i)+']/CLR' for x in ('din','dout') for i in (0,1))
    starts=sorted(['receiver_v2_i/reset_rf/U0/PR_OUT_DFF[0].FDRE_PER/C',
        'receiver_v2_i/reset_rf_lclk/U0/PR_OUT_DFF[0].FDRE_PER/C',
        'receiver_v2_i/rf_sampling_clock/inst/mmcm_adv_inst/LOCKED',
        'receiver_v2_i/delay_clock/inst/mmcm_adv_inst/LOCKED',
        'receiver_v2_i/rf_lclk_monitor/inst/ready_reg/C'])
    inputs=['din_raw_reset','dout_raw_reset','pll_locked','source_ready']
    assert len(rows)==16
    for design in ('candidate','baseline'):
        for sink in sinks:assert rows[(design,'fanin',sink)]==starts
        for name in inputs:assert rows[(design,'fanout',root+'/'+name)]==sinks
    for records in (cdc,old_cdc):
        assert len(records)==2 and {r[2] for r in records}=={s for s in sinks if '_reg[0]/' in s}
        assert all(kind=='CDC-10' and start in starts for kind,start,end in records)
    for marker in ('R11_SCOPED_CONE_TYPED_PASS candidate','R11_SCOPED_CONE_TYPED_PASS baseline'):
        assert re.search('^'+re.escape(marker)+'$',(platform/'prebit-review.log').read_text(),re.M)
    return {'representative_rows_equal':cdc==old_cdc,'complete_five_startpoints':starts,
            'four_CLR_endpoints':sinks,'four_local_fault_inputs':inputs,
            'complete_fanin_and_exclusive_fanout_equal':True,
            'typed_FDCE_ASYNC_REG_clock_pair_and_normal_timing_revalidated':True,
            'new_timing_exceptions_or_waivers':False,
            'proof_TSV_sha256':hashlib.sha256((platform/'fault-cones.tsv').read_bytes()).hexdigest(),
            'helper_sha256':hashlib.sha256((platform/helper).read_bytes()).hexdigest(),
            'candidate_DCP_sha256':hashlib.sha256((platform/'project/g0_source_review_routed.dcp').read_bytes()).hexdigest(),
            'baseline_DCP_sha256':hashlib.sha256((baseline/'project/g0_source_review_routed.dcp').read_bytes()).hexdigest()}


def review(platform, baseline, implementation_log=None):
    p, b = platform/'project', baseline/'project'
    log_path = implementation_log or platform/'route.log'
    log_bytes = log_path.read_bytes()
    log = log_bytes.decode()
    assert re.search(r'^G0_SOURCE_ROUTE_COMPLETE_REVIEW_FULL_REPORTS_BEFORE_BITSTREAM$', log, re.M)
    if implementation_log is not None:
        checkpoint = (p/'g0_source_review_routed.dcp').resolve()
        assert f"The checkpoint '{checkpoint}' has been generated." in log, 'Implementation log belongs to a different checkpoint'
    markers = ['CAPTURE_RESET_GUARD_CONTRACT_POST_SYNTH_PASS',
               'CAPTURE_RESET_GUARD_CONTRACT_POST_ROUTE_PASS',
               'RELEASE_ASSERTION_TYPED_CONTRACT_PASS phase=post_synth',
               'RELEASE_ASSERTION_TYPED_CONTRACT_PASS phase=post_route',
               'HDMI_EPOCH_CDC_EXACT_PASS label=ready',
               'HDMI_EPOCH_CDC_EXACT_PASS label=ack',
               'G0_COUNTER_RESET_CONTRACT_PASS local_state=65 async_release_stages=2']
    for marker in markers:
        assert re.search('^'+re.escape(marker), log, re.M), marker
    route = (p/'implementation_route_status.rpt').read_text()
    def nets(label):
        match = re.search(re.escape(label)+r'\.+\s*:\s*(\d+)\s*:', route)
        assert match, label
        return int(match[1])
    total, routed, errors = nets('# of routable nets'), nets('# of fully routed nets'), nets('# of nets with routing errors')
    assert total == routed and total > 90000 and errors == 0
    timing = (p/'implementation_timing.rpt').read_text()
    match = re.search(r'WNS\(ns\).*?\n[^\n]+\n([^\n]+)', timing)
    assert match
    values = list(map(float, match[1].split()))
    assert len(values) == 12
    for slack, violation, failing in ((0, 1, 2), (4, 5, 6), (8, 9, 10)):
        assert values[slack] >= 0 and values[violation] == values[failing] == 0
    io = (p/'prebit_io_inventory.rpt').read_text()
    old_io = (b/'prebit_io_inventory.rpt').read_text()
    for name in ('no_clock', 'unconstrained_internal_endpoints'):
        assert counts(timing, name) == counts(io, name) == 0
    for name in ('no_input_delay', 'no_output_delay'):
        assert counts(io, name) == counts(old_io, name)
    port_re = r'^[A-Za-z_][A-Za-z_0-9]*(?:\[\d+\])?$'
    ports, old_ports = set(re.findall(port_re, io, re.M)), set(re.findall(port_re, old_io, re.M))
    assert len(ports) == 24 and ports == old_ports, sorted(ports-old_ports)
    cdc = critical_roots((p/'implementation_cdc.rpt').read_text())
    old_cdc = critical_roots((b/'implementation_cdc.rpt').read_text())
    cone=None
    if cdc!=old_cdc:cone=scoped_cdc_equivalence(platform,baseline,cdc,old_cdc)
    else:assert len(cdc)==2
    bus = (p/'implementation_bus_skew.rpt').read_text()
    old_bus = (b/'implementation_bus_skew.rpt').read_text()
    requirements = re.findall(r'^Requirement:\s*([\d.]+)ns', bus, re.M)
    assert requirements == re.findall(r'^Requirement:\s*([\d.]+)ns', old_bus, re.M)
    skew = re.findall(r'^Slack \((MET|VIOLATED)\)\s*:\s*(-?[\d.]+)ns', bus, re.M)
    assert len(skew) == len(requirements) == 2 and all(state == 'MET' and float(slack) >= 0 for state, slack in skew)
    drc = (p/'implementation_drc.rpt').read_text()
    assert not re.search(r'\|\s*(Critical Warning|Error)\s*\|', drc)
    resource = read_rows(p/'implementation_utilization.rpt')[0]
    files = ['implementation_timing.rpt', 'implementation_drc.rpt', 'implementation_cdc.rpt',
             'implementation_bus_skew.rpt', 'implementation_route_status.rpt', 'prebit_io_inventory.rpt']
    return {'scope': __doc__, 'route_complete_no_errors': True, 'routable_nets': total,
            'implementation_log': {'path': str(log_path.resolve()),
                                   'sha256': hashlib.sha256(log_bytes).hexdigest()},
            'WNS_ns': values[0], 'WHS_ns': values[4], 'WPWS_ns': values[8],
            'unconstrained_internal_endpoints': 0, 'no_clock': 0,
            'inherited_missing_input_delay_count': counts(io, 'no_input_delay'),
            'inherited_missing_output_delay_count': counts(io, 'no_output_delay'),
            'inherited_untimed_ports_exactly_match_baseline': sorted(ports),
            'new_unconstrained_external_ports': [], 'inherited_CDC_critical_rows': cdc,
            'baseline_CDC_critical_rows':old_cdc,'inherited_CDC_representative_rows_equal':cdc==old_cdc,
            'inherited_CDC_scoped_cone_equivalence':cone,
            'baseline': str(baseline), 'typed_RF_HDMI_counter65_contracts_passed': True,
            'bus_skew_requirements_ns': list(map(float, requirements)),
            'bus_skew_slacks_ns': [float(s) for _, s in skew],
            'routed_resources': {k: resource[k] for k in ('LUT', 'FF', 'BRAM36_equivalent', 'DSP')},
            'full_production_IO_signoff': False,
            'finite_RX_only_G0_candidate_review_passed': True,
            'justification': 'Only finite G0 measurements: inherited24peripheral gaps and two CDC10 reset assertion endpoints. Representative start may differ only after both actual DCPs prove exact same5 roots/four CLR cones/exclusive fault fanout and typed synchronizers. No new exceptions/waivers; full production IO/CDC closure still required.',
            'source_report_sha256': {name: hashlib.sha256((p/name).read_bytes()).hexdigest() for name in files}}


def main(a):
    platform, baseline = a.platform.resolve(), a.baseline.resolve()
    if not a.inventory_existing:
        dcps={'candidate_DCP_sha256':platform/'project/g0_source_review_routed.dcp',
              'baseline_DCP_sha256':baseline/'project/g0_source_review_routed.dcp'}
        before={k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in dcps.items()}
        script = platform/'prebit-review.tcl'
        text=f'set_param general.maxThreads {a.threads}\nset out [open fault-cones.tsv w]\n'
        for label,path in [('candidate',platform),('baseline',baseline)]:
            text+='open_checkpoint {'+str(path/'project/g0_source_review_routed.dcp')+'}\n'
            text+='source {'+str(path/'sources/pl/receiver_v2/integration/capture_reset_guard_contract.tcl')+'}\n'
            text+='receiver_v2_capture_contract::validate post_route\n'
            text+='puts "R11_SCOPED_CONE_TYPED_PASS '+label+'"\n'
            text+='set root receiver_v2_i/rf_capture_guard/inst\n'
            text+='foreach domain {din dout} { foreach i {0 1} {\n'
            text+=' set pin [get_pins -quiet -hier -filter [format {NAME == "%s/%s_release_sync_reg[%d]/CLR"} $root $domain $i]]\n'
            text+=' if {[llength $pin]!=1} {error "missing CLR"}\n'
            text+=' puts $out [join [concat [list '+label+' fanin [get_property NAME $pin]] [lsort [all_fanin -flat -startpoints_only -to $pin]]] "\\t"]\n}}\n'
            text+='foreach name {din_raw_reset dout_raw_reset pll_locked source_ready} {\n'
            text+=' set pin [get_pins -quiet -hier -filter [format {NAME == "%s/%s"} $root $name]]\n'
            text+=' if {[llength $pin]!=1} {error "missing fault input"}\n'
            text+=' puts $out [join [concat [list '+label+' fanout [get_property NAME $pin]] [lsort [all_fanout -flat -endpoints_only -from $pin]]] "\\t"]\n}\n'
            if label=='candidate':text+='check_timing -override_defaults {no_clock unconstrained_internal_endpoints no_input_delay no_output_delay} -verbose -file project/prebit_io_inventory.rpt\n'
            text+='close_design\n'
        text+='close $out\n';script.write_text(text)
        with (platform/'prebit-review.log').open('w') as log:
            subprocess.run([str(a.vivado_bin.resolve()/'vivado'), '-mode', 'batch', '-source', str(script),
                            '-nolog', '-nojournal'], cwd=platform, stdout=log, stderr=subprocess.STDOUT, check=True)
        assert before=={k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in dcps.items()}
        meta={**before,'proof_TSV_sha256':hashlib.sha256((platform/'fault-cones.tsv').read_bytes()).hexdigest(),
              'query_log_sha256':hashlib.sha256((platform/'prebit-review.log').read_bytes()).hexdigest(),
              'helper_sha256':hashlib.sha256((platform/'sources/pl/receiver_v2/integration/capture_reset_guard_contract.tcl').read_bytes()).hexdigest(),
              'no_new_timing_exceptions_or_waivers':True}
        (platform/'fault-cones.meta.json').write_text(json.dumps(meta,indent=2)+'\n')
    result = review(platform, baseline, a.implementation_log)
    (a.output or platform/'route-review.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('platform', 'baseline', 'vivado-bin'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--inventory-existing', action='store_true')
    p.add_argument('--implementation-log', type=Path, help='successful log for a separately stored implementation attempt')
    p.add_argument('--output', type=Path)
    p.add_argument('--threads',type=int,choices=range(1,9),default=2)
    main(p.parse_args())
