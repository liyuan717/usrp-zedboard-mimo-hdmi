#!/usr/bin/env python3
"""Finite variable-length startup CFO/quality RTL oracle and service/cost probe.

Gold uses direct public despreading, independent whole-block OLS, full-circle
sin/cos and exact integer/Fraction comparisons. No CFO/channel truth enters RTL.
This does not implement the startup scheduler or final NCO descriptor dispatch.
"""
import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import zlib
import numpy as np
from g0_packet_probe import public_prefix
from phase_angle_cost_probe import cordic_phase
from g0_fixed_startup_model import estimate_startup
from g0_fixed_training_model import estimate_training,TrainingInvalid,training_signs
from fec_inverse_cost_probe import routed_measurements

MASK32=(1<<32)-1


def pack(values,width):
    return sum((int(v)&((1<<width)-1))<<(k*width) for k,v in enumerate(values))


def concat(fields):
    value=0
    for x,b in fields:value=(value<<b)|(int(x)&((1<<b)-1))
    return value


def nearest(n,d):
    q=math.floor(Fraction(int(n),int(d))+Fraction(1,2))
    assert q==(2*int(n)+int(d))//(2*int(d))
    return q


def signs_for(first,m,tx=0):
    seq=public_prefix()[first:first+m,tx]
    assert np.allclose(abs(seq.real),1/np.sqrt(2)) and np.allclose(abs(seq.imag),1/np.sqrt(2))
    return np.column_stack((np.where(seq.real>0,1,-1),np.where(seq.imag>0,1,-1)))


def inverse_despread(de,signs):
    de=np.asarray(de,dtype=np.int64);out=np.zeros_like(de)
    for rx in (0,1):
        i,q=de[:,2*rx],de[:,2*rx+1]
        a=i*signs[:,0]-q*signs[:,1];b=i*signs[:,1]+q*signs[:,0]
        assert np.all(a%2==0) and np.all(b%2==0)
        out[:,2*rx]=a//2;out[:,2*rx+1]=b//2
    assert np.all(out>=-(1<<20)) and np.all(out<(1<<20))
    return out


def independent_estimate(lanes,first,override_num=None,override_energy=False):
    """Direct variable-M arithmetic, independently indexed from public symbols."""
    lanes=np.asarray(lanes,dtype=np.int64);m=len(lanes);b=m//16;s=signs_for(first,m)
    assert lanes.shape==(m,4) and 128<=m<=448
    energy=[sum(int(x)**2 for x in lanes[:,2*r:2*r+2].flat) for r in (0,1)]
    de=[]
    for row,(si,sq) in zip(lanes,s):
        d=[]
        for r in (0,1):
            i,q=map(int,row[2*r:2*r+2]);d.extend((i*int(si)+q*int(sq),q*int(si)-i*int(sq)))
        de.append(d)
    blocks=[[sum(de[k][lane] for k in range(16*j,16*j+16)) for lane in range(4)] for j in range(b)]
    nums=[];bad=[];all_phases=[];all_unwrapped=[];float_bounds=[]
    divisor=16*sum((2*j-b+1)**2 for j in range(b))//2
    assert divisor==8*b*(b*b-1)//3 and 0<divisor<65536
    for rx in (0,1):
        p=[];invalid=False
        for row in blocks:
            phase,error=cordic_phase(row[2*rx],row[2*rx+1]);p.append(phase);invalid|=bool(error)
        signed=[v if v<(1<<31) else v-(1<<32) for v in p];u=[signed[0]]
        for old,new in zip(signed,signed[1:]):
            raw=new-old
            delta=raw-(1<<32) if raw>(1<<31) else raw+(1<<32) if raw<-(1<<31) else raw
            invalid|=abs(delta)>=(1<<31)-64;u.append(u[-1]+delta)
        num=sum((2*j-b+1)*phase for j,phase in enumerate(u));nums.append(num);bad.append(invalid)
        all_phases.append(p);all_unwrapped.append(u)
        if not invalid:
            exact=np.unwrap(np.arctan2([v[2*rx+1] for v in blocks],[v[2*rx] for v in blocks]))/(2*np.pi)*(1<<32)
            center=np.arange(b)-(b-1)/2
            float_step=float(np.dot(center,exact)/(16*np.dot(center,center)))
            bound=sum(abs(2*j-b+1) for j in range(b))*32/divisor+.5
            assert abs(nearest(num,divisor)-float_step)<=bound+1e-5
            float_bounds.append({'RX':rx,'step_error_LSB':abs(nearest(num,divisor)-float_step),'step_bound_LSB':bound})
    if override_energy:energy=[0,0]
    selected=int(energy[1]>energy[0]);num=nums[selected] if override_num is None else int(override_num)
    result={'selected':selected,'energy':energy,'num':0,'D':0,'step':0,'coherent':[0,0],'quality':0,'h':[0]*4,'error':2,'admitted':0}
    diagnostics={'M':m,'B':b,'tail':m%16,'numerators':nums,'branch_invalid':bad,'block_sums':blocks,'phases':all_phases,'unwrapped':all_unwrapped,'float_OLS_bounds':float_bounds}
    if bad[selected]:return result,diagnostics
    step=nearest(num,divisor);totals=[0,0]
    # Entire circle avoids the RTL quarter-wave folding and pipeline schedule.
    for k,row in enumerate(de):
        phase=(-k*step)&MASK32;index=((phase+0x80000)&MASK32)>>20
        angle=index*2*math.pi/4096;c=round(math.cos(angle)*65536);sine=round(math.sin(angle)*65536)
        i,q=row[2*selected:2*selected+2]
        totals[0]+=nearest(i*c-q*sine,4096);totals[1]+=nearest(i*sine+q*c,4096)
    quality=sum(x*x for x in totals);frequency_ok=50*abs(num)<=divisor*(1<<32)
    quality_ok=energy[selected]>0 and 5*quality>=1024*m*energy[selected]
    result.update(num=num,D=divisor,step=step,coherent=totals,quality=quality,error=3 if not frequency_ok else 0 if quality_ok else 4,admitted=int(frequency_ok and quality_ok))
    diagnostics.update(quality_lhs=5*quality,quality_rhs=1024*m*energy[selected],CFO_lhs=50*abs(num),CFO_rhs=divisor*(1<<32))
    assert all(-(1<<35)<=x<(1<<35) for x in totals) and quality<(1<<73)
    return result,diagnostics


