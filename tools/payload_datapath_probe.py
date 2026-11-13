#!/usr/bin/env python3
"""Clock-connected SG-node interpolation/sample join/full-H rotation/exact ML.
Capture inputs are integer upstream RRC/F1/H and previously RTL-verified SG nodes;
shared-pool business scheduling is NOT yet included. Observed RTL bits are parsed
with independent link CRC/identity and compared to continuous floating ML.
"""
import argparse,hashlib,json,random,shutil,subprocess
from pathlib import Path
import numpy as np
from payload_reference import frames,rotate,ml,packet_results,interpolate_phase,SIGNS
from bpsk_header_probe import pack
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
SOURCES=['payload_phase_cost_probe.sv','payload_rotate_cost_probe.sv','payload_ml_cost_probe.sv','payload_math_cost_probe.sv','payload_datapath_cost_probe.sv']
TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;
reg rst=1,cancel=0,fv=0,gv=0,iv=0,ready=0,force_hold=0;reg [31:0] fi=0,fc=0,fg=0,gi=0,gc=0,gg=0,ii=0,ic=0,ig=0;
reg [15:0] fe=0,ge=0,ie=0;reg [207:0] h=0;reg [8:0] group=0;reg [63:0] nominal=0,steps=0;
reg signed [40:0] l0=0,r0=0,l1=0,r1=0;reg [14:0] symbol=0;reg [2:0] error=0;reg [83:0] y=0;
wire fr,gr,ir,valid,busy;wire [31:0] oi,oc,og;wire [15:0] oe;wire [14:0] os;wire [2:0] oerr;wire [3:0] decision;wire [61:0] metric;
payload_datapath_cost_probe dut(clk,rst,cancel,fv,fr,fi,fc,fg,fe,h,gv,gr,gi,gc,gg,ge,group,nominal,steps,l0,r0,l1,r1,iv,ir,ii,ic,ig,ie,symbol,error,y,valid,ready,oi,oc,og,oe,os,oerr,decision,metric,busy);
reg [83:0] samples[0:368255];reg [449:0] gold[0:368255];
reg [207:0] matrices[0:20];reg [63:0] frequency[0:20];reg signed [40:0] nodes[0:11507];
integer cycle=0,j=0,base=0,current=0,k,g,n,results=0,consumed=0,rotations=0,phase_checks=0,held_checks=0,source_stalls=0,last_accept=-1,min_ii=100000,max_ii=0,fd;
integer expected_results=0,revoked=0,cancelled=0;reg seen=0,held=0;reg [195:0] held_view;wire [195:0] view={oe,og,oc,oi,os,oerr,decision,metric};
integer first_frame_start=0,first_frame_cycles=0,largest_group_gap=0;
reg capture_mode=0;reg [2:0] expected_error=0;reg [31:0] owner_id,owner_ctx,owner_gen;reg [15:0] owner_epoch;
always @(negedge clk)ready=!force_hold && (j==0 || cycle%17>4);
always @(posedge clk)begin
 cycle=cycle+1;
 if(rst||cancel)begin if(seen)revoked=revoked+1;seen=0;held=0;end
 else begin
  if(iv&&!ir)source_stalls=source_stalls+1;
  if(iv&&ir&&capture_mode)begin
   current=base+symbol;
   if(dut.phases!==gold[current][63:0])$fatal(1,"payload exact interpolated phase oracle frame%0d symbol%0d actual%h expected%h",j,symbol,dut.phases,gold[current][63:0]);
   phase_checks=phase_checks+1;
   if(j==0&&symbol%64!=0&&last_accept>=0)begin if(cycle-last_accept<min_ii)min_ii=cycle-last_accept;if(cycle-last_accept>max_ii)max_ii=cycle-last_accept;end
   if(j==0&&symbol%64==0&&symbol!=0&&cycle-last_accept>largest_group_gap)largest_group_gap=cycle-last_accept;
   last_accept=cycle;
  end
  if(dut.math.rotate_valid&&dut.math.rotate_pop&&capture_mode)begin
   current=base+dut.math.symbol;
   if(dut.math.error||{dut.math.h,dut.math.y}!==gold[current][383:64])$fatal(1,"payload full rotation oracle frame%0d symbol%0d",j,dut.math.symbol);rotations=rotations+1;
  end
  if(valid&&!seen)begin
   if(oi!==owner_id||oc!==owner_ctx||og!==owner_gen||oe!==owner_epoch||oerr!==expected_error)$fatal(1,"payload result identity/error");
   if(capture_mode)begin
    current=base+os;if({metric,decision}!==gold[current][449:384])$fatal(1,"payload full-width ML oracle frame%0d symbol%0d",j,os);
    $fdisplay(fd,"%0d %0d %x",j,os,decision);
   end else if(oerr!=0&&(metric!==0||decision!==0))$fatal(1,"payload invalid data escaped");
   results=results+1;seen=1;
  end
  if(held&&(!valid||view!==held_view))$fatal(1,"payload held result changed");
  if(valid&&ready)begin consumed=consumed+1;seen=0;end
  held=valid&&!ready;if(held)begin held_view=view;held_checks=held_checks+1;end
 end
