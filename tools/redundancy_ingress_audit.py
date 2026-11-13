#!/usr/bin/env python3
"""Controlled synthesis of legacy+compact versus compact-only ingress islands.

Not a deployed board replacement: WFIFO/CPACK/reset-guard/CSR integration is open.
Flush writes must be gated by the owner; RAM saving reduces burst capacity.
"""
import argparse, hashlib, json, re, shutil, subprocess
from pathlib import Path
from workspace_storage_guard import check, GIB

WRAPPER='''`timescale 1ns/1ps
module audit_ingress #(parameter LEGACY=1)(
 input wire clk,resetn,flush,iv,input wire [127:0] data,
 input wire ready,output wire ov,output wire [127:0] result,
 output wire loss,ack);
 wire cv;wire [127:0] cd;wire oldloss,oldack;
 generate if(LEGACY) begin:old
 rf_rx_elastic_fifo #(.DATA_WIDTH(128)) fifo(
 .clk(clk),.resetn(resetn),.flush_sync(flush),.flush_ack(oldack),
 .packed_fifo_wr_en(iv&&!flush),.packed_fifo_wr_data(data),
 .packed_fifo_wr_overflow(oldloss),.accepted_write(),.ingress_drop(),
 .m_axis_tdata(cd),.m_axis_tkeep(),.m_axis_tstrb(),.m_axis_tvalid(cv),
 .m_axis_tready(1'b1),.m_axis_tlast());
 end else begin:new_path
 assign cv=iv&&!flush;assign cd=data;assign oldloss=0;assign oldack=0;
 end endgenerate
 wire newloss,newack;
 compact_stream_fifo_cost_probe fifo(
 .clk(clk),.resetn(resetn),.flush_sync(flush),.flush_ack(newack),
 .packed_fifo_wr_en(cv&&!flush),.packed_fifo_wr_data(cd),
 .packed_fifo_wr_overflow(newloss),.accepted_write(),.ingress_drop(),
 .m_axis_tdata(result),.m_axis_tkeep(),.m_axis_tstrb(),.m_axis_tvalid(ov),
 .m_axis_tready(ready),.m_axis_tlast());
 assign loss=oldloss|newloss;assign ack=newack;
endmodule
'''
TB='''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rstn=0,flush=0,iv=0,ready=1;reg [127:0] data=0;
 wire [1:0] ov,loss,ack;wire [127:0] d[0:1];
 audit_ingress #(.LEGACY(1)) a(clk,rstn,flush,iv,data,ready,ov[0],d[0],loss[0],ack[0]);
 audit_ingress #(.LEGACY(0)) b(clk,rstn,flush,iv,data,ready,ov[1],d[1],loss[1],ack[1]);
 integer count[0:1],last[0:1],drops[0:1];integer j,k,cycle=0,phase=0;
 reg [127:0] held_data[0:1];reg held[0:1];
 always @(posedge clk) begin
 cycle=cycle+1;
 for(j=0;j<2;j=j+1) begin
  if(!rstn||flush)held[j]=0;
  else begin
   if(held[j]&&(!ov[j]||d[j]!==held_data[j]))$fatal(1,"held changed");
   held[j]=ov[j]&&!ready;held_data[j]=d[j];
   if(ov[j]&&ready)begin
    if(d[j][127:64]!==~d[j][63:0] || d[j][63:0]<=last[j])$fatal(1,"corrupt/duplicate/reorder");
    last[j]=d[j][63:0];count[j]=count[j]+1;
   end
   if(loss[j])drops[j]=drops[j]+1;
  end
 end
 end
 task send(input integer n,input integer base);
 begin for(k=0;k<n;k=k+1)begin data={~(64'(base+k)),64'(base+k)};iv=1;@(negedge clk);end iv=0;end endtask
 task drain;begin iv=0;ready=1;repeat(1500)@(negedge clk);if(ov)$fatal(1,"unbounded drain");end endtask
 initial begin
 for(j=0;j<2;j=j+1)begin count[j]=0;last[j]=0;drops[j]=0;held[j]=0;end
 repeat(6)@(negedge clk);rstn=1;repeat(8)@(negedge clk);
 send(2000,1);drain;
 ready=0;send(96,2001);drain;
 if(count[0]!=2096||count[1]!=2096||drops[0]||drops[1])$fatal(1,"loss in supported window");
 ready=0;send(1000,2097);drain;
 if(!drops[0]||!drops[1])$fatal(1,"overload not counted");
 flush=1;repeat(6)@(negedge clk);flush=0;repeat(8)@(negedge clk);
 send(16,4000);drain;
 if(last[0]!=4015||last[1]!=4015)$fatal(1,"failed continued reception");
 $display("REDUNDANCY_INGRESS_PASS exact_nominal_each=2096 legacy_total=%0d merged_total=%0d legacy_loss=%0d merged_loss=%0d",count[0],count[1],drops[0],drops[1]);$finish;
 end
 initial begin #1000000;$fatal(1,"timeout");end
endmodule
'''

