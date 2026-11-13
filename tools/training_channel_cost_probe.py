#!/usr/bin/env python3
"""Finite training CFO/H RTL oracle, control/service tests, and 50 MHz OOC.

Expected values come from public signs and input vectors only, before simulation.
The independent floating atan2/uniform OLS comparison is separate from bit gold.
No payload, expected CFO or H is connected to the DUT.
"""
import argparse
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import zlib
import numpy as np
from fec_inverse_cost_probe import routed_measurements
from g0_packet_probe import public_prefix
from g0_sounder import TRAIN_START
from g0_fixed_training_model import (estimate_training, TrainingInvalid,
    training_signs, nearest_div, verify_normalization)

MASK=(1<<32)-1


def pack(values,bits):
    return sum((int(v)&((1<<bits)-1))<<(bits*i) for i,v in enumerate(values))


def independent_math(lanes,signs,tx,report):
    """Float atan2/centered OLS, not the CORDIC recurrence or LUT folding."""
    raw=np.asarray(lanes,dtype=float).reshape(448,2,2)
    r=raw[:,:,0]+1j*raw[:,:,1]
    q=signs[:,0]+1j*signs[:,1]
    d=r*np.conj(q[:,None])
    blocks=d.reshape(28,16,2).sum(axis=1)
    selected=int(np.argmax(np.sum(abs(r)**2,axis=0)))
    if selected!=report['selected_rx']:raise AssertionError('independent energy selection')
    exact=np.unwrap(np.angle(blocks[:,selected]))/(2*np.pi)
    centered=np.arange(28,dtype=float)-13.5
    step=float(np.dot(centered,exact)/(16*np.dot(centered,centered))*(1<<32))
    error=abs(report['symbol_step_s32']-step)
    step_bound=392*32/58464+.5
    if error>step_bound+1e-5:raise AssertionError(('independent OLS bound',error))
    positions=np.arange(448)+(160 if tx==0 else 672)
    h=np.mean(d*np.exp(-2j*np.pi*step/(1<<32)*positions[:,None]),axis=0)*16/np.sqrt(2)
    expected=np.array([h[0].real,h[0].imag,h[1].real,h[1].imag])
    herror=float(np.max(abs(np.array(report['H_raw_s26'])-expected)))
    # Rotation bin +/-half-bin, independent Q16 sin/cos coefficient errors,
    # slope bound accumulated to furthest training symbol, F4 sample rounding,
    # Q40 normalization constant error, then final output rounding.
    dmax=float(np.max(abs(d)))
    phase_error=np.pi/4096+2*np.pi*max(positions)*step_bound/(1<<32)
    coeff_error=np.sqrt(2)/(2*65536)
    exact_c=1/(448*np.sqrt(2))
    quant_c=1735428857/(1<<40)
    hbound=dmax*(phase_error+coeff_error)*16/np.sqrt(2)+448*.5*quant_c+448*(dmax*16+.5)*abs(quant_c-exact_c)+.5
    if herror>hbound+1e-5:raise AssertionError(('independent H bound',herror,hbound))
    return dict(phase_step_error_LSB=error,phase_step_bound_LSB=step_bound,
                H_error_scale64_LSB=herror,H_bound_scale64_LSB=hbound)


