#!/usr/bin/env python3
"""Measure a bounded RS20/32 PL coefficient engine, NOT a full FEC receiver.

RTL performs Gauss-Jordan; expected coefficients use direct Lagrange products.
ROM is generated independently by Vandermonde/systematic Gaussian reduction.
Successful RTL output is also applied to full 512-byte symbols after simulation.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import numpy as np
from fec_oracle import multiply, inverse
from fec_memory_cost_probe import recovery_coefficients
from g0_packet_fec import GENERATOR, TABLE, encode, combine


def vandermonde_generator():
    v=np.ones((32,20),dtype=np.uint8)
    for r in range(32):
        for c in range(1,20):v[r,c]=multiply(int(v[r,c-1]),r+1)
    a=np.concatenate((v[:20].copy(),np.eye(20,dtype=np.uint8)),axis=1)
    for c in range(20):
        p=next(r for r in range(c,20) if a[r,c])
        a[[c,p]]=a[[p,c]];a[c]=TABLE[inverse(int(a[c,c])),a[c]]
        for r in range(20):
            if r!=c:a[r]^=TABLE[a[r,c],a[c]]
    return np.bitwise_xor.reduce(TABLE[v[:,:,None],a[None,:,20:]],axis=1)


def routed_measurements(out):
    utilization=(out/'utilization.rpt').read_text()
    timing=(out/'timing.rpt').read_text();drc=(out/'drc.rpt').read_text()
    def used(label):
        match=re.search(r'\|\s*'+re.escape(label)+r'\s*\|\s*(\d+)\s*\|',utilization)
        if not match:raise ValueError('missing utilization row '+label)
        return int(match[1])
    result={'part':'xc7z020clg484-1','LUT':used('Slice LUTs'),'FF':used('Slice Registers'),
            'LUT_logic':used('LUT as Logic'),'LUT_memory':used('LUT as Memory'),
            'RAMB36':used('RAMB36/FIFO*'),'RAMB18':used('RAMB18'),'DSP':used('DSPs')}
    result['BRAM36_equivalent']=result['RAMB36']+result['RAMB18']/2
    for kind,slack,total in [('Setup','WNS_ns','TNS_ns'),('Hold','WHS_ns','THS_ns')]:
        match=re.search(kind+r'\s*:\s*(\d+)\s+Failing Endpoints,\s+Worst Slack\s+([-.\d]+)ns,\s+Total Violation\s+([-.\d]+)ns',timing)
        if not match:raise ValueError('missing timing result')
        result[slack]=float(match[2]);result[total]=float(match[3])
        if int(match[1]) or result[slack]<0 or result[total]!=0:raise ValueError('timing failed')
    for check in ('unconstrained_internal_endpoints','no_input_delay','no_output_delay','no_clock'):
        match=re.search(r'checking '+check+r' \((\d+)\)',timing)
        if not match or int(match[1]):raise ValueError('incomplete timing constraints: '+check)
        result[check]=int(match[1])
    if re.search(r'\|\s*(Critical Warning|Error)\s*\|',drc):raise ValueError('severe DRC')
    if 'Infeasible attribute ram_style' in (out/'ooc.log').read_text():
        raise ValueError('requested scratch BRAM inference failed')
    result['DRC_warning_ids']=sorted(set(re.findall(r'^([A-Z0-9-]+)#\d+ Warning',drc,re.M)))
    result['severe_DRC']=0;result['full_design_IO_CDC_and_timing_verified']=False
    return result


def main(a):
    project=Path(__file__).resolve().parents[1]
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    rng=np.random.default_rng(20320512)
    g=vandermonde_generator()
    if not np.array_equal(g,GENERATOR):raise ValueError('independent generator disagreement')
    rom=np.zeros((32,32),dtype=np.uint8);rom[:,:20]=g
    (out/'generator.hex').write_text(''.join(f'{int(x):02x}\n' for x in rom.ravel()))
    (out/'inverse.hex').write_text('00\n'+''.join(f'{inverse(x):02x}\n' for x in range(1,256)))
    jobs=[]
    def add(ids,mode=0,target=0,code=0,origin='synthetic'):
        jobs.append(dict(id=len(jobs)+1,indices=[int(i) for i in ids],mode=mode,target=target,code=code,origin=origin))
    add(range(20),origin='identity')
    add(range(19,-1,-1),origin='systematic-reversed-pivot-swaps')
    add(range(12,32),origin='twelve-missing-source-symbols')
    add(rng.permutation(np.arange(12,32)),origin='twelve-missing-shuffled')
    for _ in range(32):add(rng.choice(32,20,replace=False))
    evidence=json.loads((project/'docs/evidence/g0-forward-packets-20261102.json').read_text())
    for run in evidence['runs']:
        if 'long' not in run['directory']:continue
        trace=run['loss_trace'];first=trace['first_packet_seq'];bits=trace['bits']
        for block in range((first+31)//32,(first+len(bits))//32):
            selected=[i for i in range(32) if bits[block*32+i-first]=='1']
            if len(selected)>=20:add(selected[:20],origin=f"{run['directory']}:block{block}")
    add([0]*20,code=1,origin='duplicate-indices')
    add(list(range(19))+[63],code=1,origin='out-of-range-final-index')
    add([32]+list(range(1,20)),code=1,origin='out-of-range-first-index')
    add(list(range(19))+[0],code=1,origin='duplicate-final-index')
    # Fault injection: corrupt scratch AFTER all ROM initialization, rather
    # than weaken the input-index validation to admit a duplicate matrix.
    add(range(12,32),mode=1,code=2,origin='zero-pivot-column-scratch-fault')
    add(range(12,32),mode=2,code=2,origin='duplicate-row-scratch-fault')
    # Both reset and cancel exercised with dirty RAM and at every phase.
    for mode in (3,4):
        for state in (1,2,4,9,12,14,17,19,20):
            add(range(19,-1,-1) if state==9 else range(12,32),mode=mode,target=state,
                origin=('cancel' if mode==3 else 'reset')+f'-state{state}')
    add(range(19,-1,-1),origin='restart-after-dirty-abort')
    with (out/'jobs.txt').open('w') as f, (out/'expected.txt').open('w') as oracle:
        for job in jobs:
            f.write(' '.join(map(str,[job['id'],job['mode'],job['target'],job['code']]+job['indices']))+'\n')
            if job['mode'] in (0,) and job['code']==0:
                for row in range(20):
                    for col,value in enumerate(recovery_coefficients(row,job['indices'])):
                        oracle.write(f"{job['id']} {row} {col} {value:02x}\n")
    (out/'jobs.json').write_text(json.dumps(jobs,indent=2)+'\n')
    good=sum(j['mode']==0 and j['code']==0 for j in jobs)
    rejected=sum(j['mode']<3 and j['code']!=0 for j in jobs)
    tb='''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,sv=0,iv=0,cr=0,dr=0;
 reg [15:0] jid=0;reg [5:0] idx=0;
 wire sr,ir,cv,last,dv;wire [7:0] data;wire [4:0] row,col;
 wire [1:0] code;wire [15:0] active;wire [31:0] elapsed;
 fec_inverse_cost_probe dut(clk,rst,cancel,sv,sr,jid,iv,ir,idx,
                           cv,cr,data,row,col,last,dv,dr,code,active,elapsed);
 integer jf,ef,of,mf,n,id,mode,target,expected_code,i,r,c,v,eid,er,ec;
 integer ids[0:19];integer cycles=0,checked=0,done_count=0,aborted=0;
 integer init_cycle=-1,first_cycle=-1,out_count=0,stalls=0,done_stalls=0;
 integer swap_rows=0,zero_factors=0,search_zeros=0,state_seen[0:20];
 integer no_stall_intervals=0,last_output_cycle=-1;
 integer monitoring=0,ejob=-1,min_compute=100000,max_compute=0;
 reg [7:0] held_data;reg [4:0] held_row,held_col;reg held_last;
 reg [15:0] held_job;reg [1:0] held_code;reg [31:0] held_elapsed;
 reg stalled=0,done_stalled=0;
 always @(negedge clk) begin
  cr=monitoring && ((id==1 || id==3) || ((cycles%11!=0) && (cycles%11!=1) && (cycles%11!=2)));
  dr=0; // Driver explicitly accepts each terminal receipt after checking it.
 end
 always @(posedge clk) begin
  cycles=cycles+1;
  if(!rst && !cancel) begin
   state_seen[dut.state]=state_seen[dut.state]+1;
   if(dut.state==9 && dut.col==0) swap_rows=swap_rows+1;
   if(dut.state==15 && dut.memory_q==0) zero_factors=zero_factors+1;
   if(dut.state==4 && dut.memory_q==0) search_zeros=search_zeros+1;
   if(monitoring) begin
    if(dut.state==2 && init_cycle<0) init_cycle=cycles;
    if(cv && first_cycle<0) begin
     first_cycle=cycles;
     if(init_cycle<0 || first_cycle-init_cycle>40000) $fatal(1,"compute bound");
     if(first_cycle-init_cycle<min_compute) min_compute=first_cycle-init_cycle;
     if(first_cycle-init_cycle>max_compute) max_compute=first_cycle-init_cycle;
    end
    if(stalled && (!cv || data!==held_data || row!==held_row || col!==held_col ||
                   last!==held_last || active!==held_job)) $fatal(1,"coefficient stall stability");
    if(done_stalled && (!dv || code!==held_code || elapsed!==held_elapsed || active!==held_job))
      $fatal(1,"done stall stability");
    stalled=cv && !cr;done_stalled=dv && !dr;
    if(stalled) begin
     held_data=data;held_row=row;held_col=col;held_last=last;held_job=active;stalls=stalls+1;
    end
    if(done_stalled) begin held_code=code;held_elapsed=elapsed;held_job=active;done_stalls=done_stalls+1;end
    if(cv && cr) begin
     if(expected_code!=0) $fatal(1,"failed job emitted coefficients");
     n=$fscanf(ef,"%d %d %d %h\\n",eid,er,ec,v);
     if(n!=4 || active!==eid[15:0] || row!==er[4:0] || col!==ec[4:0] || data!==v[7:0])
       $fatal(1,"coefficient mismatch job%0d at %0d,%0d got%h expected%h",active,row,col,data,v);
     if(last!==(out_count==399)) $fatal(1,"last mismatch");
     if((id==1 || id==3) && out_count!=0) begin
      if(cycles-last_output_cycle!=2) $fatal(1,"coefficient drain service mismatch");
      no_stall_intervals=no_stall_intervals+1;
     end
     last_output_cycle=cycles;
     $fwrite(of,"%0d %0d %0d %02h\\n",active,row,col,data);
     out_count=out_count+1;checked=checked+1;
    end
   end else begin stalled=0;done_stalled=0;end
  end else begin stalled=0;done_stalled=0;end
 end
 task revoke;
  begin
   if(mode==3) cancel=1;else rst=1;
   #1;if(cv || dv || ir || sr) $fatal(1,"revocation handshake not gated");
   @(negedge clk);cancel=0;rst=0;iv=0;sv=0;
   #1;if(!sr || cv || dv) $fatal(1,"revocation did not return idle");
   repeat(5) begin @(negedge clk);if(cv || dv) $fatal(1,"stale completion");end
   aborted=aborted+1;
  end
 endtask
 initial begin
  for(i=0;i<=20;i=i+1) state_seen[i]=0;
  jf=$fopen("jobs.txt","r");ef=$fopen("expected.txt","r");
  of=$fopen("observed.txt","w");mf=$fopen("metrics.txt","w");
  repeat(4) @(negedge clk);rst=0;#1;
  while(!$feof(jf)) begin
   n=$fscanf(jf,"%d %d %d %d",id,mode,target,expected_code);
   if(n!=4) $fatal(1,"job parse");
   for(i=0;i<20;i=i+1) begin n=$fscanf(jf,"%d",ids[i]);if(n!=1) $fatal(1,"index parse");end
   n=$fscanf(jf,"\\n");
   monitoring=mode<3;init_cycle=-1;first_cycle=-1;out_count=0;
   if(!sr) $fatal(1,"start not ready");
   @(negedge clk);jid=id;sv=1;
   @(posedge clk);if(!sr) $fatal(1,"start handshake");
   @(negedge clk);sv=0;
   if(mode>=3 && target==1) begin
    idx=ids[0];iv=1;@(posedge clk);if(!ir) $fatal(1,"index handshake");
    @(negedge clk);iv=0;revoke;
   end else begin
    for(i=0;i<20;i=i+1) begin
     repeat((i+id)%3) @(negedge clk);
     idx=ids[i];iv=1;
     @(posedge clk);if(!ir) $fatal(1,"index handshake");
     @(negedge clk);iv=0;
    end
    if(mode==1 || mode==2) begin
     while(dut.state!=3) @(negedge clk);
     if(mode==1) for(r=0;r<20;r=r+1) dut.scratch[r*40]=0;
     else for(c=0;c<20;c=c+1) dut.scratch[19*40+c]=dut.scratch[18*40+c];
    end
    if(mode>=3) begin
     // To reach DONE, drain coefficients without applying the successful-job oracle.
     if(target==20) begin
      while(!dv) begin @(negedge clk);#1;cr=1;end
     end else while(dut.state!=target) @(negedge clk);
     revoke;
    end else begin
     while(!dv) @(negedge clk);
     if(code!==expected_code[1:0] || active!==id[15:0] ||
        out_count!=(expected_code==0?400:0)) $fatal(1,"terminal receipt mismatch job%0d",id);
     $fwrite(mf,"%0d %0d %0d %0d %0d\\n",id,code,elapsed,first_cycle-init_cycle,out_count);
     repeat(5) @(negedge clk);
     // Set after the negedge ready driver, sample the actual rising-edge handshake.
     #1;dr=1;@(posedge clk);if(!dv) $fatal(1,"done handshake");
     @(negedge clk);monitoring=0;done_stalled=0;done_count=done_count+1;
    end
   end
  end
  n=$fscanf(ef,"%d %d %d %h",eid,er,ec,v);if(n==4) $fatal(1,"unused oracle");
  for(i=0;i<=20;i=i+1) if(state_seen[i]==0) $fatal(1,"uncovered state%0d",i);
  if(checked!=GOOD*400 || done_count!=GOOD+REJECTED || aborted!=18 ||
     stalls==0 || done_stalls==0 || swap_rows==0 || zero_factors==0 || search_zeros==0 || no_stall_intervals!=798)
    $fatal(1,"coverage mismatch");
  $fclose(of);$fclose(mf);
  $display("G0_FEC_INVERSE_PASS coefficients=%0d done=%0d aborted=%0d stalls=%0d done_stalls=%0d swaps=%0d zero_factors=%0d search_zeros=%0d min_compute=%0d max_compute=%0d no_stall_intervals=%0d",
            checked,done_count,aborted,stalls,done_stalls,swap_rows,zero_factors,search_zeros,min_compute,max_compute,no_stall_intervals);
  $finish;
 end
 initial begin #200000000;$fatal(1,"global timeout");end
endmodule
'''.replace('GOOD',str(good)).replace('REJECTED',str(rejected))
    (out/'tb.sv').write_text(tb)
    executed=[]
    def run(name,argv):
        executed.append(argv);(out/'commands.json').write_text(json.dumps(executed,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:
            subprocess.run(argv,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    bp=a.vivado_bin.resolve();rtl=out/'fec_inverse_cost_probe.sv'
    shutil.copyfile(project/'sim/g0/fec_inverse_cost_probe.sv',rtl)
    run('compile',[str(bp/'xvlog'),'--sv',str(rtl),str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','fec_inverse_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'fec_inverse_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_FEC_INVERSE_PASS' not in sim or 'Fatal:' in sim:raise ValueError('inverse simulation failed')
    measured={k:int(v) for k,v in re.findall(r'(coefficients|done|aborted|stalls|done_stalls|swaps|zero_factors|search_zeros|min_compute|max_compute|no_stall_intervals)=(\d+)',sim)}
    observed=np.loadtxt(out/'observed.txt',dtype=str)
    actual_bytes=0
    for job in jobs:
        if job['mode']!=0 or job['code']!=0:continue
        rows=observed[observed[:,0].astype(int)==job['id']]
        if len(rows)!=400:raise ValueError('output coefficient count')
        matrix=np.array([int(s,16) for s in rows[:,3]],dtype=np.uint8).reshape(20,20)
        if not np.array_equal(combine(matrix,g[job['indices']]),np.eye(20,dtype=np.uint8)):
            raise ValueError('observed inverse times selected generator is not identity')
        source=rng.integers(0,256,(20,512),dtype=np.uint8)
        recovered=combine(matrix,encode(source)[job['indices']])
        if not np.array_equal(source,recovered):raise ValueError('full symbol application mismatch')
        actual_bytes+=source.size
    # Corrupt observed coefficient, not the oracle, and require first-word rejection.
    fault=tb.replace('module tb;','module fault_tb;').replace(
        'wire [7:0] data;',"wire [7:0] raw_data;wire [7:0] data=raw_data^8'h01;").replace(
        'cv,cr,data,row,col,last,dv','cv,cr,raw_data,row,col,last,dv')
    # Keep successful observations intact; failed control writes separate artifacts.
    fault=fault.replace('"observed.txt"','"fault-observed.txt"').replace('"metrics.txt"','"fault-metrics.txt"')
    (out/'fault_tb.sv').write_text(fault)
    run('fault-compile',[str(bp/'xvlog'),'--sv',str(out/'fault_tb.sv')])
    run('fault-elaborate',[str(bp/'xelab'),'fault_tb','-s','fec_inverse_fault_tb','-mt','off'])
    run('fault-simulate',[str(bp/'xsim'),'fec_inverse_fault_tb','-runall'])
    fl=(out/'fault-simulate.log').read_text()
    if 'Fatal: coefficient mismatch' not in fl or 'G0_FEC_INVERSE_PASS' in fl:
        raise ValueError('corruption not rejected')
    (out/'ooc.tcl').write_text(f'''set_param general.maxThreads 4
read_verilog -sv {{{rtl}}}
synth_design -top fec_inverse_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == IN && NAME != clk}}]
set_output_delay 2.0 -clock processing [get_ports -filter {{DIRECTION == OUT}}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
report_timing -max_paths 10 -file paths.rpt
set memory_file [open bram-properties.txt w]
foreach c [get_cells -hier -filter {{REF_NAME =~ RAMB*}}] {{
 puts $memory_file "$c [get_property REF_NAME $c] [get_property READ_WIDTH_A $c] [get_property WRITE_WIDTH_A $c] [get_property WRITE_MODE_A $c]"
}}
close $memory_file
write_checkpoint -force fec_inverse_cost_probe.dcp
''')
    run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
    receipt={'scope':'G0 isolated RS20/32 coefficient generator; not full FEC scheduler/decoder',
        'clock_hz':50000000,'matrix_coefficients':400,'scratch_bytes':800,'pivot_cache_bytes':40,
        'generator_ROM_logical_bytes':640,'inverse_ROM_bytes':256,'input_basis_indices':20,
        'jobs':len(jobs),'successful_jobs':good,'rejected_jobs':rejected,
        'actual_RF_loss_selection_jobs':sum('packet-air-' in j['origin'] for j in jobs),
        'oracle':'direct Lagrange coefficient products; independent of RTL Gauss-Jordan',
        'measured':measured,'observed_coefficients_applied_to_source_bytes_on_host':actual_bytes,
        'scratch_singular_faults_rejected':2,'reset_and_cancel_phase_tests':18,
        'single_bit_result_corruption_rejected':True,'conservative_compute_bound_cycles':40000,
        'no_stall_output_drain_cycles':800,'routed_OOC':routed_measurements(out),
        'full_FEC_bank_scheduling_or_recovery_implemented':False,
        'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    main(p.parse_args())
