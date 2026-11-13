#!/usr/bin/env python3
"""Controlled exact41-tap/II8 RRC history mapping synthesis, not fullG0."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
from rrc_bram_rewrite import transform
from redundancy_diagnostic_audit import cost
from workspace_storage_guard import check,GIB
NAME='rrc_cost_probe.sv'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def validate_rrc_replacement(name,baseline,current,proof_path):
    assert name==NAME and proof_path is not None,'unproven RRC source replacement'
    p=json.loads(proof_path.read_text());n=p['numeric_probe']
    assert p['baseline_sha256']==baseline and p['optimized_sha256']==digest(current)
    assert p['exact_history_transform_verified'] and p['inferred_history_primitive_contract']['passed'] and p['actual_primitive_functional_netlist_pass']
    assert n['input_signed_bits']==18 and n['output_signed_bits']==20 and n['accumulator_signed_bits']==40
    assert n['measured']['min_ii']==n['measured']['max_ii']==8 and n['integer_component_comparisons']>=24000
    assert n['single_bit_fault_rejected'] and n['out_start_fault_rejected'] and n['context_change_invalidates_history']
    assert p['paired_island_net_synthesis_saving']['LUT']>0 and p['paired_island_net_synthesis_saving']['FF']>0
    return {'name':name,'baseline_sha256':baseline,'compiled_sha256':digest(current),'proof':str(proof_path),'same_precision_schedule_logical_history':True,'physical_history_mapping':'three_TDP_BRAM_images_prefetch_and3recent_words'}
def main(a):
    r=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=a.review_only)
    c=check('before-RRC-paired-implementation');(out/'disk-before.json').write_text(json.dumps(c,indent=2));assert c['disk']['available_bytes']>=5*GIB
    try:
        old=a.baseline.resolve();new=a.optimized.resolve();n=a.numeric_probe.resolve();numeric=json.loads((n/'receipt.json').read_text());assert numeric['measured']['max_ii']==8
        assert (n/NAME).read_bytes()==new.read_bytes();assert transform(old.read_text())==new.read_text()
        for label,p in [('baseline',old),('optimized',new)]:shutil.copyfile(p,out/(label+'.sv'))
        shutil.copyfile(n/'rrc.hex',out/'rrc.hex')
        t='set_param general.maxThreads 2\n'
        for v in ('baseline','optimized'):
            t+='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY} [current_project]\n'
            t+=f'read_verilog -sv {{{out/(v+".sv")}}}\nsynth_design -top rrc_cost_probe -part xc7z020clg484-1 -mode out_of_context -flatten_hierarchy rebuilt\n'
            t+='create_clock -period 20 [get_ports clk]\nset_input_delay 2 -clock [get_clocks clk] [get_ports -filter {DIRECTION == IN && NAME != clk}]\nset_output_delay 2 -clock [get_clocks clk] [all_outputs]\nopt_design\n'
            t+=f'report_utilization -file {{{out/(v+".rpt")}}}\nclose_project\n'
        if not a.review_only:(out/'paired.tcl').write_text(t)
        bp=a.vivado_bin.resolve()
        def run(args,log):
            with (out/log).open('w') as f:subprocess.run([str(x) for x in args],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
        if not a.review_only:run([bp/'vivado','-mode','batch','-source',out/'paired.tcl','-nolog','-nojournal'],'synthesis.log')
        run([bp/'vivado','-mode','batch','-source',r/'pl/g0/check_rrc_bram_mapping.tcl','-nolog','-nojournal','-tclargs',n,out],'primitive-contract.log')
        log=(out/'primitive-contract.log').read_text();marker=next(x for x in log.splitlines() if x.startswith('RRC_HISTORY_PRIMITIVES_PASS'))
        tb=(n/'tb.sv').read_text()
        oldinst='rrc_cost_probe dut(clk,rst,iv,ir,istart,tag,data,ov,ready,otag,result,fences,ostart,ictx,ocontext);'
        newinst='rrc_netlist dut(.clk(clk),.rst(rst),.in_valid(iv),.in_ready(ir),.in_start(istart),.in_tag(tag),.in_data(data),.out_valid(ov),.out_ready(ready),.out_tag(otag),.out_data(result),.fence_count(fences),.out_start(ostart),.in_context(ictx),.out_context(ocontext));'
        assert oldinst in tb
        # Wait for simulated configuration GSR release before the test stream.
        tb=tb.replace(oldinst,newinst).replace('repeat(3) @(negedge clk);rst=0;','repeat(12) @(negedge clk);rst=0;')
        (out/'tb_netlist.sv').write_text(tb)
        for f in ('input.txt','expected.txt'):shutil.copyfile(n/f,out/f)
        run([bp/'xvlog','--sv','rrc_netlist.v','tb_netlist.sv',bp.parent/'data/verilog/src/glbl.v'],'netlist-compile.log')
        run([bp/'xelab','-L','unisims_ver','-L','secureip','tb','glbl','-s','rrc_netlist_tb','-mt','off'],'netlist-elaborate.log')
        run([bp/'xsim','rrc_netlist_tb','-runall'],'netlist-simulate.log')
        netlog=(out/'netlist-simulate.log').read_text();assert 'G0_RRC_PASS' in netlog and 'Fatal:' not in netlog
        oldcost=cost(out/'baseline.rpt');newcost=cost(out/'optimized.rpt')
        result={'scope':__doc__,'baseline_sha256':digest(old),'optimized_sha256':digest(new),'exact_history_transform_verified':True,'coefficient_ROM_sha256':digest(n/'rrc.hex'),'numeric_probe':numeric,'actual_primitive_functional_netlist_pass':True,'netlist_simulation':next(x for x in netlog.splitlines() if x.startswith('G0_RRC_PASS')),'inferred_history_primitive_contract':{'passed':True,'marker':marker,'log_sha256':hashlib.sha256(log.encode()).hexdigest()},'baseline_synthesis':oldcost,'optimized_synthesis':newcost,'paired_island_net_synthesis_saving':{k:oldcost[k]-newcost[k] for k in oldcost},'whole_board_savings_not_assumed':True,'full_G0_passed':False}
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='numeric_probe'}),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(check('after-RRC-paired-implementation'),indent=2)+'\n')
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('baseline','optimized','numeric-probe','output','vivado-bin'):p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--review-only',action='store_true');main(p.parse_args())