end
task frame(input integer f);
 begin
 j=f;base=f*17536;owner_id=32'hfffffff0+f;owner_ctx=32'h80000000^f;owner_gen=32'hffffffff-3*f;owner_epoch=16'hffff-f;
 fi=owner_id;fc=owner_ctx;fg=owner_gen;fe=owner_epoch;h=matrices[f];fv=1;
 @(posedge clk);while(!fr)@(posedge clk);@(negedge clk);fv=0;fi=~fi;fc=~fc;fg=~fg;fe=~fe;h=~h;
 end endtask
task configure_group(input integer b);
 begin
 gi=owner_id;gc=owner_ctx;gg=owner_gen;ge=owner_epoch;group=9'(b);steps=frequency[j];
 nominal={32'(frequency[j][63:32]*32'(1538+66*b)),32'(frequency[j][31:0]*32'(1538+66*b))};
 l0=nodes[j*548+2*b];r0=nodes[j*548+2*(b<273?b+1:273)];l1=nodes[j*548+2*b+1];r1=nodes[j*548+2*(b<273?b+1:273)+1];gv=1;
 @(posedge clk);while(!gr)@(posedge clk);@(negedge clk);gv=0;gi=~gi;gc=~gc;gg=~gg;ge=~ge;group=~group;nominal=~nominal;steps=~steps;l0=~l0;r0=~r0;l1=~l1;r1=~r1;
 end endtask
