#!/usr/bin/env python3
"""Streamed FEC: real RAM/inverse, early-source dedup, k queue and wall deadlines.
Only encoded bytes/CRC verdicts enter the DUT; source truth is monitor-only.
Not session/range admission, ordering, atomic DMA, RF/video or full G0.
"""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
import numpy as np
from fec_inverse_cost_probe import vandermonde_generator,routed_measurements
from fec_oracle import inverse
from g0_packet_fec import GENERATOR,encode
from workspace_storage_guard import check as disk_check,GIB
SOURCES=['fec_inverse_cost_probe.sv','fec_memory_cost_probe.sv','fec_stream_recovery_cost_probe.sv']

TB=r'''`timescale 1ns/1ps
module tb;
localparam BANKS=@BANKS@,BW=$clog2(BANKS),JOBS=21,DEADLINE=25000000;
reg clk=0;always #10 clk=~clk;reg rst=1,ov=0,sv=0,rv=0,cv=0,pv=0,bv=0,bl=0,cg=1,pa=0;
reg [BW-1:0] ob=0,sb=0,rb=0,cb=0,pb=0;reg [31:0] oj=0,sj=0,rj=0,cj=0,pj=0;reg [4:0] pi=0;reg [7:0] bd=0;
reg sr=0,tr=1;integer ready_mode=1;reg status_mode=1;
wire ore,sre,rre,pre,bre,sov,slast,recovered,tv,sabort;wire [7:0] data;wire [BW-1:0] sob,tbank;wire [31:0] soj,tjob,elapsed,committed,badcrc,badlen,stale,duplicate,cancels,aborts,expired;
wire [4:0] soi;wire [8:0] soo;wire [2:0] code;wire [19:0] missing,delivered;
fec_stream_recovery_cost_probe dut(clk,rst,ov,ore,ob,oj,sv,sre,sb,sj,rv,rre,rb,rj,cv,cb,cj,pv,pre,pb,pj,pi,bv,bre,bd,bl,cg,
 sov,sr,data,sob,soj,soi,soo,slast,recovered,tv,tr,tbank,tjob,code,missing,elapsed,committed,badcrc,badlen,stale,duplicate,cancels,pa,sabort,delivered,aborts,expired);
reg [7:0] golden[0:JOBS*10240-1],coded[0:JOBS*16384-1];reg [31:0] ids[0:JOBS-1],original[0:JOBS-1];
integer counts[0:JOBS-1],positions[0:JOBS-1][0:19],queue_cycle[0:JOBS-1],start_cycle[0:JOBS-1],end_cycle[0:JOBS-1],terminal_code[0:JOBS-1],restore_count[0:JOBS-1];
reg [19:0] seen[0:JOBS-1],want_seen[0:JOBS-1],want_missing[0:JOBS-1];integer want_code[0:JOBS-1];reg ended[0:JOBS-1],revoked[0:JOBS-1];
integer opened[0:BANKS-1],cycle=0,j,k,i,idx,which,open_slot,load_count=0,checked=0,restored=0,holds=0,statuses=0,early_writes=0,concurrent_MAC=0,deadline_edges=0,first_commit,early_delay,max_service=0,burst_begin,burst_cycles;
reg source_held=0,status_held=0;reg [BW+55:0] source_view;reg [BW+86:0] status_view;
wire [BW+55:0] current_source={sob,soj,soi,soo,slast,recovered,data};
wire [BW+86:0] current_status={tbank,tjob,code,missing,elapsed};
function automatic integer find_job(input [31:0] v);integer n;begin find_job=-1;for(n=0;n<JOBS;n=n+1)if(ids[n]==v)find_job=n;end endfunction
always @(negedge clk)begin sr=ready_mode==2||(ready_mode==1&&cycle%13>2)||(ready_mode==3&&soo<128);tr=status_mode&&cycle%11>2;end
always @(posedge clk)begin
 cycle=cycle+1;
 if(rst)begin source_held=0;status_held=0;for(integer n=0;n<BANKS;n=n+1)opened[n]=-1;end
 else begin
  if(dut.state!=0 && dut.bank_delivered[dut.active_bank]!==seen[find_job(dut.active_job)])$fatal(1,"stream delivered bitmap before whole source");
  if(source_held&&!sabort&&(!sov||current_source!==source_view))$fatal(1,"stream source held owner changed");
  if(status_held&&!dut.cancel_core&&(!tv||current_status!==status_view))$fatal(1,"stream terminal held changed");
  if(sov&&sr)begin
   which=find_job(soj);
   if(which<0||revoked[which]||ended[which]||seen[which][soi]||soo!==positions[which][soi][8:0]||data!==golden[which*10240+soi*512+soo]||slast!==(soo==511)||recovered!==!original[which][soi])$fatal(1,"stream source byte/dedup oracle job%h row%0d offset%0d",soj,soi,soo);
   positions[which][soi]=positions[which][soi]+1;counts[which]=counts[which]+1;checked=checked+1;
   if(recovered)begin restored=restored+1;restore_count[which]=restore_count[which]+1;end
   if(slast)seen[which][soi]=1;
  end
  if(tv&&tr)begin
   which=find_job(tjob);
   if(which<0||revoked[which]||ended[which]||code!=want_code[which]||missing!==want_missing[which]||seen[which]!==want_seen[which]||delivered!==seen[which])$fatal(1,"stream terminal oracle job%h code%0d missing%h seen%h delivered%h",tjob,code,missing,seen[which],delivered);
   if(code==4&&elapsed!=1000000)$fatal(1,"stream active watchdog period");
   if(code==0&&elapsed>=1000000)$fatal(1,"stream successful service deadline");
   if(code==0&&elapsed>max_service)max_service=elapsed;
   ended[which]=1;terminal_code[which]=code;end_cycle[which]=cycle;statuses=statuses+1;
   $display("FEC_STREAM_TERMINAL slot=%0d job=%h code=%0d bytes=%0d restored=%0d cycles=%0d missing=%h",which,tjob,code,counts[which],restore_count[which],elapsed,missing);
  end
  if(ov&&ore)opened[ob]=cycle;
  for(integer n=0;n<BANKS;n=n+1)begin
   if(opened[n]>=0 && cycle-opened[n]>=DEADLINE && (dut.bank_state[n]==1||dut.bank_state[n]==2||dut.bank_state[n]==3) && !dut.expiring[n])$fatal(1,"stream bank deadline late");
   if(dut.expiring[n])begin
   if(opened[n]<0||cycle-opened[n]!=DEADLINE)$fatal(1,"stream exact bank deadline bank%0d got%0d",n,cycle-opened[n]);deadline_edges=deadline_edges+1;
   end
  end
  if(dut.queue_now)begin which=find_job(dut.rx_job);queue_cycle[which]=cycle;end
  if(dut.state==0&&dut.bank_state[dut.scan_bank]==2&&!dut.expiring[dut.scan_bank])begin which=find_job(dut.bank_job[dut.scan_bank]);start_cycle[which]=cycle;end
  if(dut.load_we&&dut.early_mode&&dut.rx_bank==dut.active_bank)early_writes=early_writes+1;
  if(dut.load_we&&dut.state==6&&dut.op_valid&&dut.op_ready)concurrent_MAC=concurrent_MAC+1;
  source_held=sov&&!sr;if(source_held)begin source_view=current_source;holds=holds+1;end
  status_held=tv&&!tr;if(status_held)begin status_view=current_status;holds=holds+1;end
 end
end
task open_job(input integer bank,input integer slot);
 begin ob=BW'(bank);oj=ids[slot];ov=1;@(posedge clk);while(!ore)@(posedge clk);@(negedge clk);ov=0;end
endtask
task seal_job(input integer bank,input integer slot);
 begin sb=BW'(bank);sj=ids[slot];sv=1;@(posedge clk);while(!sre)@(posedge clk);@(negedge clk);sv=0;end
endtask
task release_job(input integer bank,input integer slot);
 begin while(!ended[slot])@(negedge clk);rb=BW'(bank);rj=ids[slot];rv=1;@(posedge clk);while(!rre)@(posedge clk);@(negedge clk);rv=0;end
endtask
task packet(input integer bank,input integer slot,input integer index,input integer length,input integer good,input integer mutate,input integer abort_at);
 begin
 pb=BW'(bank);pj=ids[slot];pi=5'(index);pv=1;@(posedge clk);while(!pre)@(posedge clk);@(negedge clk);pv=0;cg=good;
 for(integer n=0;n<length;n=n+1)begin
  if(n==abort_at)begin bd=coded[slot*16384+index*512+(n%512)];bv=1;bl=1;pa=1;#1;if(bre||dut.load_we)$fatal(1,"stream packet abort fence late");@(negedge clk);pa=0;bv=0;bl=0;break;end
  bd=coded[slot*16384+index*512+(n%512)]^(mutate?8'h55:8'd0);bl=n==length-1;bv=1;
  @(posedge clk);while(!bre)@(posedge clk);@(negedge clk);bv=0;bl=0;
 end
 cg=1;load_count=load_count+1;
 end
endtask
task cancel_job(input integer bank,input integer slot);
 begin cb=BW'(bank);cj=ids[slot];cv=1;#1;if(sov||tv||!sabort)$fatal(1,"stream current active cancel fence");revoked[slot]=1;
 @(negedge clk);cv=0;repeat(2)@(negedge clk);end
endtask
task wait_full(input integer slot);
 begin while(!ended[slot])@(negedge clk);if(counts[slot]!=10240||seen[slot]!=20'hfffff)$fatal(1,"stream complete source census");end
endtask
initial begin
 $readmemh("source.hex",golden);$readmemh("coded.hex",coded);
 ids[0]=32'h80000001;ids[1]=32'h80000002;ids[2]=32'h80000003;ids[3]=32'h80000004;ids[4]=32'h80000005;ids[5]=32'hc0000005;ids[6]=32'h80000006;ids[7]=32'h80000007;ids[8]=32'h80000008;ids[9]=32'h80000009;ids[10]=32'hc0000009;ids[11]=32'h8000000a;
 for(j=12;j<JOBS;j=j+1)ids[j]=32'h90000000+32'(j);
 for(j=0;j<JOBS;j=j+1)begin counts[j]=0;restore_count[j]=0;seen[j]=0;ended[j]=0;revoked[j]=0;queue_cycle[j]=-1;start_cycle[j]=-1;end_cycle[j]=-1;want_code[j]=0;want_seen[j]=20'hfffff;want_missing[j]=0;original[j]=32'h000fffff;for(k=0;k<20;k=k+1)positions[j][k]=0;end
 original[1]=32'hfffff000;original[2]=32'hffffe000;original[3]=0;original[4]=32'hfffff000;original[5]=32'hfff000ff;original[6]=1;original[7]=0;original[8]=32'hfffff000;original[9]=8;original[11]=32'h800;
 for(j=12;j<JOBS;j=j+1)original[j]=32'hfffff000;
 want_code[2]=1;want_seen[2]=20'hfe000;want_missing[2]=20'h01fff;want_code[3]=1;want_seen[3]=0;want_missing[3]=20'hfffff;
 for(j=6;j<=8;j=j+1)begin want_code[j]=j==6?4:5;want_seen[j]=0;want_missing[j]=20'hfffff;end
 repeat(5)@(negedge clk);rst=0;
 open_job(0,0);packet(0,0,7,512,1,0,-1);first_commit=cycle;
 while(!seen[0][7])begin @(negedge clk);if(cycle-first_commit>8192)$fatal(1,"stream early delivery not before k");end
 early_delay=cycle-first_commit;if(dut.bank_count[0]!=1||dut.bank_state[0]!=1)$fatal(1,"stream early source waited for seal");
 packet(0,0,7,512,1,1,-1);for(k=0;k<512;k=k+1)if(dut.storage.memory[7*512+k]!==golden[7*512+k])$fatal(1,"stream duplicate overwrote committed RAM");
 packet(0,0,8,511,1,0,-1);packet(0,0,8,512,0,0,-1);packet(0,0,8,512,1,0,511);
 if(dut.bank_mask[0][8]||seen[0][8]||dut.bank_count[0]!=1)$fatal(1,"stream incomplete/bad packet committed");
 packet(0,0,8,512,1,0,-1);
 for(k=0;k<20;k=k+1)if(k!=7&&k!=8)packet(0,0,k,512,1,0,-1);
 if(dut.bank_state[0]==1||dut.bank_count[0]!=20)$fatal(1,"stream automatic k queue absent");
 wait_full(0);if(start_cycle[0]-queue_cycle[0]>8192)$fatal(1,"stream k dispatch latency");release_job(0,0);
 open_job(0,1);for(k=12;k<32;k=k+1)packet(0,1,k,512,1,0,-1);
 while(dut.state!=6)@(negedge clk);
 open_job(1,2);for(k=13;k<32;k=k+1)packet(1,2,k,512,1,0,-1);seal_job(1,2);
 wait_full(1);if(restore_count[1]!=6144||start_cycle[1]-queue_cycle[1]>8192)$fatal(1,"stream twelve-source recovery/dispatch");release_job(0,1);release_job(1,2);
 open_job(0,3);seal_job(0,3);release_job(0,3);
 open_job(0,4);for(k=12;k<32;k=k+1)packet(0,4,k,512,1,0,-1);while(dut.state!=4)@(negedge clk);cancel_job(0,4);
 open_job(0,5);for(k=0;k<8;k=k+1)packet(0,5,k,512,1,0,-1);for(k=20;k<32;k=k+1)packet(0,5,k,512,1,0,-1);wait_full(5);if(restore_count[5]!=6144)$fatal(1,"stream new inverse basis after cancel");release_job(0,5);
 ready_mode=3;open_job(0,11);packet(0,11,11,512,1,0,-1);while(counts[11]<128)@(negedge clk);while(!sov)@(negedge clk);
 if(soo!=128||seen[11])$fatal(1,"stream partial source cancel target");cancel_job(0,11);ready_mode=1;
 if(aborts!=1||duplicate==0||badcrc==0||badlen==0||early_writes==0||concurrent_MAC<1000)$fatal(1,"stream ownership/concurrent coverage");
 // Full fixed8-bank burst,12missing sources per block. No extra bank/IP.
 ready_mode=0;burst_begin=cycle;
 for(j=0;j<8;j=j+1)begin open_job(j,12+j);for(k=12;k<32;k=k+1)packet(j,12+j,k,512,1,0,-1);end
 for(j=0;j<8;j=j+1)if(dut.bank_state[j]==0)$fatal(1,"stream full8queue was not occupied");
 ob=0;oj=32'hdeadbeef;ov=1;#1;if(ore)$fatal(1,"stream full8queue accepted ninth owner");@(negedge clk);ov=0;
 cb=7;cj=ids[19];cv=1;#1;if(sabort||!sov||soj!=ids[12])$fatal(1,"stream queued cancel revoked unrelated source");revoked[19]=1;@(negedge clk);cv=0;
 open_job(7,20);for(k=12;k<32;k=k+1)packet(7,20,k,512,1,0,-1);
 ready_mode=1;for(j=0;j<7;j=j+1)begin wait_full(12+j);if(restore_count[12+j]!=6144)$fatal(1,"stream full8queue recovery count");release_job(j,12+j);end
 wait_full(20);if(restore_count[20]!=6144)$fatal(1,"stream queued bank reuse source");release_job(7,20);
 burst_cycles=cycle-burst_begin;if(burst_cycles>12307692)$fatal(1,"stream full8queue service below1.3margin");
 $display("FEC_STREAM_QUEUE_PASS banks=8 recovered_blocks=8 queue_cancelled=1 new_owner_reused=1 burst_cycles=%0d",burst_cycles);
 // Hold one active early record, then hold its terminal while other LOADING
 // and QUEUED banks hit their absolute deadlines, independently of the core.
 ready_mode=0;status_mode=0;open_job(0,6);packet(0,6,0,512,1,0,-1);while(!sov)@(negedge clk);
 open_job(1,7);open_job(2,8);for(k=12;k<32;k=k+1)packet(2,8,k,512,1,0,-1);
 while(!tv)@(negedge clk);if(code!=4)$fatal(1,"stream blocked source watchdog");
 while(cycle-opened[1]-1<DEADLINE-513)@(negedge clk);
 packet(1,7,0,512,1,0,-1);
 while(dut.bank_state[1]!=5||dut.bank_state[2]!=5)@(negedge clk);
 if(dut.bank_mask[1]!=0||dut.bank_state[0]!=6||!tv||code!=4||expired!=2)$fatal(1,"stream loading/queued deadline or held status changed");
 status_mode=1;ready_mode=1;release_job(0,6);release_job(1,7);release_job(2,8);
 if(deadline_edges!=2||seen[7]||seen[8])$fatal(1,"stream expired bank emitted late source");
 // Dirty reset after a whole early source; reuse same bank with a high-bit
 // different job sharing the same16-bit inverse tag, and different contents.
 open_job(0,9);packet(0,9,3,512,1,0,-1);while(!seen[9][3])@(negedge clk);revoked[9]=1;
 rst=1;#1;if(sov||tv||ore||pre)$fatal(1,"stream reset publication fence");@(negedge clk);rst=0;repeat(2)@(negedge clk);
 open_job(0,10);for(k=0;k<20;k=k+1)packet(0,10,k,512,1,0,-1);wait_full(10);release_job(0,10);
 for(j=0;j<BANKS;j=j+1)if(dut.bank_state[j]!=0)$fatal(1,"stream bank not released");
 if(statuses!=17||counts[11]!=128||restore_count[1]!=6144||restore_count[5]!=6144)$fatal(1,"stream final conservation");
 $display("G0_FEC_STREAM_PASS banks=@BANKS@ terminal_jobs=%0d exact_bytes=%0d restored_bytes=%0d partial_revoked_bytes=128 early_first_source_cycles=%0d same_bank_early_writes=%0d concurrent_MAC=%0d held_checks=%0d deadline_edges=%0d max_success_cycles=%0d full8queue_cycles=%0d",statuses,checked,restored,early_delay,early_writes,concurrent_MAC,holds,deadline_edges,max_service,burst_cycles);$finish;
end
initial begin #2000000000;$fatal(1,"stream global timeout state%0d cycle%0d",dut.state,cycle);end
endmodule
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);bp=a.vivado_bin.resolve()
 before=disk_check('before-fec-stream');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  rng=np.random.default_rng(0x53545245414d);g=vandermonde_generator();assert np.array_equal(g,GENERATOR);rom=np.zeros((32,32),dtype=np.uint8);rom[:,:20]=g
  (out/'generator.hex').write_text(''.join(f'{int(v):02x}\n' for v in rom.ravel()));(out/'inverse.hex').write_text('00\n'+''.join(f'{inverse(v):02x}\n' for v in range(1,256)))
  src=[];coded=[]
  for _ in range(21):
   raw=rng.integers(0,256,(20,512),dtype=np.uint8);src.extend(raw.ravel());coded.extend(encode(raw).ravel())
  for name,values in [('source',src),('coded',coded)]: (out/(name+'.hex')).write_text(''.join(f'{int(v):02x}\n' for v in values))
  (out/'tb.sv').write_text(TB.replace('@BANKS@',str(a.banks)))
  for n in SOURCES:shutil.copyfile(project/'sim/g0'/n,out/n)
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  run('compile',[str(bp/'xvlog'),'--sv',*SOURCES,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','fec_stream_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'fec_stream_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_FEC_STREAM_PASS' in log and 'Fatal:' not in log,log[-5000:]
  result={'scope':__doc__,'banks':a.banks,'simulation':next(s for s in log.splitlines() if s.startswith('G0_FEC_STREAM_PASS')),'terminals':[s for s in log.splitlines() if s.startswith('FEC_STREAM_TERMINAL')],'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES},'routed_OOC':None,'g0_passed':False}
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  if not a.sim_only:
   before_ooc=disk_check('before-fec-stream-OOC');(out/'disk-before-ooc.json').write_text(json.dumps(before_ooc,indent=2)+'\n');assert before_ooc['disk']['available_bytes']>=5*GIB
   try:
    tcl='set_param general.maxThreads 4\n'+''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in SOURCES)+f'''synth_design -top fec_stream_recovery_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20 [get_ports clk]
set_input_delay 2 -clock processing [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_output_delay 2 -clock processing [get_ports -filter {{DIRECTION == OUT}}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force fec_stream.dcp
'''
    (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source','ooc.tcl','-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out)
   finally:(out/'disk-after-ooc.json').write_text(json.dumps(disk_check('after-fec-stream-OOC'),indent=2)+'\n')
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-fec-stream'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ('output','vivado-bin'):p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--banks',type=int,choices=[8],default=8);p.add_argument('--sim-only',action='store_true');main(p.parse_args())