def make_jobs(fixtures):
    public=public_prefix();signs=[training_signs(public,tx)[1] for tx in range(2)]
    jobs=[];rng=np.random.default_rng(267981)
    def from_despread(d,tx):
        d=np.asarray(d,dtype=np.int64).reshape(448,4)
        if np.any(d%2):raise ValueError('integer inversion requires even despread')
        s=signs[tx];a=np.empty_like(d)
        for rx in (0,1):
            u,v=d[:,2*rx],d[:,2*rx+1]
            a[:,2*rx]=(u*s[:,0]-v*s[:,1])//2
            a[:,2*rx+1]=(u*s[:,1]+v*s[:,0])//2
        return a
    def constant(u,v=0,weak=0,tx=0):
        return from_despread(np.tile([u,v,weak,0],(448,1)),tx)
    def tone(tx,step,phase=.0,amp=(12000,3000)):
        k=np.arange(448)+(160 if tx==0 else 672)
        phase=phase+step*k
        d=np.zeros((448,4),dtype=np.int64)
        for rx in (0,1):
            p=2*np.pi*(phase+rx*.17)
            d[:,2*rx]=2*np.rint(amp[rx]*np.cos(p)/2).astype(np.int64)
            d[:,2*rx+1]=2*np.rint(amp[rx]*np.sin(p)/2).astype(np.int64)
        return from_despread(d,tx)
    def add(lanes,tx=0,name='synthetic',cadence=32,mode=0,target=0,inject=0,bad_at=-1,source=None):
        lanes=np.asarray(lanes,dtype=np.int64)
        if lanes.shape!=(448,4):raise ValueError('fixture dimensions')
        try:
            report=estimate_training(lanes,signs[tx],tx);error=0
            mathematical=independent_math(lanes,signs[tx],tx,report)
        except TrainingInvalid as exc:
            report=exc.diagnostics;error=2;mathematical=None
        result={'selected':report['selected_rx'],'energy':report['energy_u50'],
                'step':report.get('symbol_step_s32',0),'h':report.get('H_raw_s26',[0]*4),'error':error}
        if inject:
            before=lanes[:bad_at]
            result={'selected':0,'energy':[sum(int(v)**2 for v in before[:,2*r:2*r+2].flat) for r in (0,1)],'step':0,'h':[0]*4,'error':1}
        jobs.append(dict(id=(0xfffffff0+13*len(jobs))&MASK,tx=tx,name=name,cadence=cadence,mode=mode,target=target,inject=inject,bad_at=bad_at,lanes=lanes,result=result,report=report,independent_math=mathematical,source=source))
    # Exact axes and constant-pi use a zero weak RX, which must not reject RX0.
    for tx in (0,1):
        for u,v,name in ((8000,0,'zero-CFO'),(-8000,0,'constant-pi'),(0,8000,'positive-quadrature'),(0,-8000,'negative-quadrature')):
            add(constant(u,v,tx=tx),tx,name+f'-TX{tx}',cadence=50)
    add(constant(8000,weak=8000),name='exact-energy-tie-RX0')
    add(from_despread(np.tile([0,0,12000,-8000],(448,1)),1),1,'RX1-strong-weak-RX0-zero')
    for tx in (0,1):
        for cycles in (-.03,-.02,-.00005,.00005,.02,.03):
            add(tone(tx,cycles,.49999,amp=(18000,32000)),tx,f'CFO-{cycles}-TX{tx}',cadence=32)
    add(tone(0,.02,.49),name='bursty-valid-pending-pair-backpressure',cadence=1)
    add(tone(1,-.02,.49),1,'bursty-RX1-TX1',cadence=1)
    # Rails exercise negation of -2^20 and all four lane arrangements.
    for n in range(16):
        rail=np.array([-(1<<20) if n&(1<<k) else (1<<20)-1 for k in range(4)])
        add(np.tile(rail,(448,1)),n%2,f'constant-rail-mask{n}',cadence=32)
    # Valid rail-aligned training reaches despread2^21, large accumulators and H.
    for tx in (0,1):
        for quadrant in range(4):
            s=signs[tx];rr=np.empty((448,4),dtype=np.int64)
            si,sq=s[:,0],s[:,1]
            if quadrant==1:si,sq=-sq,si
            elif quadrant==2:si,sq=-si,-sq
            elif quadrant==3:si,sq=sq,-si
            rr[:,0]=np.where(si>0,(1<<20)-1,-(1<<20))
            rr[:,1]=np.where(sq>0,(1<<20)-1,-(1<<20))
            rr[:,2:4]=rr[:,:2]
            add(rr,tx,f'valid-full-rails-TX{tx}-quadrant{quadrant}')
    d=np.tile([8000,0,7998,0],(448,1));d[-1,2]=40000
    add(from_despread(d,0),name='last-sample-flips-selected-RX1')
    for direction in (-1,1):
        for perturbation in (1,2,4):
            d=np.tile([-1000000,0,0,0],(448,1));d[:16,0]=1000000
            rr=from_despread(d,0)
            rr[16,0]+=direction*perturbation
            add(rr,name=f'halfturn-guard-side{direction}-perturb{perturbation}')
    add(rng.integers(-(1<<20),1<<20,(448,4)),1,'full-range-random',cadence=1)
    add(np.zeros((448,4),dtype=np.int64),name='all-zero-selected-invalid')
    d=np.tile([8000,0,2000,0],(448,1));d[16:32,0:2]=0
    add(from_despread(d,0),name='selected-zero-block')
    d=np.tile([8000,0,0,0],(448,1));d[16:32:2,0]=16000;d[17:32:2,0]=-16000
    add(from_despread(d,0),name='selected-zero-block-high-energy-cancellation')
    d=np.tile([8000,0,2000,0],(448,1));d.reshape(28,16,4)[1::2,:,0]=-8000
    add(from_despread(d,0),name='selected-halfturn-ambiguous')
    d=np.tile([8000,0,2000,0],(448,1));d.reshape(28,16,4)[1::2,:,2]=-2000
    add(from_despread(d,0),name='weak-halfturn-ignored')
    # Input rejection at the first, while angles work, and last sample.
    for inject in (1,2,3):
        for bad_at in (0,31,447):
            add(tone(0,.00003),name=f'bad-input-{inject}-at{bad_at}',inject=inject,bad_at=bad_at)
    for inject in (1,2,3):
        add(tone(0,.00003),name=f'input-error-{inject}-collides-angle-valid',cadence=34,inject=inject,bad_at=16)
    # Cancellation/reset with dirty buffers and active/held arithmetic.
    for mode in (1,2):
        for target in (1,2,3,4,5,6,7,8,9,10):
            add(tone(1,-.00004),1,f'{"cancel" if mode==1 else "reset"}-state{target}',mode=mode,target=target,cadence=1 if target==1 else 32)
    add(tone(0,.00004),name='restart-after-dirty-cancel-reset')
    # Real inputs are copied into the artifact with source CRC/provenance.
    for fp in fixtures:
        fp=Path(fp);meta=json.loads(fp.with_suffix('.json').read_text())
        with np.load(fp,allow_pickle=False) as f:
            if 'input_iq' not in f or 'tx' not in f:raise ValueError('training fixture needs input_iq,tx arrays')
            array=np.asarray(f['input_iq']);txs=np.asarray(f['tx']).reshape(-1)
            fixture_gold={k:np.asarray(f[k]).tolist() for k in ('expected_error','expected_selected_rx','expected_step_s32','expected_H_s26')}
            if not np.array_equal(f['public_signs'],signs[int(txs[0])]):raise ValueError('fixture public signs differ from public ROM')
            if array.ndim==2:array=array[None,:,:]
            if len(txs)!=len(array):raise ValueError('fixture TX count')
            for i,(lanes,tx) in enumerate(zip(array,txs)):
                add(lanes,int(tx),f'{fp.stem}:{i}',cadence=50 if i%2==0 else 32,
                    source={'path':str(fp.resolve()),'crc32':f'{zlib.crc32(fp.read_bytes()):08x}','index':i,'metadata':meta})
                result=jobs[-1]['result']
                if (result['error'],result['selected'],result['step'],result['h']) != (fixture_gold['expected_error'],fixture_gold['expected_selected_rx'],fixture_gold['expected_step_s32'],fixture_gold['expected_H_s26']):raise AssertionError('fixture stored output/model mismatch')
    # Independent exact algebra/unit tests, including negative division ties.
    for numerator in range(-10,11):
        for offset in (-29233,-29232,-29231,0,29231,29232,29233):
            n=numerator*58464+offset
            q,r=divmod(n,58464)
            if nearest_div(n,58464)!=q+int(2*r>=58464):raise AssertionError('division tie math')
    if sum(2*b-27 for b in range(28))!=0 or sum((2*b-27)**2 for b in range(28))!=7308:raise AssertionError('OLS weights')
    for j in jobs:
        if j['name'].startswith(('zero-CFO','constant-pi','positive-quadrature','negative-quadrature')):
            if j['result']['error'] or j['result']['step']!=0:raise AssertionError('constant phase mathematics')
    if jobs[-1]['result']['error'] not in (0,2):raise AssertionError('terminal job invariant')
    return jobs,public