def main(a):
 root=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
 before=check('before-redundancy-ingress-audit');(out/'disk-before.json').write_text(json.dumps(before,indent=2))
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Storage first')
 try:
  src=[root/'build/g0-adc-platform-r4/sources/pl/rtl/rf_rx_elastic_fifo.v',root/'sim/g0/compact_stream_fifo_cost_probe.sv']
  hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in src}
  for p in src:shutil.copyfile(p,out/p.name)
  (out/'audit.sv').write_text(WRAPPER);(out/'tb.sv').write_text(TB)
  def run(cmd,log):
   with (out/log).open('w') as f:subprocess.run([str(x) for x in cmd],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  bp=a.vivado_bin.resolve();names=[p.name for p in src]
  run([bp/'xvlog','--sv','-L','xpm',*names,'audit.sv','tb.sv'],'compile.log')
  run([bp/'xelab','--debug','typical','-L','xpm','tb','-s','audit_tb'],'elaborate.log')
  run([bp/'xsim','audit_tb','-runall'],'simulate.log')
  log=(out/'simulate.log').read_text();assert 'REDUNDANCY_INGRESS_PASS' in log and 'Fatal:' not in log,log[-1500:]
  tcl='set_param general.maxThreads 2\n'
  for variant in (1,0):
   tcl+='create_project -in_memory -part xc7z020clg484-1\nset_property XPM_LIBRARIES {XPM_MEMORY} [current_project]\n'
   for n in names+['audit.sv']:tcl+=f'read_verilog -sv {{{out/n}}}\n'
   tcl+=f'synth_design -top audit_ingress -part xc7z020clg484-1 -mode out_of_context -flatten_hierarchy rebuilt -generic LEGACY={variant}\n'
   tcl+='create_clock -period 20 [get_ports clk]\nset_input_delay 2 -clock [get_clocks clk] [get_ports -filter {DIRECTION == IN && NAME != clk}]\nset_output_delay 2 -clock [get_clocks clk] [all_outputs]\nopt_design\n'
   tcl+=f'report_utilization -file variant{variant}.rpt\nreport_utilization -hierarchical -file hierarchy{variant}.rpt\nwrite_checkpoint -force variant{variant}.dcp\nclose_project\n'
  (out/'audit.tcl').write_text(tcl);run([bp/'vivado','-mode','batch','-source',out/'audit.tcl','-nolog','-nojournal'],'synthesis.log')
  def cost(v):
   s=(out/f'variant{v}.rpt').read_text()
   def row(n):
    m=re.search(r'\|\s*'+re.escape(n)+r'\s*\|\s*([\d.]+)',s);assert m,n
    return float(m[1])
   return {'LUT':int(row('Slice LUTs*')),'FF':int(row('Slice Registers')),'BRAM36_equivalent':row('Block RAM Tile'),'DSP':int(row('DSPs'))}
  old,new=cost(1),cost(0);saving={k:old[k]-new[k] for k in old}
  receipt={'scope':__doc__,'source_sha256':hashes,'simulation':next(l for l in log.splitlines() if 'REDUNDANCY_INGRESS_PASS' in l),'legacy_cascade_synthesis':old,'merged_synthesis':new,'island_net_synthesis_saving':saving,'full_board_saving_booked':False,'physical_CDC_reset_guard_CSR_integration_passed':False,'routed_timing_passed':False,'full_G0_passed':False}
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(check('after-redundancy-ingress-audit'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);main(p.parse_args())