task sample(input integer s,input integer fault);
 begin
 ii=owner_id;ic=owner_ctx;ig=owner_gen;ie=owner_epoch;symbol=15'(s);error=0;y=samples[base+s];
 case(fault)1:ii=~ii;2:ic=ic^32'h80000000;3:ig=ig^32'h80000000;4:ie=ie^16'h8000;5:symbol=symbol^15'h4000;6:error=3'd6;endcase
 iv=1;@(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
 end endtask
task cancel_check(input bit reset_instead);
 begin
 if(reset_instead)rst=1;else cancel=1;#1;if(valid||ir||fr||gr)$fatal(1,"payload cancel edge");repeat(2)@(negedge clk);rst=0;cancel=0;iv=0;fv=0;gv=0;cancelled=cancelled+1;
 repeat(64)begin @(negedge clk);if(valid||busy||!fr||gr||ir)$fatal(1,"payload cancelled ownership revived");end
 end endtask
initial begin
 $readmemh("samples.hex",samples);$readmemh("gold.hex",gold);$readmemh("H.hex",matrices);$readmemh("steps.hex",frequency);$readmemh("nodes.hex",nodes);fd=$fopen("observed.txt","w");repeat(5)@(negedge clk);rst=0;
 for(n=0;n<21;n=n+1)begin
  capture_mode=1;expected_error=0;if(n==0)first_frame_start=cycle;frame(n);
  for(g=0;g<274;g=g+1)begin configure_group(g);for(k=0;k<64;k=k+1)sample(64*g+k,0);end
  while(busy)@(negedge clk);if(results!=(n+1)*17536||consumed!=results)$fatal(1,"payload capture census");
  if(n==0)begin first_frame_cycles=cycle-first_frame_start;if(first_frame_cycles>674461)$fatal(1,"payload arithmetic service below1.3Msymbol/s");$display("G0_PAYLOAD_SERVICE_PASS symbols=17536 cycles=%0d largest_group_input_gap=%0d clock_Hz=50000000",first_frame_cycles,largest_group_gap);end
 end
 capture_mode=0;
 for(n=1;n<=6;n=n+1)begin
  frame(0);configure_group(0);expected_error=n==6?3'd6:3'd3;sample(0,n);while(!valid)@(negedge clk);@(negedge clk);while(valid)@(negedge clk);cancel_check(0);
 end
 expected_error=0;
 // Configuration is rejected while work holds the matrix ownership.
 frame(0);configure_group(0);capture_mode=1;sample(0,0);while(dut.math.rotation.state!=4)@(negedge clk);fv=1;
 repeat(8)begin @(negedge clk);if(fr)$fatal(1,"payload busy matrix overwritten");end fv=0;while(dut.math.busy)@(negedge clk);capture_mode=0;cancel_check(0);
 for(n=0;n<14;n=n+1)begin
  frame(0);force_hold=n==13;expected_error=0;
  if(n<5)begin
   configure_group(0);
   case(n)0:while(!(dut.phasor.state==2&&dut.phasor.iteration==0))@(negedge clk);1:while(!(dut.phasor.state==2&&dut.phasor.iteration==41))@(negedge clk);2:while(dut.phasor.state!=4)@(negedge clk);3:while(dut.phasor.state!=5)@(negedge clk);4:while(!dut.phase_valid)@(negedge clk);endcase
  end else begin
   configure_group(0);sample(0,0);
   case(n)
    5:while(dut.math.rotation.state!=1)@(negedge clk);
    6:while(dut.math.rotation.state!=3)@(negedge clk);
    7:while(!(dut.math.rotation.state==4&&dut.math.rotation.issued==0))@(negedge clk);
    8:while(!(dut.math.rotation.state==4&&dut.math.rotation.issued==15))@(negedge clk);
    9:while(!(dut.math.detector.error_valid&&dut.math.detector.error_candidate==3))@(negedge clk);
    10:while(!(dut.math.detector.square_valid&&dut.math.detector.square_candidate==3))@(negedge clk);
    11:while(!(dut.math.detector.metric_valid&&dut.math.detector.metric_candidate==14))@(negedge clk);
    12:while(!(dut.math.detector.product_valid&&dut.math.detector.product_candidate==7))@(negedge clk);
    13:begin while(!valid)@(negedge clk);repeat(7)@(negedge clk);end
   endcase
  end
  cancel_check(n==12);force_hold=0;
 end
 if(phase_checks!=368257||rotations!=368257||results!=368264||consumed!=368263||cancelled!=21||revoked!=1||min_ii!=22||max_ii!=22)$fatal(1,"payload total census phase%0d rot%0d result%0d cons%0d cancel%0d revoked%0d II%0d..%0d",phase_checks,rotations,results,consumed,cancelled,revoked,min_ii,max_ii);
 $fclose(fd);$display("G0_PAYLOAD_DATAPATH_PASS capture_frames=21 symbols=368256 exact_phases=%0d exact_rotations=%0d results=%0d consumed=%0d cancels=%0d revoked=%0d held_checks=%0d source_stalls=%0d steady_input_II=%0d..%0d",phase_checks,rotations,results,consumed,cancelled,revoked,held_checks,source_stalls,min_ii,max_ii);$finish;
end
initial begin #2000000000;$fatal(1,"payload timeout frame%0d group%0d sample%0d rotstate%0d phase_state%0d",j,g,k,dut.math.rotation.state,dut.phasor.state);end
endmodule
'''
def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve();before=disk_check('before-payload-datapath');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  metadata=[];ff={n:(out/(n+'.hex')).open('w') for n in ('samples','gold','H','steps','nodes')}
  try:
   for f in frames(a.reference.resolve(),a.phase_reference.resolve(),a.header_reference.resolve()):
    ff['H'].write(f"{pack(f['H'].ravel(),26):052x}\n");ff['steps'].write(f"{pack(f['steps'],32):016x}\n")
    for row in f['sg']:
     for v in row:ff['nodes'].write(f'{int(v)%(1<<41):011x}\n')
    for i in range(17536):
     ff['samples'].write(f"{pack(f['samples'][i].ravel(),21):021x}\n");v=pack(f['phases'][i],32)+(pack(f['scaled'][i],26)<<64)+(pack(f['effective'][i],27)<<168)+(int(f['decision'][i])<<384)+(int(f['metric'][i])<<388);ff['gold'].write(f'{v:0113x}\n')
    r,_=packet_results(f['float_decision'],f['session'],f['superframe']);metadata.append({'model':f['model'],'window':f['window'],'session':f['session'],'superframe':f['superframe'],'float_packets':r,'expected_decisions':f['decision'].astype(int).tolist()})
  finally:
   for f in ff.values():f.close()
  shutil.copyfile(a.phase_reference/'sine.hex',out/'sine.hex');(out/'tb.sv').write_text(TB)
  for n in SOURCES:shutil.copyfile(project/'sim/g0'/n,out/n)
  def run(step,args):
   with (out/(step+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',*[str(out/n) for n in SOURCES],str(out/'tb.sv')]);run('elaborate',[str(bp/'xelab'),'tb','-s','payload_datapath_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'payload_datapath_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_PAYLOAD_DATAPATH_PASS' in log and 'Fatal:' not in log,log[-4000:]
  rows=[s.split() for s in (out/'observed.txt').read_text().splitlines()];assert len(rows)==368257;packet_rows=[]
  for j,m in enumerate(metadata):
   got=rows[j*17536:(j+1)*17536];assert all(int(r[0])==j and int(r[1])==i for i,r in enumerate(got));d=np.array([int(r[2],16) for r in got]);assert d.tolist()==m['expected_decisions'];r,_=packet_results(d,m['session'],m['superframe']);assert all(not f['accepted'] or g['accepted'] for f,g in zip(m['float_packets'],r));packet_rows.append({k:m[k] for k in ('model','window','superframe')}|{'RTL_good_packets':sum(p['accepted'] for p in r),'float_good_packets':sum(p['accepted'] for p in m['float_packets'])})
  result={'scope':__doc__,'simulation':next(s for s in log.splitlines() if s.startswith('G0_PAYLOAD_DATAPATH_PASS')),'service':next(s for s in log.splitlines() if s.startswith('G0_PAYLOAD_SERVICE_PASS')),'packets':packet_rows,'additional_CRC_erasures':0,'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'routed_OOC':None,'g0_passed':False}
  if not a.sim_only:
   tcl='create_project -in_memory -part xc7z020clg484-1\nset_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in SOURCES)+'''synth_design -top payload_datapath_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20 [get_ports clk]
set_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2 -clock processing [get_ports -filter {DIRECTION == OUT}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force payload.dcp
'''
   (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out)
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-payload-datapath'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--phase-reference',type=Path,required=True);p.add_argument('--header-reference',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