TB=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,cv=0,ct=0,iv=0,ie=0,ready=0;
 reg [31:0] cid=0,iid=0;reg [8:0] idx=0;reg [83:0] data=0;
 wire cr,ir,ov,ot,os;wire [31:0] oid;wire signed [31:0] raw_step;
 wire [103:0] raw_h;wire [2:0] error;wire [49:0] e0,e1;
 wire signed [31:0] step=raw_step ^ @PHASE_FAULT@;
 wire [103:0] h=raw_h ^ @H_FAULT@;
 training_channel_cost_probe #(.SIGN_FILE("@SIGNS@")) dut(
 .clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cv),.cfg_ready(cr),.cfg_id(cid),.cfg_tx(ct),
 .in_valid(iv),.in_ready(ir),.in_id(iid),.in_index(idx),.in_error(ie),.in_data(data),
 .out_valid(ov),.out_ready(ready),.out_id(oid),.out_tx(ot),.out_selected_rx(os),
 .out_phase_step(raw_step),.out_h(raw_h),.out_error(error),.out_energy0(e0),.out_energy1(e1));
 integer fd,n,jobno=0,jmode,target,cadence,inject,bad_at,count,i,c;
 integer cycle=0,last_input_cycle=0,job_last_cycle=0,job_angles=0,last_cfg_cycle=0;
 integer accepted=0,configured=0,outputs=0,success=0,rejected1=0,rejected2=0,aborted=0;
 integer output_stalls=0,input_stalls=0,cfg_stalls=0,cancel_cycles=0,reset_cycles=0;
 integer cadence32=0,cadence50=0,min_tail=999999,max_tail=0,max_tail32=0,max_tail50=0,angles=0;
 integer service_jobs=0,min_job_service=9999999,max_job_service=0;
 integer held_checks=0,job_inputs=0,max_burst_stalls=0,burst_stalls=0;
 integer state_seen[0:9];integer finished=0;
 integer input_error_angle_collisions=0,collect_cancel_angle_busy=0;
 reg [31:0] ejid,estep;reg etx,esel;reg [103:0] eh;reg [2:0] ee;reg [49:0] ee0,ee1;
 reg [83:0] samples[0:447];reg active=0,seen=0,held=0,inputheld=0;
 reg [272:0] held_output;reg [125:0] held_input;
 wire [272:0] all_output={oid,ot,os,step,h,error,e0,e1};
 wire [125:0] all_input={iid,idx,ie,data};
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst||cancel) begin
   held=0;inputheld=0;
   if(rst) reset_cycles=reset_cycles+1;
   if(cancel) cancel_cycles=cancel_cycles+1;
  end else begin
   if(held && (!ov || all_output!==held_output)) $fatal(1,"held output changed");
   if(inputheld && (!iv || all_input!==held_input)) $fatal(1,"held input changed");
   if(cv&&!cr) cfg_stalls=cfg_stalls+1;
   if(cv&&cr) begin
    configured=configured+1;job_inputs=0;job_angles=0;seen=0;active=1;
    last_input_cycle=0;last_cfg_cycle=cycle;burst_stalls=0;
   end
   if(iv&&!ir) begin input_stalls=input_stalls+1;burst_stalls=burst_stalls+1;end
   if(iv&&ir) begin
    if(!active) $fatal(1,"input without descriptor");
    if(last_input_cycle && cadence>1) begin
     if(cycle-last_input_cycle!=cadence) $fatal(1,"unexpected backpressure at cadence%0d difference%0d",cadence,cycle-last_input_cycle);
     if(cadence==32) cadence32=cadence32+1;
     if(cadence==50) cadence50=cadence50+1;
    end
    last_input_cycle=cycle;job_last_cycle=cycle;job_inputs=job_inputs+1;accepted=accepted+1;
   end
   if(dut.angle_valid) begin job_angles=job_angles+1;angles=angles+1;end
   if(dut.angle_valid && dut.input_bad) input_error_angle_collisions=input_error_angle_collisions+1;
   if(ov&&!seen) begin
    if(!active) $fatal(1,"unexpected stale output");
    if({oid,ot,os,step,h,error,e0,e1}!=={ejid,etx,esel,estep,eh,ee,ee0,ee1})
      $fatal(1,"training mismatch job%0d id%h/%h err%0d/%0d selected%0d/%0d step%h/%h H%h/%h energy%0d,%0d/%0d,%0d",jobno,oid,ejid,error,ee,os,esel,step,estep,h,eh,e0,e1,ee0,ee1);
    if(ee!=1 && job_angles!=56) $fatal(1,"expected56 angles got%0d",job_angles);
    if(!ee) begin
     if(cycle-job_last_cycle>1650) $fatal(1,"nominal33us tail deadline exceeded %0d",cycle-job_last_cycle);
     if(cycle-job_last_cycle<min_tail) min_tail=cycle-job_last_cycle;
     if(cycle-job_last_cycle>max_tail) max_tail=cycle-job_last_cycle;
     if(cadence==32 && cycle-job_last_cycle>max_tail32) max_tail32=cycle-job_last_cycle;
     if(cadence==50 && cycle-job_last_cycle>max_tail50) max_tail50=cycle-job_last_cycle;
     success=success+1;
     if(cadence==1) begin
      service_jobs=service_jobs+1;
      if(cycle-last_cfg_cycle<min_job_service) min_job_service=cycle-last_cfg_cycle;
      if(cycle-last_cfg_cycle>max_job_service) max_job_service=cycle-last_cfg_cycle;
      if(burst_stalls>max_burst_stalls) max_burst_stalls=burst_stalls;
     end
    end else if(ee==1) rejected1=rejected1+1;else rejected2=rejected2+1;
    outputs=outputs+1;seen=1;
   end
   held=ov&&!ready;
   if(held) begin held_output=all_output;output_stalls=output_stalls+1;held_checks=held_checks+1;end
   inputheld=iv&&!ir;if(inputheld) held_input=all_input;
   if(ov&&ready) begin active=0;seen=0;end
   state_seen[dut.state]=state_seen[dut.state]+1;
  end
  #1;if((rst||cancel)&&(ov||cr||ir)) $fatal(1,"reset/cancel gates");
 end
 task revoke;
  begin
   iv=0;cv=0;ready=0;
   if(target==1) begin
    if(!dut.angle.busy) $fatal(1,"COLLECT revocation did not hit busy angle");
    collect_cancel_angle_busy=collect_cancel_angle_busy+1;
   end
   if(jmode==1) cancel=1;else rst=1;
   repeat(2) @(negedge clk);cancel=0;rst=0;active=0;seen=0;
   #1;if(!cr||ov||ir) $fatal(1,"revoke not idle");
   repeat(5) begin @(negedge clk);if(ov) $fatal(1,"stale result after revoke");end
   aborted=aborted+1;
  end
 endtask
 initial begin
  for(i=0;i<10;i=i+1) state_seen[i]=0;
  fd=$fopen("jobs.txt","r");repeat(4) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%h %d %d %d %d %d %d %d %h %h %d %h %h\n",ejid,etx,cadence,jmode,target,inject,bad_at,esel,estep,eh,ee,ee0,ee1);
   if(n!=13) $fatal(1,"job parse %0d",n);
   for(i=0;i<448;i=i+1) begin n=$fscanf(fd,"%h\n",samples[i]);if(n!=1) $fatal(1,"sample parse");end
   jobno=jobno+1;ready=0;cid=ejid;ct=etx;cv=1;
   @(posedge clk);if(!cr) $fatal(1,"descriptor not ready");
   @(negedge clk);cv=0;
   // Reassert next descriptor while active; it must remain blocked.
   cv=1;cid=ejid+1;ct=!etx;
   count=(inject!=0)?bad_at+1:448;
   if(jmode && target==1) count=23;
   for(i=0;i<count;i=i+1) begin
    iid=ejid;idx=9'(i);ie=0;data=samples[i];
    if(i==bad_at) begin
     if(inject==1) iid=ejid^32'h01000000;
     if(inject==2) idx=9'(i+1);
     if(inject==3) ie=1;
    end
    iv=1;@(posedge clk);while(!ir) @(posedge clk);
    @(negedge clk);iv=0;
    if(i!=count-1 && cadence>1) repeat(cadence-1) @(negedge clk);
   end
   if(jmode && target==1) revoke;
   else if(jmode && target<10) begin
    while(dut.state!=target) @(negedge clk);
    revoke;
   end else begin
    while(!ov) @(negedge clk);
    repeat(17) @(negedge clk);
    if(jmode) revoke;
    else begin
     if(cr||ir) $fatal(1,"held result admitted input/config");
     cv=0;ready=1;@(posedge clk);if(!ov) $fatal(1,"output absent");
     @(negedge clk);ready=0;
    end
   end
  end
  repeat(5) @(negedge clk);
  if(jobno!=@JOBS@ || outputs!=@OUTPUTS@ || aborted!=20 || success<20 || rejected1!=12 || rejected2<3 || input_error_angle_collisions!=3 || collect_cancel_angle_busy!=2 ||
     input_stalls==0 || output_stalls<500 || cfg_stalls==0 || cadence32<10000 || cadence50<3000 ||
     service_jobs<3 || max_burst_stalls==0 || cancel_cycles!=20 || reset_cycles!=24)
    $fatal(1,"coverage jobs%0d output%0d aborted%0d success%0d errors%0d/%0d stalls%0d cadence%0d/%0d service%0d",jobno,outputs,aborted,success,rejected1,rejected2,input_stalls,cadence32,cadence50,service_jobs);
  for(i=0;i<10;i=i+1) if(!state_seen[i]) $fatal(1,"uncovered state%0d",i);
  $display("G0_TRAINING_CHANNEL_PASS jobs=%0d configured=%0d accepted=%0d outputs=%0d success=%0d error1=%0d error2=%0d aborted=%0d angles=%0d cadence32_intervals=%0d cadence50_intervals=%0d min_tail_cycles=%0d max_tail_cycles=%0d max_tail32_cycles=%0d max_tail50_cycles=%0d input_stalls=%0d output_stalls=%0d cfg_stalls=%0d burst_jobs=%0d min_burst_job_cycles=%0d max_burst_job_cycles=%0d max_burst_stalls=%0d cancel_cycles=%0d reset_cycles=%0d input_error_angle_collisions=%0d collect_cancel_angle_busy=%0d",jobno,configured,accepted,outputs,success,rejected1,rejected2,aborted,angles,cadence32,cadence50,min_tail,max_tail,max_tail32,max_tail50,input_stalls,output_stalls,cfg_stalls,service_jobs,min_job_service,max_job_service,max_burst_stalls,cancel_cycles,reset_cycles,input_error_angle_collisions,collect_cancel_angle_busy);
  $finish;
 end
 initial begin #500000000;$fatal(1,"timeout");end