def result_word(job):
    r=job['result']
    return concat([(job['id'],32),(job['tx'],1),(r['selected'],1),(r['step'],32),(pack(r['h'],26),104),(r['error'],3),
                   (r['energy'][0],50),(r['energy'][1],50),(r['num'],48),(r['D'],16),(r['quality'],73),(pack(r['coherent'],36),72),(r['admitted'],1)])


def make_jobs(fixtures,normal_fixtures=(),nonzero_sweep=False):
    jobs=[];rng=np.random.default_rng(354098)
    def add(lanes,first=464,name='synthetic',cadence=32,mode=0,target=0,inject=0,bad_at=-1,override_num=None,override_energy=False,tx=0,startup=1,cfg_count=None,cfg_first=None,source=None):
        lanes=np.asarray(lanes,dtype=np.int64);m=len(lanes);count=m if cfg_count is None else cfg_count;first_cfg=first if cfg_first is None else cfg_first
        if startup and (tx or count<128 or count>448 or first_cfg<384 or first_cfg+count>896):
            result={'selected':0,'energy':[0,0],'num':0,'D':0,'step':0,'coherent':[0,0],'quality':0,'h':[0]*4,'error':5,'admitted':0};diag={}
        elif not startup:
            _,signs=training_signs(public_prefix(),tx)
            try:
                info=estimate_training(lanes,signs,tx);result={'selected':info['selected_rx'],'energy':info['energy_u50'],'num':info['weighted_sums_s48'][info['selected_rx']],'D':58464,'step':info['symbol_step_s32'],'coherent':[0,0],'quality':0,'h':info['H_raw_s26'],'error':0,'admitted':1}
            except TrainingInvalid as error:
                info=error.diagnostics;result={'selected':info['selected_rx'],'energy':info['energy_u50'],'num':0,'D':0,'step':0,'coherent':[0,0],'quality':0,'h':[0]*4,'error':2,'admitted':0}
            diag=info
        else:
            result,diag=independent_estimate(lanes,first,override_num,override_energy)
            if override_num is None and not override_energy:
                model=estimate_startup(lanes,signs_for(first,m),first,0,0)
                assert result['selected']==model['selected_rx'] and result['energy']==model['energy_u50']
                if result['error']!=2:
                    assert (result['num'],result['D'],result['step'],result['coherent'],result['admitted'])==(model['num_s48'],model['D_u16'],model['step_s32'],model['coherent_sums_s36'],int(model['accepted']))
        if inject:
            result={'selected':0,'energy':[sum(int(v)**2 for v in lanes[:bad_at,2*r:2*r+2].flat) for r in (0,1)],'num':0,'D':0,'step':0,'coherent':[0,0],'quality':0,'h':[0]*4,'error':1,'admitted':0}
        jobs.append(dict(id=(0xfffffff0+13*len(jobs))&MASK32,name=name,lanes=lanes,first=first_cfg,count=count,tx=tx,startup=startup,cadence=cadence,mode=mode,target=target,inject=inject,bad_at=bad_at,override_num=override_num,override_energy=override_energy,result=result,diagnostics=diag,source=source))
    def constant(m=400,first=464,values=(8000,0,2000,0)):
        return inverse_despread(np.tile(values,(m,1)),signs_for(first,m))
    def tone(m=400,first=464,cycles=.01,strong=0):
        de=np.zeros((m,4),dtype=np.int64)
        for r in (0,1):
            phase=2*np.pi*(cycles*np.arange(m)+.49999+r*.17);amp=20000 if r==strong else 2000
            de[:,2*r]=2*np.rint(amp*np.cos(phase)/2).astype(np.int64);de[:,2*r+1]=2*np.rint(amp*np.sin(phase)/2).astype(np.int64)
        return inverse_despread(de,signs_for(first,m))
    if nonzero_sweep:
        for b in range(8,29):
            m=16*b+(15 if b<28 else 0);first=896-m
            for cycles in (-.007631,.013713):
                add(tone(m,first,cycles,strong=b%2),first,f'nonzero-OLS-B{b}-M{m}-cycles{cycles}',cadence=32 if cycles<0 else 50)
        return jobs
    for m in range(128,449):
        first=384 if m%2==0 else 896-m
        add(constant(m,first,values=(8000,0,2000,0) if m%3 else (2000,0,8000,0)),first,f'all-counts-M{m}',cadence=50 if m%7==0 else 32)
    for cycles in (-.025,-.020001,-.02,-.019999,-.00001,.00001,.019999,.02,.020001,.025):add(tone(cycles=cycles),name=f'CFO-{cycles}',cadence=50)
    for m in (128,448):
        first=896-m
        for delta in (-2,0,2):
            de=np.zeros((m,4),dtype=np.int64);de[:,0]=2;de[:,1]=np.tile([4,-4,2,-2,2,-2,0,0],m//8);de[0,1]+=delta;de[1,1]-=delta
            for scale in (1,250000):add(inverse_despread(de*scale,signs_for(first,m)),first,f'quality-boundary-M{m}-delta{delta}-scale{scale}')
    for sign in (-1,1):
        limit=41600*(1<<32)//50
        for delta in (-1,0,1):add(tone(cycles=sign*.02),name=f'OLS-domain-CFO-boundary-sign{sign}-delta{delta}',override_num=sign*limit+delta)
    add(constant(),name='quality-domain-zero-energy',override_energy=True)
    add(np.zeros((400,4),dtype=np.int64),name='selected-all-zero')
    add(constant(values=(-8000,0,0,0)),name='constant-pi-weak-zero')
    d=np.tile([8000,0,2000,0],(400,1));d[16:32,:2]=0;add(inverse_despread(d,signs_for(464,400)),name='selected-zero-block')
    d=np.tile([8000,0,2000,0],(400,1));d[16:32,0]=-8000;add(inverse_despread(d,signs_for(464,400)),name='selected-halfturn')
    d=np.tile([8000,0,2000,0],(400,1));d[16:32,2]=-2000;add(inverse_despread(d,signs_for(464,400)),name='weak-halfturn-ignored')
    d=np.tile([8000,0,7998,0],(415,1));d[-1,2]=40000;add(inverse_despread(d,signs_for(448,415)),448,'tail-flips-selected-RX1')
    add(constant(values=(8000,0,8000,0)),name='energy-tie-RX0')
    for r in (0,1):add(tone(strong=r),name=f'bursty-RX{r}',cadence=1)
    for mask in range(16):
        values=[-(1<<20) if mask&(1<<lane) else (1<<20)-1 for lane in range(4)]
        add(np.tile(values,(128,1)),768,f'raw-signed21-rail-mask{mask}')
    for quadrant in range(4):
        signs=signs_for(448,448);i,q=signs[:,0],signs[:,1]
        if quadrant==1:i,q=-q,i
        elif quadrant==2:i,q=-i,-q
        elif quadrant==3:i,q=q,-i
        row=np.column_stack((np.where(i>0,(1<<20)-1,-(1<<20)),np.where(q>0,(1<<20)-1,-(1<<20))))
        add(np.tile(row,(1,2)),448,f'valid-full-rails-quadrant{quadrant}')
    add(rng.integers(-(1<<20),1<<20,(143,4)),700,'random-full-range-M143',cadence=1)
    for tx in (0,1):
        first=416+512*tx;ss=signs_for(first,448,tx);add(inverse_despread(np.tile([8000,0,2000,0],(448,1)),ss),first,name=f'normal-mode-TX{tx}',tx=tx,startup=0,cfg_count=0,cfg_first=0)
    for count,first,tx in [(0,384,0),(127,384,0),(449,384,0),(511,384,0),(128,383,0),(128,769,0),(128,1023,0),(400,464,1)]:add(constant(),name=f'bad-cfg-{count}-{first}-{tx}',cfg_count=count,cfg_first=first,tx=tx)
    for inject in (1,2,3):
        for at in (0,16,399):add(tone(),name=f'input-error{inject}-at{at}',cadence=34 if at==16 else 32,inject=inject,bad_at=at)
    for mode in (1,2):
        for target in (1,2,3,4,5,6,7,10,11,12,13,14):add(tone(),name=f'cancel-reset-{mode}-state{target}',cadence=1 if target==1 else 32,mode=mode,target=target)
    for path in fixtures:
        path=Path(path);meta=json.loads(path.with_suffix('.json').read_text())
        with np.load(path,allow_pickle=False) as f:
            lanes=f['lanes'];first=int(f['public_indices'][0]);assert np.array_equal(f['signs'],signs_for(first,len(lanes)))
        add(lanes,first,path.stem,cadence=50,source={'path':str(path.resolve()),'crc32':f'{zlib.crc32(path.read_bytes()):08x}','metadata':meta})
        expected=meta['expected'];actual=jobs[-1]['result']
        assert (actual['num'],actual['D'],actual['step'],actual['coherent'],actual['energy'],actual['selected'],actual['admitted'])==(expected['num_s48'],expected['D_u16'],expected['step_s32'],expected['coherent_sums_s36'],expected['energy_u50'],expected['selected_rx'],int(expected['accepted']))
        for tx in (0,1):
            matches=[Path(fp) for fp in normal_fixtures if Path(fp).stem==path.stem.replace('-startup-0',f'-TX{tx}')]
            if not matches:continue
            normal_path=matches[0];normal_meta=json.loads(normal_path.with_suffix('.json').read_text())
            with np.load(normal_path,allow_pickle=False) as f:
                normal=f['input_iq'].copy();normal_gold=(int(f['expected_error']),int(f['expected_selected_rx']),int(f['expected_step_s32']),f['expected_H_s26'].tolist())
                assert int(f['tx'])==tx and np.array_equal(f['public_signs'],signs_for(416+512*tx,448,tx))
            add(normal,416+512*tx,normal_path.stem+'-shared-runtime-normal',cadence=50,tx=tx,startup=0,cfg_count=0,cfg_first=0,
                source={'path':str(normal_path.resolve()),'crc32':f'{zlib.crc32(normal_path.read_bytes()):08x}','metadata':normal_meta})
            r=jobs[-1]['result'];assert (r['error'],r['selected'],r['step'],r['h'])==normal_gold
            add(lanes,first,path.stem+f'-restart-after-normalTX{tx}',cadence=50,source={'path':str(path.resolve()),'crc32':f'{zlib.crc32(path.read_bytes()):08x}','metadata':meta})
    return jobs


TB=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,cv=0,tx=0,startup=1,iv=0,ie=0,ready=0;
 reg[31:0] cid=0,iid=0;reg[8:0] cfg_count=400,idx=0;reg[9:0] first=464;reg[83:0] data=0;
 wire cr,ir,ov,ot,os,admitted;wire[31:0] oid;wire signed[31:0] raw_step;
 wire[103:0] h;wire[2:0] error;wire[49:0] e0,e1;wire signed[47:0] num;wire[15:0] divisor;wire[72:0] raw_quality;wire[71:0] raw_coherent;
 integer fault=0;
 wire[31:0] step=raw_step ^ ((fault==1 && ov && !error)?32'd1:32'd0);
 wire[72:0] quality=raw_quality ^ ((fault==2 && ov && !error)?73'd1:73'd0);
 wire[71:0] coherent=raw_coherent ^ ((fault==3 && ov && !error)?72'd1:72'd0);
 training_channel_cost_probe #(.ENABLE_STARTUP(1),.SIGN_FILE("@SIGNS@")) dut(
 .clk(clk),.rst(rst),.cancel(cancel),.cfg_valid(cv),.cfg_ready(cr),.cfg_id(cid),.cfg_tx(tx),.cfg_startup(startup),.cfg_count(cfg_count),.cfg_first_symbol(first),
 .in_valid(iv),.in_ready(ir),.in_id(iid),.in_index(idx),.in_error(ie),.in_data(data),.out_valid(ov),.out_ready(ready),.out_id(oid),.out_tx(ot),.out_selected_rx(os),.out_phase_step(raw_step),.out_h(h),.out_error(error),.out_energy0(e0),.out_energy1(e1),.out_numerator(num),.out_divisor(divisor),.out_quality_sum(raw_quality),.out_coherent(raw_coherent),.out_admitted(admitted));
 wire[482:0] observed={oid,ot,os,step,h,error,e0,e1,num,divisor,quality,coherent,admitted};
 reg[482:0] gold,held_bits;reg[125:0] held_input;reg[83:0] samples[0:447];reg held=0,inputheld=0,active=0,seen=0;
 reg[31:0] jid;reg etx,estartup;reg signed[47:0] forced_num;
 integer count,first_symbol,cadence,mode,target,inject,bad_at,force_kind,m,fd,n,i,jobs=0,cycle=0,configured=0,inputs=0,outputs=0,consumed=0,aborted=0,revoked=0;
 integer errors[0:5],state_seen[0:13],remainders[0:15],blocks_seen[0:28];
 integer held_checks=0,input_stalls=0,cfg_stalls=0,cadence32=0,cadence50=0,last_input=0,last_cfg=0,job_angles=0,angles=0;
 integer min_tail=999999,max_tail=0,max_tail32=0,max_tail50=0,burst_jobs=0,max_burst_job=0;
 integer input_error_angle_overlap=0,collect_cancel_busy=0,domain_num=0,domain_energy=0,cancel_cycles=0,reset_cycles=0;
 wire[125:0] input_word={iid,idx,ie,data};
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst||cancel) begin held=0;inputheld=0;if(rst) reset_cycles=reset_cycles+1;if(cancel) cancel_cycles=cancel_cycles+1;end
  else begin
   if(held && (!ov || observed!==held_bits)) $fatal(1,"held output changed");
   if(inputheld && (!iv || input_word!==held_input)) $fatal(1,"held input changed");
   if(cv&&!cr) cfg_stalls=cfg_stalls+1;
   if(cv&&cr) begin configured=configured+1;active=1;seen=0;last_cfg=cycle;last_input=0;job_angles=0;end
   if(iv&&!ir) input_stalls=input_stalls+1;
   if(iv&&ir) begin
    if(!active) $fatal(1,"unowned input");inputs=inputs+1;
    if(last_input && cadence>1) begin
     if(cycle-last_input!=cadence) $fatal(1,"cadence%0d missed",cadence);
     if(cadence==32) cadence32=cadence32+1;if(cadence==50) cadence50=cadence50+1;
    end
    last_input=cycle;
   end
   if(dut.angle_valid) begin angles=angles+1;job_angles=job_angles+1;end
   if(dut.angle_valid && dut.input_bad) input_error_angle_overlap=input_error_angle_overlap+1;
   if(ov&&!seen) begin
    if(!active || observed!==gold) $fatal(1,"startup mismatch job%0d id%h err%0d got%h expected%h",jobs,oid,error,observed,gold);
    if(error!=1 && error!=5 && job_angles!=2*(estartup?(count/16):28)) $fatal(1,"complete-block angle count %0d",job_angles);
    outputs=outputs+1;errors[error]=errors[error]+1;seen=1;
    if(estartup && error!=1 && error!=2 && error!=5) begin
     if(cycle-last_input>6400) $fatal(1,"startup512-sample commit budget");
     if(cycle-last_input<min_tail) min_tail=cycle-last_input;if(cycle-last_input>max_tail) max_tail=cycle-last_input;
     if(cadence==32 && cycle-last_input>max_tail32) max_tail32=cycle-last_input;
     if(cadence==50 && cycle-last_input>max_tail50) max_tail50=cycle-last_input;
     if(cadence==1) begin burst_jobs=burst_jobs+1;if(cycle-last_cfg>max_burst_job) max_burst_job=cycle-last_cfg;end
     remainders[count%16]=remainders[count%16]+1;blocks_seen[count/16]=blocks_seen[count/16]+1;
    end
   end
   held=ov&&!ready;if(held) begin held_bits=observed;held_checks=held_checks+1;end
   inputheld=iv&&!ir;if(inputheld) held_input=input_word;
   if(ov&&ready) begin consumed=consumed+1;active=0;seen=0;end
   state_seen[dut.state]=state_seen[dut.state]+1;
  end
  #1;if((rst||cancel)&&(ov||cr||ir)) $fatal(1,"reset/cancel gates");
 end
 task revoke;
  begin
   iv=0;cv=0;ready=0;
   if(target==1) begin if(!dut.angle.busy) $fatal(1,"COLLECT did not cancel active angle");collect_cancel_busy=collect_cancel_busy+1;end
   if(seen) revoked=revoked+1;else aborted=aborted+1;
   if(mode==1) cancel=1;else rst=1;
   repeat(2) @(negedge clk);rst=0;cancel=0;active=0;seen=0;
   #1;if(!cr||ov||ir) $fatal(1,"cancel failed to idle");repeat(5) begin @(negedge clk);if(ov) $fatal(1,"stale terminal");end
  end
 endtask
 initial begin
  n=$value$plusargs("FAULT=%d",fault);
  for(i=0;i<6;i=i+1) errors[i]=0;for(i=0;i<14;i=i+1) state_seen[i]=0;for(i=0;i<16;i=i+1) remainders[i]=0;for(i=0;i<29;i=i+1) blocks_seen[i]=0;
  fd=$fopen("jobs.txt","r");repeat(4) @(negedge clk);rst=0;
  while(!$feof(fd)) begin
   n=$fscanf(fd,"%h %d %d %d %d %d %d %d %d %d %d %h %d %h\n",jid,etx,estartup,count,first_symbol,cadence,mode,target,inject,bad_at,force_kind,forced_num,m,gold);
   if(n!=14) $fatal(1,"job parse%0d",n);
   for(i=0;i<m;i=i+1) begin n=$fscanf(fd,"%h\n",samples[i]);if(n!=1) $fatal(1,"sample parse");end
   jobs=jobs+1;cid=jid;tx=etx;startup=estartup;cfg_count=9'(count);first=10'(first_symbol);cv=1;ready=0;
   @(posedge clk);if(!cr) $fatal(1,"cfg not ready");@(negedge clk);cv=0;
   // Change every external descriptor field while held as a new request.
   cv=1;cid=~jid;tx=!etx;startup=!estartup;cfg_count=0;first=1023;
   if(gold[312:310]!=5) begin
    n=inject?bad_at+1:m;if(mode && target==1) n=23;
    for(i=0;i<n;i=i+1) begin
     iid=jid;idx=9'(i);ie=0;data=samples[i];
     if(i==bad_at) begin if(inject==1) iid=jid^32'h01000000;if(inject==2) idx=9'(i+1);if(inject==3) ie=1;end
     iv=1;@(posedge clk);while(!ir) @(posedge clk);@(negedge clk);iv=0;
     if(i!=n-1 && cadence>1) repeat(cadence-1) @(negedge clk);
    end
   end
   if(force_kind) begin
    // Algebraic gate boundaries; only these explicitly labelled jobs override
    // estimator numerator/E after real input and block angles are available.
    while(dut.state!=2 || dut.angles_finished!=2*(count/16)) @(negedge clk);
    if(force_kind==1) begin dut.weighted_sum[0]=forced_num;domain_num=domain_num+1;end
    else begin dut.energy[0]=50'd0;dut.energy[1]=50'd0;domain_energy=domain_energy+1;end
    while(dut.state==2) @(negedge clk);
   end
   if(mode && target==1) revoke;
   else if(mode && target<14) begin while(dut.state!=target) @(negedge clk);revoke;end
   else begin
    while(!ov) @(negedge clk);repeat(17) @(negedge clk);
    if(mode) revoke;
    else begin if(cr||ir) $fatal(1,"held output admitted next owner");cv=0;ready=1;@(posedge clk);@(negedge clk);ready=0;end
   end
  end
  repeat(5) @(negedge clk);
  if(jobs!=@JOBS@ || configured!=jobs || outputs!=@OUTPUTS@ || configured!=consumed+aborted+revoked || outputs!=consumed+revoked || aborted!=@ABORTS@ || revoked!=@REVOKES@ || input_error_angle_overlap!=@OVERLAPS@ || collect_cancel_busy!=@COLLECT_CANCEL@ || domain_num!=@DOMAIN_NUM@ || domain_energy!=@DOMAIN_ENERGY@ || (@FULL@ && (input_stalls==0 || held_checks<500 || cfg_stalls==0 || cadence32<20000 || cadence50<3000 || burst_jobs<2))) $fatal(1,"coverage/conservation jobs%0d cfg%0d output%0d abort%0d revoke%0d collision%0d",jobs,configured,outputs,aborted,revoked,input_error_angle_overlap);
  if(@FULL@) begin
  for(i=0;i<14;i=i+1) if(!state_seen[i]) $fatal(1,"state%0d not covered",i);
  for(i=0;i<16;i=i+1) if(!remainders[i]) $fatal(1,"tail%0d not covered",i);
  for(i=8;i<29;i=i+1) if(!blocks_seen[i]) $fatal(1,"block count%0d not covered",i);
  for(i=0;i<6;i=i+1) if(!errors[i]) $fatal(1,"error%0d not covered",i);
  end
  $display("G0_STARTUP_TRAINING_PASS jobs=%0d configured=%0d inputs=%0d outputs=%0d consumed=%0d active_aborted=%0d held_revoked=%0d success=%0d error1=%0d error2=%0d error3=%0d error4=%0d error5=%0d angles=%0d cadence32_intervals=%0d cadence50_intervals=%0d min_tail_cycles=%0d max_tail_cycles=%0d max_tail32=%0d max_tail50=%0d input_stalls=%0d held_checks=%0d cfg_stalls=%0d burst_jobs=%0d max_burst_job_cycles=%0d input_error_angle_overlap=%0d collect_cancel_busy=%0d numerator_domain_cases=%0d energy_domain_cases=%0d cancel_cycles=%0d reset_cycles=%0d",jobs,configured,inputs,outputs,consumed,aborted,revoked,errors[0],errors[1],errors[2],errors[3],errors[4],errors[5],angles,cadence32,cadence50,min_tail,max_tail,max_tail32,max_tail50,input_stalls,held_checks,cfg_stalls,burst_jobs,max_burst_job,input_error_angle_overlap,collect_cancel_busy,domain_num,domain_energy,cancel_cycles,reset_cycles);
  $finish;
 end
 initial begin #500000000;$fatal(1,"startup timeout");end
