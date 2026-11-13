#!/usr/bin/env python3
"""Paired OOC synthesis of actual retained HDMI observer and diagnostic-free copy.
Audit only: physical EOF/token/IRQ logic retained; no board design changed.
"""
import argparse,hashlib,json,re,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check,GIB

def trimmed(text):
    start=text.index('  hdmi_axi_read_probe hdmi_axi_read_probe_i (')
    end=text.index('.live_status(probe_live_status), .seen_status(probe_seen_status));',start)+len('.live_status(probe_live_status), .seen_status(probe_seen_status));')
    zero='\n'.join('  assign '+n+' = 0;' for n in ('probe_ar_fire_count','probe_ar_stall_cycle_count','probe_r_fire_count','probe_r_stall_cycle_count','probe_r_last_count','probe_axis_fire_count','probe_axis_stall_cycle_count','probe_axis_last_count','probe_last_araddr','probe_rresp_seen','probe_live_status','probe_seen_status'))
    text=text[:start]+zero+text[end:]
    pat=r'wire probe_read_addr = .*?;'
    text,n=re.subn(pat,"wire probe_read_addr = 1'b0; // Diagnostic window absent: SLVERR.",text,flags=re.S)
    assert n==1
    return text

def cost(path):
    s=path.read_text()
    def v(n):
        m=re.search(r'\|\s*'+re.escape(n)+r'\s*\|\s*([\d.]+)',s);assert m,n
        return float(m[1])
    return {'LUT':int(v('Slice LUTs*')),'FF':int(v('Slice Registers')),'BRAM36_equivalent':v('Block RAM Tile'),'DSP':int(v('DSPs'))}

def main(a):
    root=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    before=check('before-HDMI-diagnostic-redundancy-audit');(out/'disk-before.json').write_text(json.dumps(before,indent=2));assert before['disk']['available_bytes']>=5*GIB
    try:
        ref=a.platform.resolve()/'sources/pl/rtl';full=(ref/'hdmi_presentation_observer.v').read_text();compact=trimmed(full)
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ref/'hdmi_presentation_observer.v',ref/'hdmi_axi_read_probe.v')}
        tests=['tb_hdmi_presentation_observer','tb_hdmi_presentation_event_fifo']
        bp=a.vivado_bin.resolve()
        def run(cmd,where,log):
            with (where/log).open('w') as f:subprocess.run([str(x) for x in cmd],cwd=where,stdout=f,stderr=subprocess.STDOUT,check=True)
        receipts={}
        for variant,s in [('full',full),('compact',compact)]:
            dst=out/variant;dst.mkdir();(dst/'hdmi_presentation_observer.v').write_text(s);shutil.copyfile(ref/'hdmi_axi_read_probe.v',dst/'hdmi_axi_read_probe.v')
            for t in tests:
                tb=(root.parent/'pl/tb'/(t+'.sv')).read_text()
                # Probe-disabled reads must be explicit errors, never plausible
                # zero diagnostic counters. Also prove the response stays held.
                if variant=='compact' and t=='tb_hdmi_presentation_observer':
                    extra='''
    @(negedge clk); rready=0; araddr=16'h0080; arvalid=1;
    @(posedge clk); if(!arready)$fatal(1,"diagnostic-window not ready");
    @(negedge clk); arvalid=0;
    while(!rvalid)@(negedge clk);
    repeat(5)begin
      if(rresp!==2'b10 || rdata!==0 || !rvalid)$fatal(1,"disabled diagnostic window not held SLVERR/zero");
      @(negedge clk);
    end
    rready=1;@(posedge clk);@(negedge clk);
'''
                    tb=tb.replace('    $display("PASS tb_hdmi_presentation_observer");',extra+'    $display("PASS tb_hdmi_presentation_observer");')
                (dst/(t+'.sv')).write_text(tb)
            # Same original lifecycle regressions, no replacement surrogate.
            run([bp/'xvlog','--sv','hdmi_presentation_observer.v','hdmi_axi_read_probe.v',*[t+'.sv' for t in tests]],dst,'compile.log')
            receipts[variant]=[]
            for t in tests:
                run([bp/'xelab',t,'-s',t],dst,t+'-elaborate.log');run([bp/'xsim',t,'-runall'],dst,t+'-simulate.log')
                log=(dst/(t+'-simulate.log')).read_text();assert '$finish' in log and 'Fatal:' not in log and 'Error:' not in log
                receipts[variant].append({'test':t,'pass':True,'log_sha256':hashlib.sha256(log.encode()).hexdigest()})
        tcl='set_param general.maxThreads 2\n'
        for v in ('full','compact'):
            dst=out/v
            tcl+='create_project -in_memory -part xc7z020clg484-1\n'
            for n in ('hdmi_presentation_observer.v','hdmi_axi_read_probe.v'):tcl+=f'read_verilog {{{dst/n}}}\n'
            tcl+='synth_design -top hdmi_presentation_observer -part xc7z020clg484-1 -mode out_of_context -flatten_hierarchy rebuilt\ncreate_clock -period 20 [get_ports s_axi_aclk]\n'
            tcl+='set_input_delay 2 -clock [get_clocks s_axi_aclk] [get_ports -filter {DIRECTION == IN && NAME != s_axi_aclk}]\nset_output_delay 2 -clock [get_clocks s_axi_aclk] [all_outputs]\nopt_design\n'
            tcl+=f'report_utilization -file {{{dst/"utilization.rpt"}}}\nreport_utilization -hierarchical -file {{{dst/"hierarchy.rpt"}}}\nclose_project\n'
        (out/'audit.tcl').write_text(tcl);run([bp/'vivado','-mode','batch','-source',out/'audit.tcl','-nolog','-nojournal'],out,'synthesis.log')
        costs={v:cost(out/v/'utilization.rpt') for v in ('full','compact')}
        receipt={'scope':__doc__,'reference_platform':str(a.platform),'source_sha256':hashes,'compact_sha256':hashlib.sha256(compact.encode()).hexdigest(),'production_core_0x00_to_0x50_unchanged':True,'diagnostic_0x80_to_0xac_read_response':'SLVERR, zero data','only_replaced_probe_instance_and_window_admission':True,'simulation':receipts,'cost':costs,'island_net_synthesis_saving':{k:costs['full'][k]-costs['compact'][k] for k in costs['full']},'whole_board_saving_booked':False,'whole_board_routed':False,'physical_HDMI_ownership_validation':False,'production_adopted':False}
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(check('after-HDMI-diagnostic-redundancy-audit'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ('platform','output','vivado-bin'):p.add_argument('--'+n,type=Path,required=True)
    main(p.parse_args())