endmodule
'''


def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    project=Path(__file__).resolve().parents[1]
    jobs,public=make_jobs(a.fixture);verify_normalization()
    sign_words=[]
    for tx in (0,1):
        p=public[TRAIN_START[tx]:TRAIN_START[tx]+512,tx]
        sign_words += [int(v.real<0)+2*int(v.imag<0) for v in p]
    (out/'training_signs.hex').write_text(''.join(f'{v:x}\n' for v in sign_words))
    (out/'wrong_training_signs.hex').write_text(''.join(f'{v^1:x}\n' for v in sign_words))
    (out/'sine.hex').write_text(''.join(f'{round(65536*math.sin(math.pi*i/2048)):05x}\n' for i in range(1024)))
    with (out/'jobs.txt').open('w') as f:
        for j in jobs:
            r=j['result'];f.write(f"{j['id']:08x} {j['tx']} {j['cadence']} {j['mode']} {j['target']} {j['inject']} {j['bad_at']} {r['selected']} {r['step']&MASK:08x} {pack(r['h'],26):026x} {r['error']} {r['energy'][0]:013x} {r['energy'][1]:013x}\n")
            f.writelines(f'{pack(row,21):021x}\n' for row in j['lanes'])
    serial=[{k:v for k,v in j.items() if k!='lanes'} for j in jobs]
    (out/'jobs.json').write_text(json.dumps(serial,indent=2)+'\n')
    for fixture in a.fixture:
        shutil.copyfile(fixture,out/Path(fixture).name)
        shutil.copyfile(Path(fixture).with_suffix('.json'),out/Path(fixture).with_suffix('.json').name)
    sources=[]
    for filename in ('training_channel_cost_probe.sv','phase_angle_cost_probe.sv'):
        p=out/filename;shutil.copyfile(project/'sim/g0'/filename,p);sources.append(p)
    tb=TB.replace('@JOBS@',str(len(jobs))).replace('@OUTPUTS@',str(sum(j['mode']==0 or j['target']==10 for j in jobs))).replace('@PHASE_FAULT@',"32'd0").replace('@H_FAULT@',"104'd0").replace('@SIGNS@','training_signs.hex')
    (out/'tb.sv').write_text(tb)
    bp=a.vivado_bin.resolve();runs=[]
    def run(name,args):
        runs.append(args);(out/'commands.json').write_text(json.dumps(runs,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    run('compile',[str(bp/'xvlog'),'--sv',*[str(p) for p in sources],str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','training_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'training_tb','-runall'])
    sim=(out/'simulate.log').read_text()
    if 'G0_TRAINING_CHANNEL_PASS' not in sim or 'Fatal:' in sim:raise ValueError('training simulation failed')
    measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',sim.split('G0_TRAINING_CHANNEL_PASS')[-1])}
    for kind in ('phase','H','sign'):
        name=kind.lower()+'_fault_tb';fault=tb.replace('module tb;',f'module {name};')
        if kind=='phase':fault=fault.replace("raw_step ^ 32'd0","raw_step ^ (ov && error==0 ? 32'd1:32'd0)")
        elif kind=='H':fault=fault.replace("raw_h ^ 104'd0","raw_h ^ (ov && error==0 ? 104'd1:104'd0)")
        else:fault=fault.replace('"training_signs.hex"','"wrong_training_signs.hex"')
        fp=out/(name+'.sv');fp.write_text(fault)
        run(name+'-compile',[str(bp/'xvlog'),'--sv',str(fp)])
        run(name+'-elaborate',[str(bp/'xelab'),name,'-s',name,'-mt','off'])
        run(name+'-simulate',[str(bp/'xsim'),name,'-runall'])
        log=(out/(name+'-simulate.log')).read_text()
        if 'Fatal: training mismatch' not in log or 'G0_TRAINING_CHANNEL_PASS' in log:raise ValueError(kind+' negative control was not rejected')
    receipt={'scope':'Configured finite448-symbol training CFO/H arithmetic and service only; no acquisition/header/pilot consumer or full PHY acceptance',
      'clock_hz':50000000,'input_signed_bits':21,'input_scale':4,'H_signed_bits':26,'H_scale':64,
      'public_signs_source':'g0_packet_probe.public_prefix(),1024 public QPSK symbols, no payload/expected channel input',
      'oracle':'g0_fixed_training_model estimate_training; independent float atan2/uniform OLS and direct H bound per valid job',
      'independent_math_cases':sum(j['independent_math'] is not None for j in jobs),'gold_jobs':len(jobs),
      'real_fixture_jobs':sum(j['source'] is not None for j in jobs),'measured':measured,
      'nominal_header_gap_cycles':1650,'nominal_header_gap_passed':measured['max_tail_cycles']<=1650,
      'max_frontend_header_gap_cycles':1056,'max_frontend_header_buffer_symbols_needed':max(0,math.ceil((measured['max_tail32_cycles']-1056)/32)),
      'header_gap_assumption':'Conservative TX1/full-matrix barrier before header, at indicated symbol input cadence. Current reference header uses TX0 only; an early TX0 header path is not implemented here.32-cycle cadence is service/catchup evidence, not measured RF symbol timing.',
      'tail_latency_microseconds':measured['max_tail_cycles']/50,
      'negative_controls_rejected':['phase output bit','H output bit','public training sign corruption'],
      'sources_copied_before_sim_and_synthesis':True,'routed_OOC':None,'g0_passed':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    if not a.sim_only:
        (out/'ooc.tcl').write_text('set_param general.maxThreads 4\n'+''.join(f'read_verilog -sv {{{p}}}\n' for p in sources)+'''synth_design -top training_channel_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20.000 [get_ports clk]
set_input_delay 2.0 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2.0 -clock processing [get_ports -filter {DIRECTION == OUT}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
report_timing -max_paths 10 -file paths.rpt
write_checkpoint -force training_channel_cost_probe.dcp
''')
        run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
        receipt['routed_OOC']=routed_measurements(out)
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--sim-only',action='store_true')
    p.add_argument('--fixture',type=Path,action='append',default=[])
    main(p.parse_args())