endmodule
'''



def complete_route(out,bp,sources,run,receipt):
    (out/'ooc.tcl').write_text('set_param general.maxThreads 4\n'+''.join(f'read_verilog -sv {{{p}}}\n' for p in sources)+'''synth_design -top training_channel_cost_probe -part xc7z020clg484-1 -mode out_of_context -generic ENABLE_STARTUP=1
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
write_checkpoint -force startup_training_cost_probe.dcp
''')
    run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
    try:
        receipt['routed_OOC']=routed_measurements(out)
        receipt['routed_OOC']['accepted']=True
    except ValueError as e:
        receipt['routed_OOC']={'accepted':False,'validation_error':str(e),'raw_timing_report':str(out/'timing.rpt')};(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');raise
    from collections import Counter
    log=(out/'ooc.log').read_text()
    receipt['routed_OOC']['tool_warning_counts']=dict(sorted(Counter(re.findall(r'^WARNING: \[([^\]]+)\]',log,re.M)).items()))
    receipt['routed_OOC']['tool_critical_warning_count']=len(re.findall(r'^CRITICAL WARNING:',log,re.M))
    receipt['routed_OOC']['tool_error_count']=len(re.findall(r'^ERROR:',log,re.M))
    receipt['routed_OOC']['OOC_warning_scope']='External port partial routes/placement and HD.CLK_SRC clock source/skew remain unverified; no full-board IO/CDC sign-off.'
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')

def route_existing(a):
    """Append OOC to passed immutable RTL snapshots without repeating simulation."""
    out=a.output.resolve();project=Path(__file__).resolve().parents[1]
    receipt=json.loads((out/'receipt.json').read_text())
    if receipt['routed_OOC'] is not None:raise ValueError('route evidence already exists; preserve it')
    if 'G0_STARTUP_TRAINING_PASS' not in (out/'simulate.log').read_text():raise ValueError('normal simulation did not pass')
    sources=[]
    for name in ('training_channel_cost_probe.sv','phase_angle_cost_probe.sv'):
        p=out/name
        if p.read_bytes()!=(project/'sim/g0'/name).read_bytes():raise ValueError('RTL changed after simulation: '+name)
        sources.append(p)
    commands=json.loads((out/'commands.json').read_text())
    def run(name,args):
        commands.append(args);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
    complete_route(out,a.vivado_bin.resolve(),sources,run,receipt)
    print(json.dumps({'artifact':str(out),'measured':receipt['measured'],'routed_OOC':receipt['routed_OOC']}))


def main(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);project=Path(__file__).resolve().parents[1]
    jobs=make_jobs(a.fixture,a.normal_fixture,a.nonzero_ols_only)
    if a.mode_switch_only:jobs=[j for j in jobs if j['source'] is not None]
    if a.mode_switch_only and (len(jobs)!=15 or sum(not j['startup'] for j in jobs)!=6):raise ValueError('mode supplement needs three startup and six normal fixtures')
    serial=[{k:v for k,v in job.items() if k!='lanes'} for job in jobs]
    (out/'jobs.json').write_text(json.dumps(serial,indent=2)+'\n')
    with (out/'jobs.txt').open('w') as f:
        for j in jobs:
            force_kind=1 if j['override_num'] is not None else 2 if j['override_energy'] else 0
            f.write(f"{j['id']:08x} {j['tx']} {j['startup']} {j['count']} {j['first']} {j['cadence']} {j['mode']} {j['target']} {j['inject']} {j['bad_at']} {force_kind} {(j['override_num'] or 0)&((1<<48)-1):012x} {len(j['lanes'])} {result_word(j):0121x}\n")
            f.writelines(f'{pack(row,21):021x}\n' for row in j['lanes'])
    public=public_prefix();words=[]
    for tx in (0,1):
        sequence=public[384+512*tx:896+512*tx,tx]
        words.extend(int(v.real<0)+2*int(v.imag<0) for v in sequence)
    (out/'training_signs.hex').write_text(''.join(f'{v:x}\n' for v in words))
    (out/'wrong_signs.hex').write_text(''.join(f'{v^1:x}\n' for v in words))
    (out/'sine.hex').write_text(''.join(f'{round(65536*math.sin(math.pi*i/2048)):05x}\n' for i in range(1024)))
    sources=[]
    for name in ('training_channel_cost_probe.sv','phase_angle_cost_probe.sv'):
        p=out/name;shutil.copyfile(project/'sim/g0'/name,p);sources.append(p)
    tb=TB.replace('@SIGNS@','training_signs.hex').replace('@JOBS@',str(len(jobs))).replace('@OUTPUTS@',str(sum(j['mode']==0 or j['target']==14 for j in jobs)))
    replacements={'@ABORTS@':sum(j['mode']!=0 and j['target']!=14 for j in jobs),'@REVOKES@':sum(j['mode']!=0 and j['target']==14 for j in jobs),'@OVERLAPS@':sum(j['inject']!=0 and j['bad_at']==16 for j in jobs),'@COLLECT_CANCEL@':sum(j['mode']!=0 and j['target']==1 for j in jobs),'@DOMAIN_NUM@':sum(j['override_num'] is not None for j in jobs),'@DOMAIN_ENERGY@':sum(j['override_energy'] for j in jobs),'@FULL@':int(not a.mode_switch_only and not a.nonzero_ols_only)}
    for key,value in replacements.items():tb=tb.replace(key,str(value))
    (out/'tb.sv').write_text(tb)
    bp=a.vivado_bin.resolve();commands=[]
    def run(name,args):
        commands.append(args);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
    run('compile',[str(bp/'xvlog'),'--sv',*[str(p) for p in sources],str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','startup_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'startup_tb','-runall','-testplusarg','FAULT=0'])
    log=(out/'simulate.log').read_text()
    if 'G0_STARTUP_TRAINING_PASS' not in log or 'Fatal:' in log:raise ValueError('startup simulation failed')
    measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',log.split('G0_STARTUP_TRAINING_PASS')[-1])}
    for fault in (1,2,3):
        run('fault'+str(fault),[str(bp/'xsim'),'startup_tb','-runall','-testplusarg','FAULT='+str(fault)])
        log=(out/('fault'+str(fault)+'.log')).read_text()
        if 'Fatal: startup mismatch' not in log or 'G0_STARTUP_TRAINING_PASS' in log:raise ValueError('negative output mutation not rejected')
    (out/'sign_fault_tb.sv').write_text(tb.replace('module tb;','module sign_fault_tb;').replace('"training_signs.hex"','"wrong_signs.hex"'))
    run('sign-fault-compile',[str(bp/'xvlog'),'--sv',str(out/'sign_fault_tb.sv')])
    run('sign-fault-elaborate',[str(bp/'xelab'),'sign_fault_tb','-s','sign_fault_tb','-mt','off'])
    run('sign-fault-simulate',[str(bp/'xsim'),'sign_fault_tb','-runall'])
    log=(out/'sign-fault-simulate.log').read_text()
    if 'Fatal: startup mismatch' not in log or 'G0_STARTUP_TRAINING_PASS' in log:raise ValueError('public sign mutation not rejected')
    receipt={'scope':__doc__,'clock_hz':50000000,'input_signed_bits':21,'input_scale':4,'source_snapshots_before_simulation_and_route':True,
        'measured':measured,'all_M_values':sorted(set(j['count'] for j in jobs if j['startup'] and j['result']['error']!=5)),'all_B_values':sorted(set(j['count']//16 for j in jobs if j['startup'] and j['result']['error']!=5)),'all_OLS_tail_counts':sorted(set(j['count']%16 for j in jobs if j['startup'] and j['result']['error']!=5)),'mode_switch_supplement_only':a.mode_switch_only,'nonzero_OLS_supplement_only':a.nonzero_ols_only,
        'oracle':'Direct public despread/whole complete-block sums/independent unwrap+weighted OLS/full-circle sincos/Fraction rounded F4 replay and exact gates; separately cross-checked against software startup model',
        'actual_fixture_jobs':sum(j['source'] is not None for j in jobs),'actual_fixture_results':[{'name':j['name'],'source':j['source'],'result':j['result']} for j in jobs if j['source'] is not None],
        'quality_exact_boundary':None if (a.mode_switch_only or a.nonzero_ols_only) else 'Real signed21 input: despread I=2 and Q=[4,-4,2,-2,2,-2,0,0], repeated. Exact .40 equality and both sides, M128/448 at scales1/250000.',
        'domain_injections':None if (a.mode_switch_only or a.nonzero_ols_only) else '6 exact +/-20kHz numerator boundaries inject selected numerator after input/phase estimation; 1 strict positive-E test injects E0. These supplement ADC-derived tests and are not measured RF samples.',
        'negative_controls_rejected':['phase step bit','quality sum bit','coherent sum bit','public training sign corruption'],
        'startup_commit_budget_cycles_at_4MSPS':6400,'latency_reference':'last core input handshake to first terminal out_valid; excludes frontend/interpolation/request scheduling and downstream final NCO configuration','end_to_end_startup_deadline_verified':False,'routed_OOC':None,'g0_passed':False,'new_RF_transmissions':False,
        'limitations':['configured startup arithmetic only; no actual scheduling or downstream final NCO rational descriptor hardware','floating calibration/notch remain outside RTL','full receiver fit/throughput/HDMI/video/RF acceptance not established']}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    if not a.sim_only:complete_route(out,bp,sources,run,receipt)
    print(json.dumps({'artifact':str(out),'measured':receipt['measured'],'routed_OOC':receipt['routed_OOC']}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--fixture',type=Path,action='append',default=[]);p.add_argument('--normal-fixture',type=Path,action='append',default=[]);p.add_argument('--mode-switch-only',action='store_true');p.add_argument('--nonzero-ols-only',action='store_true');p.add_argument('--sim-only',action='store_true');p.add_argument('--route-existing',action='store_true')
    a=p.parse_args()
    route_existing(a) if a.route_existing else main(a)
