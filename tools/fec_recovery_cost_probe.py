#!/usr/bin/env python3
"""Integrated G0 PL FEC recovery/ownership/service measurement, not production.

Only encoded packet bytes and upstream CRC verdicts enter the DUT. Source
goldens connect exclusively to the monitor; inverse coefficients are computed
inside RTL and restored bytes are written/read through the real storage ports.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import numpy as np
from fec_oracle import inverse
from fec_inverse_cost_probe import vandermonde_generator, routed_measurements
from g0_packet_fec import GENERATOR, encode


def main(a):
    p=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    rng=np.random.default_rng(5122032)
    g=vandermonde_generator()
    if not np.array_equal(g,GENERATOR):raise ValueError('independent generator mismatch')
    rom=np.zeros((32,32),dtype=np.uint8);rom[:,:20]=g
    (out/'generator.hex').write_text(''.join(f'{int(x):02x}\n' for x in rom.ravel()))
    (out/'inverse.hex').write_text('00\n'+''.join(f'{inverse(x):02x}\n' for x in range(1,256)))
    jobs=[];commands=[]
    def job(indices,code=0,name=''):
        mask=sum(1<<i for i in indices)
        jobs.append({'id':len(jobs)+1,'indices':list(indices),'mask':mask,'code':code,'name':name,
                     'deliver':(mask&0xfffff) if code==1 else (0 if code>1 else 0xfffff),
                     'missing':((~mask)&0xfffff) if code==1 else (0xfffff if code>1 else 0)})
        return len(jobs)
    def cmd(op,bank=0,j=0,idx=0,length=0,crc=0,mutate=0,extra=0):
        commands.append([int(x) for x in (op,bank,j,idx,length,crc,mutate,extra)])
    def packet(bank,j,idx,length=512,crc=1,mutate=0,extra=0):cmd(1,bank,j,idx,length,crc,mutate,extra)
    def load(bank,j,shuffle=False):
        ids=jobs[j-1]['indices'].copy()
        if shuffle:rng.shuffle(ids)
        for idx in ids:packet(bank,j,idx)
    def complete(bank,j):cmd(3,bank,j);cmd(4,bank,j)
    # Cancelled jobs have no terminal receipt and are checked for stale output.
    j=job(range(12,32),-1,'reset-during-inversion');cmd(0,0,j);load(0,j);cmd(2,0,j);cmd(7,0,j,4)
    worst=job(range(12,32),0,'twelve-missing-with-concurrent-input')
    cmd(0,0,worst);load(0,worst,True);cmd(2,0,worst)
    # Sealed/active bank must reject even correctly tagged late packet writes.
    packet(0,worst,12,mutate=1)
    cmd(11,0,worst,6)
    dirty=job([i for i in range(32) if i!=3],0,'CRC-length-duplicate-stale-and-concurrent-bank')
    cmd(0,1,dirty);packet(1,dirty,3,crc=0);packet(1,dirty,4,length=511);packet(1,dirty,5,length=513)
    load(1,dirty,True);packet(1,dirty,0,mutate=1);packet(1,1,0,mutate=1)
    cmd(10,1,1);cmd(2,1,dirty);complete(0,worst);complete(1,dirty)
    # Observed real loss bitmap: bytes remain new, independently randomized test data.
    evidence=json.loads((p/'docs/evidence/g0-forward-packets-20261102.json').read_text())
    run=next(r for r in evidence['runs'] if r['directory'].endswith('packet-air-g12-long-r3'))
    trace=run['loss_trace'];first=trace['first_packet_seq'];bits=trace['bits']
    candidates=[]
    for block in range((first+31)//32,(first+len(bits))//32):
        ids=[i for i in range(32) if bits[block*32+i-first]=='1']
        if len(ids)>=20:candidates.append((len(ids),block,ids))
    _,actual_block,ids=min(candidates)
    real=job(ids,0,f"{run['directory']}:block{actual_block}")
    cmd(0,0,real);load(0,real,True);cmd(2,0,real);complete(0,real)
    for ids,name in [(list(range(13,32)),'thirteen-erasures-partial-source-delivery'),([], 'whole-block-loss')]:
        j=job(ids,1,name);cmd(0,0,j);load(0,j);cmd(2,0,j);complete(0,j)
    bad=job([0]+list(range(12,32)),3,'CRC-good-inconsistent-extra-parity')
    cmd(0,0,bad)
    for idx in jobs[bad-1]['indices']:packet(0,bad,idx,mutate=idx==31)
    cmd(2,0,bad);complete(0,bad)
    for target,name,count in [(4,'cancel-inverse',0),(8,'cancel-writeback',0),(13,'cancel-partial-source-output',100)]:
        j=job(range(12,32),-1,name);cmd(0,0,j);load(0,j);cmd(2,0,j);cmd(5,0,j,target,count)
        j=job(range(20),0,'reentry-after-'+name);cmd(0,0,j);load(0,j);cmd(2,0,j);complete(0,j)
    timed=job(range(20),4,'active-watchdog-during-output-backpressure')
    cmd(8,idx=0);cmd(0,0,timed);load(0,timed);cmd(2,0,timed);complete(0,timed);cmd(8,idx=1)
    # Reopen with a new owner WHILE the cancelled packet still has a tail.
    old=job([0],-1,'cancel-during-packet-tail')
    fresh=job(range(20),0,'reopen-before-old-packet-tail-finishes')
    cmd(0,0,old);packet(0,old,0,extra=fresh);load(0,fresh);cmd(2,0,fresh);complete(0,fresh)
    # Hold the first output, fill EVERY bank, reject a second OPEN, cancel
    # one queued job, and reuse that bank before draining the bounded queue.
    cmd(8,idx=0);queued=[]
    for bank in range(a.banks):
        j=job(range(20),0,f'bounded-queue-bank{bank}');queued.append(j)
        cmd(0,bank,j);load(bank,j);cmd(2,bank,j)
    cmd(9,0,queued[0]+100)
    cancelled=queued[-1];jobs[cancelled-1]['code']=-1
    cmd(5,a.banks-1,cancelled,-1)
    replacement=job(range(20),0,'queued-cancel-and-bank-reuse');queued[-1]=replacement
    cmd(0,a.banks-1,replacement);load(a.banks-1,replacement);cmd(2,a.banks-1,replacement)
    cmd(8,idx=1)
    for bank,j in enumerate(queued):complete(bank,j)
    (out/'commands.txt').write_text(''.join(' '.join(map(str,c))+'\n' for c in commands))
    (out/'jobs.json').write_text(json.dumps(jobs,indent=2)+'\n')
    with (out/'source.hex').open('w') as sf,(out/'coded.hex').open('w') as cf:
        sf.write('00\n'*10240);cf.write('00\n'*16384)
        for j in jobs:
            source=rng.integers(0,256,(20,512),dtype=np.uint8);coded=encode(source)
            sf.write(''.join(f'{int(x):02x}\n' for x in source.ravel()))
            cf.write(''.join(f'{int(x):02x}\n' for x in coded.ravel()))
    (out/'jobmeta.txt').write_text(''.join(f"{j['id']} {j['code']} {j['mask']:08x} {j['deliver']:05x} {j['missing']:05x}\n" for j in jobs))
    tb=r'''`timescale 1ns/1ps
module tb;
 localparam BANKS=@BANKS@,BW=$clog2(BANKS),JOBS=@JOBS@;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,ov=0,sv=0,rv=0,cv=0,pv=0,bv=0,bl=0,cg=1,sr=1,tr=1;
 reg [BW-1:0] ob=0,sb=0,rb=0,cb=0,pb=0;
 reg [31:0] oj=0,sj=0,rj=0,cj=0,pj=0;
 reg [4:0] pi=0;reg [7:0] bd=0;
 wire ore,sre,rre,pre,bre,source_v,source_last,recovered,tv;
 wire [7:0] data;wire [BW-1:0] source_b,tb;
 wire [31:0] source_j,tj,elapsed,committed,bad_crc,bad_len,stale,duplicate,cancelled;
 wire [4:0] source_i;wire [8:0] source_o;wire [2:0] code;wire [19:0] missing;
 fec_recovery_cost_probe #(.BANKS(BANKS)) dut(
  clk,rst,ov,ore,ob,oj,sv,sre,sb,sj,rv,rre,rb,rj,cv,cb,cj,
  pv,pre,pb,pj,pi,bv,bre,bd,bl,cg,
  source_v,sr,data,source_b,source_j,source_i,source_o,source_last,recovered,
  tv,tr,tb,tj,code,missing,elapsed,committed,bad_crc,bad_len,stale,duplicate,cancelled);
 reg [7:0] golden[0:(JOBS+1)*10240-1],coded[0:(JOBS+1)*16384-1];
 integer want_code[0:JOBS],counts[0:JOBS],restored_counts[0:JOBS],position[0:JOBS][0:19];
 reg [31:0] original[0:JOBS];reg [19:0] deliver[0:JOBS],want_missing[0:JOBS],seen[0:JOBS];
 reg ended[0:JOBS],revoked[0:JOBS];
 integer fd,mf,rf,n,op,bank,j,idx,len,crc,mut,extra,i,k,cycle=0,readymode=1;
 integer source_bytes=0,recovered_bytes=0,terminals=0,source_stalls=0,status_stalls=0;
 integer concurrent_writes=0,concurrent_MAC=0,atomic_hold=0,max_cycles=0;
 integer packet_count=0,open_rejections=0,seal_rejections=0;
 reg source_held=0,status_held=0;reg [63:0] held_source;
 reg [BW-1:0] held_source_bank,held_status_bank;
 reg [86:0] held_status;
 wire [63:0] source_tuple={source_j,source_i,source_o,recovered,source_last,data,8'b0};
 wire [86:0] status_tuple={tj,code,missing,elapsed};
 always @(negedge clk) begin
  sr=(readymode==2)||((readymode==1) && cycle%13!=0 && cycle%13!=1 && cycle%13!=2);
  tr=cycle%11!=0 && cycle%11!=1 && cycle%11!=2;
 end
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst) begin source_held=0;status_held=0;end
  else begin
   if(source_held && !dut.cancel_core && !dut.watchdog &&
      (!source_v || source_tuple!==held_source || source_b!==held_source_bank)) $fatal(1,"source hold changed");
   if(status_held && !dut.cancel_core &&
      (!tv || status_tuple!==held_status || tb!==held_status_bank)) $fatal(1,"status hold changed");
   source_held=source_v && !sr;status_held=tv && !tr;
   if(source_held) begin held_source=source_tuple;held_source_bank=source_b;source_stalls=source_stalls+1;end
   if(status_held) begin held_status=status_tuple;held_status_bank=tb;status_stalls=status_stalls+1;end
   if(dut.load_we && dut.state!=0) begin
    if(dut.rx_bank==dut.active_bank) $fatal(1,"write into active owner bank");
    concurrent_writes=concurrent_writes+1;
    if(dut.state==6 && dut.op_valid && dut.op_ready) concurrent_MAC=concurrent_MAC+1;
   end
   if(source_v && sr) begin
    if(source_j<1 || source_j>JOBS || revoked[source_j] || ended[source_j]) $fatal(1,"stale source owner");
    if(!deliver[source_j][source_i] || source_o!==position[source_j][source_i][8:0] || seen[source_j][source_i])
      $fatal(1,"source duplicate/order index job%0d symbol%0d offset%0d",source_j,source_i,source_o);
    if(data!==golden[source_j*10240+source_i*512+source_o])
      $fatal(1,"source byte mismatch job%0d symbol%0d offset%0d got%h expected%h",source_j,source_i,source_o,data,golden[source_j*10240+source_i*512+source_o]);
    if(source_last!==(source_o==511) || recovered!==!original[source_j][source_i]) $fatal(1,"source metadata");
    position[source_j][source_i]=position[source_j][source_i]+1;
    if(source_last) seen[source_j][source_i]=1;
    counts[source_j]=counts[source_j]+1;source_bytes=source_bytes+1;
    if(recovered) begin recovered_bytes=recovered_bytes+1;restored_counts[source_j]=restored_counts[source_j]+1;end
   end
   if(tv && tr) begin
    if(tj<1 || tj>JOBS || revoked[tj] || ended[tj]) $fatal(1,"stale terminal owner");
    if(code!==want_code[tj][2:0] || missing!==want_missing[tj] || seen[tj]!==deliver[tj])
      $fatal(1,"terminal mismatch job%0d gotcode%0d wanted%0d missing%h wanted%h seen%h wanted%h",tj,code,want_code[tj],missing,want_missing[tj],seen[tj],deliver[tj]);
    if(code!=4 && elapsed>=1000000) $fatal(1,"job exceeded watchdog");
    if(code==4 && elapsed!=1000000) $fatal(1,"watchdog cycle mismatch");
    if(code==0 && elapsed>max_cycles) max_cycles=elapsed;
    ended[tj]=1;terminals=terminals+1;
    $fwrite(rf,"%0d %0d %0d %0d %05h %0d\n",tj,code,elapsed,counts[tj],missing,restored_counts[tj]);
   end
  end
 end
 task open_job(input integer b,input integer id);
  begin
   @(negedge clk);ob=b;oj=id;ov=1;#1;
   while(!ore) @(negedge clk);
   @(posedge clk);if(!ore) $fatal(1,"open handshake");
   @(negedge clk);ov=0;
  end
 endtask
 task cancel_job(input integer b,input integer id);
  begin
   @(negedge clk);cb=b;cj=id;cv=1;revoked[id]=1;
   @(negedge clk);cv=0;
  end
 endtask
 task packet(input integer b,input integer id,input integer symbol,input integer length,
             input integer crc_ok,input integer mutation,input integer replacement);
  integer x;
  begin
   @(negedge clk);pb=b;pj=id;pi=symbol;pv=1;#1;
   while(!pre) @(negedge clk);
   @(posedge clk);if(!pre) $fatal(1,"packet handshake");
   @(negedge clk);pv=0;
   for(x=0;x<length;x=x+1) begin
    if(x==200 && replacement!=0) begin cancel_job(b,id);open_job(b,replacement);end
    if(x%37==0) @(negedge clk);
    bd=coded[id*16384+symbol*512+(x%512)] ^ ((mutation && x==77)?8'h5a:8'h00);
    bv=1;bl=x==length-1;cg=crc_ok;
    @(posedge clk);if(!bre) $fatal(1,"byte handshake");
    @(negedge clk);bv=0;bl=0;
   end
   packet_count=packet_count+1;
  end
 endtask
 initial begin
  $readmemh("source.hex",golden);$readmemh("coded.hex",coded);
  for(i=0;i<=JOBS;i=i+1) begin
   ended[i]=0;revoked[i]=0;seen[i]=0;counts[i]=0;restored_counts[i]=0;
   for(k=0;k<20;k=k+1) position[i][k]=0;
  end
  mf=$fopen("jobmeta.txt","r");
  for(i=1;i<=JOBS;i=i+1) begin
   n=$fscanf(mf,"%d %d %h %h %h\n",j,want_code[i],original[i],deliver[i],want_missing[i]);
   if(n!=5 || j!=i) $fatal(1,"metadata parse");
  end
  rf=$fopen("metrics.txt","w");fd=$fopen("commands.txt","r");
  repeat(4) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%d %d %d %d %d %d %d %d\n",op,bank,j,idx,len,crc,mut,extra);
   if(n!=8) $fatal(1,"command parse");
   case(op)
    0:open_job(bank,j);
    1:packet(bank,j,idx,len,crc,mut,extra);
    2:begin
     @(negedge clk);sb=bank;sj=j;sv=1;#1;
     while(!sre) @(negedge clk);
     @(posedge clk);if(!sre) $fatal(1,"seal handshake");
     @(negedge clk);sv=0;
    end
    3:while(!ended[j]) @(negedge clk);
    4:begin
     @(negedge clk);rb=bank;rj=j;rv=1;#1;
     while(!rre) @(negedge clk);
     @(posedge clk);if(!rre) $fatal(1,"release handshake");
     @(negedge clk);rv=0;
    end
    5:begin
     if(idx>=0) while(dut.state!=idx || dut.active_job!=j || counts[j]<len) @(negedge clk);
     cancel_job(bank,j);
    end
    7:begin
     while(dut.state!=idx || dut.active_job!=j) @(negedge clk);
     @(negedge clk);rst=1;revoked[j]=1;
     repeat(3) @(negedge clk);rst=0;
    end
    8:readymode=idx;
    9:begin
     @(negedge clk);ob=bank;oj=j;ov=1;
     repeat(5) begin @(posedge clk);if(ore) $fatal(1,"occupied bank reopened");@(negedge clk);end
     ov=0;open_rejections=open_rejections+1;
    end
    10:begin
     @(negedge clk);sb=bank;sj=j;sv=1;
     repeat(5) begin @(posedge clk);if(sre) $fatal(1,"stale seal accepted");@(negedge clk);end
     sv=0;seal_rejections=seal_rejections+1;
    end
    11:while(dut.state!=idx || dut.active_job!=j) @(negedge clk);
    default:$fatal(1,"unknown command");
   endcase
  end
  repeat(10) @(negedge clk);
  for(i=1;i<=JOBS;i=i+1) begin
   if(want_code[i]<0 && (!revoked[i] || ended[i])) $fatal(1,"cancel accounting job%0d",i);
   if(want_code[i]>=0 && (!ended[i] || revoked[i])) $fatal(1,"completion accounting job%0d",i);
  end
  for(i=0;i<BANKS;i=i+1) if(dut.bank_state[i]!=0) $fatal(1,"bank not released");
  if(source_stalls==0 || status_stalls==0 || concurrent_writes==0 || concurrent_MAC<1000 ||
     bad_crc!=1 || bad_len!=2 || duplicate!=1 || stale<2 || cancelled!=5 ||
     open_rejections!=1 || seal_rejections!=1) $fatal(1,"coverage counters crc%0d len%0d duplicate%0d stale%0d cancelled%0d",bad_crc,bad_len,duplicate,stale,cancelled);
  $fclose(rf);
  $display("G0_FEC_RECOVERY_PASS bytes=%0d restored=%0d terminals=%0d source_stalls=%0d status_stalls=%0d concurrent_writes=%0d concurrent_MAC=%0d max_cycles=%0d packets=%0d committed=%0d cancelled=%0d",
   source_bytes,recovered_bytes,terminals,source_stalls,status_stalls,concurrent_writes,concurrent_MAC,max_cycles,packet_count,committed,cancelled);
  $finish;
 end
 initial begin #1000000000;$fatal(1,"global timeout");end
endmodule
'''.replace('@BANKS@',str(a.banks)).replace('@JOBS@',str(len(jobs)))
    (out/'tb.sv').write_text(tb)
    runs=[]
    def run(name,argv):
        runs.append(argv);(out/'commands.json').write_text(json.dumps(runs,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:
            subprocess.run(argv,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    bp=a.vivado_bin.resolve();sources=[]
    for name in ['fec_inverse_cost_probe.sv','fec_memory_cost_probe.sv','fec_recovery_cost_probe.sv']:
        path=out/name;shutil.copyfile(p/'sim/g0'/name,path);sources.append(str(path))
    run('compile',[str(bp/'xvlog'),'--sv',*sources,str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','fec_recovery_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'fec_recovery_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_FEC_RECOVERY_PASS' not in sim or 'Fatal:' in sim:raise ValueError('integrated data/control failed')
    measured={k:int(v) for k,v in re.findall(r'(bytes|restored|terminals|source_stalls|status_stalls|concurrent_writes|concurrent_MAC|max_cycles|packets|committed|cancelled)=(\d+)',sim)}
    metrics=[]
    for line in (out/'metrics.txt').read_text().splitlines():
        j,code,cycles,count,missing,restored=line.split()
        metrics.append({'job':int(j),'name':jobs[int(j)-1]['name'],'code':int(code),'cycles':int(cycles),
                        'source_bytes':int(count),'restored_source_bytes':int(restored),
                        'missing_mask':missing,'seconds':int(cycles)/50000000})
    if any(m['code']==0 and m['cycles']>=50000000*0.04/1.3 for m in metrics):
        raise ValueError('finite successful job lacks 1.3 service margin versus40ms block period')
    # The monitor must actually reject corrupt data, not merely match counts.
    fault=tb.replace('module tb;','module fault_tb;').replace('wire [7:0] data;',
        "wire [7:0] raw_data;wire [7:0] data=raw_data^8'h01;")
    fault=fault.replace('source_v,sr,data,source_b','source_v,sr,raw_data,source_b')
    fault=fault.replace('"metrics.txt"','"fault-metrics.txt"')
    (out/'fault_tb.sv').write_text(fault)
    run('fault-compile',[str(bp/'xvlog'),'--sv',str(out/'fault_tb.sv')])
    run('fault-elaborate',[str(bp/'xelab'),'fault_tb','-s','fec_recovery_fault_tb','-mt','off'])
    run('fault-simulate',[str(bp/'xsim'),'fec_recovery_fault_tb','-runall'])
    fl=(out/'fault-simulate.log').read_text()
    if 'Fatal: source byte mismatch' not in fl or 'G0_FEC_RECOVERY_PASS' in fl:raise ValueError('data corruption not rejected')
    (out/'ooc.tcl').write_text('set_param general.maxThreads 4\n'+''.join(f'read_verilog -sv {{{s}}}\n' for s in sources)+f'''
synth_design -top fec_recovery_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic BANKS={a.banks}
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_output_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == OUT}}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
report_timing -max_paths 10 -file paths.rpt
write_checkpoint -force fec_recovery_cost_probe.dcp
''')
    run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
    receipt={'scope':'G0 integrated candidate bank/inverse/GF-MAC/writeback/parity-check/output cost, not production FEC/DMA',
        'banks':a.banks,'clock_hz':50000000,'symbol_bytes':512,'k':20,'n':32,
        'measured':measured,'jobs':jobs,'terminal_metrics':metrics,'routed_OOC':routed_measurements(out),
        'source_bytes_from_completed_jobs':sum(m['source_bytes'] for m in metrics),
        'restored_source_bytes_from_completed_jobs':sum(m['restored_source_bytes'] for m in metrics),
        'cancelled_prefix_bytes_checked_but_not_delivered':measured['bytes']-sum(m['source_bytes'] for m in metrics),
        'source_goldens_only_connected_to_monitor':True,'coefficients_generated_inside_RTL':True,
        'restored_bytes_written_and_read_via_real_RAM':True,'single_bit_output_corruption_rejected':True,
        'upstream_link_CRC_calculated_here':False,'active_watchdog_cycles':1000000,
        'production_early_source_forwarding_queued_expiry_atomic_DMA_implemented':False,
        'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({k:v for k,v in receipt.items() if k not in ('jobs','terminal_metrics')}))


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--banks',type=int,choices=[8,16],default=8)
    ap.add_argument('--vivado-bin',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    main(ap.parse_args())
