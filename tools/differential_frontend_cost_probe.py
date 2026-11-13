#!/usr/bin/env python3
"""Independent ADC/lag4/E/direct127 correlation and512-peak reference.

Joint RTL, numeric component tests and routed cost are measured. Only public
reference coefficients enter gold. Power, energy, squares and ratio products
use Python integers; direct correlation fits int64 for full signed16 inputs.
"""
from dataclasses import dataclass
import json
import math
import operator
from pathlib import Path
import zlib
import numpy as np
from g0_packet_probe import public_prefix
from phase_angle_cost_probe import cordic_phase

MASK48=(1<<48)-1
MASK32=(1<<32)-1
DENOMINATOR_FACTOR=1979168


def pack(values,bits):
    return sum((operator.index(v)&((1<<bits)-1))<<(bits*i) for i,v in enumerate(values))


def public_weights():
    pre=public_prefix()[256:384,0]
    signs=[(1 if v.real>0 else -1,1 if v.imag>0 else -1) for v in pre]
    dr=[];di=[]
    for (ai,aq),(bi,bq) in zip(signs,signs[1:]):
        dr.append((ai*bi+aq*bq)//2);di.append((ai*bq-aq*bi)//2)
    assert len(dr)==127 and (sum(dr),sum(di))==(16,17)
    wr=tuple(127*v-16 for v in dr);wi=tuple(127*v-17 for v in di)
    assert sum(wr)==sum(wi)==0 and sum(a*a+b*b for a,b in zip(wr,wi))==DENOMINATOR_FACTOR
    return wr,wi


WR,WI=public_weights()


def lag4_product(previous,current):
    previous=tuple(map(operator.index,previous));current=tuple(map(operator.index,current))
    if len(previous)!=4 or len(current)!=4 or any(not -32768<=v<32768 for v in previous+current):raise ValueError('signed16 dual-complex input required')
    z=[];identity=0
    for rx in (0,1):
        ar,ai=previous[2*rx:2*rx+2];br,bi=current[2*rx:2*rx+2]
        z.extend((ar*br+ai*bi,ar*bi-ai*br))
        identity+=(ar*ar+ai*ai)*(br*br+bi*bi)
    power=sum(v*v for v in z)
    assert power==identity and 0<=power<=1<<63
    assert all(-(1<<32)<=v<1<<32 for v in z)
    return tuple(z),power


def direct_correlation(rows):
    """Chronological127 stride4 vectors -> sense0 RX0I/Q RX1I/Q, sense1."""
    if len(rows)!=127:raise ValueError('127 vectors required')
    result=[]
    for sense in (0,1):
        for rx in (0,1):
            real=imag=0
            for row,wr,wi in zip(rows,WR,WI):
                zi,zq=map(operator.index,row[2*rx:2*rx+2])
                real+=zi*wr+(-1 if sense else 1)*zq*wi
                imag+=zq*wr+(1 if sense else -1)*zi*wi
            result.extend((real,imag))
    assert all(-(1<<48)<=v<1<<48 for v in result)
    return tuple(result)


def normalize_angle(x,y):
    x,y=operator.index(x),operator.index(y)
    shift=max(0,max(abs(x),abs(y)).bit_length()-25)
    a,b=((x+(1<<(shift-1)))>>shift,(y+(1<<(shift-1)))>>shift) if shift else (x,y)
    assert -(1<<26)<=a<(1<<26) and -(1<<26)<=b<(1<<26)
    phase,error=cordic_phase(a,b)
    return {'x_s27':a,'y_s27':b,'shift':shift,'phase_u32':phase,'error':error}


@dataclass(frozen=True)
class Candidate:
    generation:int
    start_tag:int
    endpoint_tag:int
    correlations:tuple
    energy:int
    def __post_init__(self):
        object.__setattr__(self,'correlations',tuple(map(operator.index,self.correlations)))
        object.__setattr__(self,'energy',operator.index(self.energy))
    def numerator(self,sense):return sum(v*v for v in self.correlations[4*sense:4*sense+4])
    def exceeds_threshold(self,sense):return self.energy>0 and 5*self.numerator(sense)>DENOMINATOR_FACTOR*self.energy
    def selected_rx(self,sense):
        c=self.correlations[4*sense:4*sense+4]
        return int(c[2]*c[2]+c[3]*c[3]>c[0]*c[0]+c[1]*c[1])
    def score_observation(self,sense):return self.numerator(sense)/(DENOMINATOR_FACTOR*self.energy) if self.energy else 0.


def better(candidate,sense,ordinal,best,best_sense,best_ordinal):
    if best is None:return True
    if candidate.energy==0:return best.energy==0 and (sense,ordinal)<(best_sense,best_ordinal)
    if best.energy==0:return candidate.numerator(sense)>0 or (sense,ordinal)<(best_sense,best_ordinal)
    lhs=candidate.numerator(sense)*best.energy;rhs=best.numerator(best_sense)*candidate.energy
    return lhs>rhs if lhs!=rhs else (sense,ordinal)<(best_sense,best_ordinal)


class PeakWindowOracle:
    """One configured search; first strict .20 hit then512 contiguous scores."""
    def __init__(self):self.arm()
    def arm(self):
        self.previous=None;self.first=None;self.count=0;self.best=None
        self.best_sense=0;self.best_ordinal=0;self.done=False
    def accept(self,candidate):
        if self.done:return None
        p=self.previous
        contiguous=(p is not None and candidate.generation==p.generation and candidate.start_tag>>48==p.start_tag>>48 and
                    (candidate.start_tag&MASK48)==((p.start_tag+1)&MASK48))
        if not contiguous:self.first=None;self.count=0;self.best=None
        self.previous=candidate
        if self.first is None:
            if not any(candidate.exceeds_threshold(s) for s in (0,1)):return None
            self.first=candidate;self.count=0
        for sense in (0,1):
            if better(candidate,sense,self.count,self.best,self.best_sense,self.best_ordinal):
                self.best=candidate;self.best_sense=sense;self.best_ordinal=self.count
        self.count+=1
        if self.count!=512:return None
        self.done=True;b=self.best;s=self.best_sense;rx=b.selected_rx(s);c=b.correlations[4*s+2*rx:4*s+2*rx+2]
        angle=normalize_angle(*c);phase=angle['phase_u32'];signed=phase if phase<1<<31 else phase-(1<<32)
        nco_step=((signed if s else -signed)+2)//4
        return {'generation':b.generation,'start_tag':b.start_tag,'endpoint_tag':b.endpoint_tag,
                'first_hit_tag':self.first.start_tag,'last_scan_tag':candidate.start_tag,'sense':s,'selected_rx':rx,
                'C':list(b.correlations),'energy':b.energy,'numerator':b.numerator(s),'score_observation':b.score_observation(s),
                'selected_complex':list(c),'angle':angle,'coarse_NCO_step_s32':nco_step,'coarse_NCO_step_u32':nco_step&MASK32,
                'selected_phase_radians_observation':math.atan2(c[1],c[0]),'scan_positions':self.count,'best_ordinal':self.best_ordinal}


class ADCOracle:
    """Accepted-input reference; pipeline revocations are tracked by the TB."""
    def __init__(self):self.reset()
    def reset(self):
        self.generation=0;self.fences=0;self.previous=None;self.raw=[];self.z=[];self.power=[]
    def cancel(self):
        self.generation=(self.generation+1)&MASK32;self.previous=None;self.raw=[];self.z=[];self.power=[]
    def accept(self,tag,raw,start=False):
        tag=operator.index(tag);raw=tuple(map(operator.index,raw))
        if not 0<=tag<1<<64 or len(raw)!=4 or any(not -32768<=v<32768 for v in raw):raise ValueError('tag64/signed16 input')
        gap=(self.previous is None or start or tag>>48!=self.previous>>48 or (tag&MASK48)!=((self.previous+1)&MASK48))
        if gap:
            if self.previous is not None:self.fences+=1
            self.generation=(self.generation+1)&MASK32;self.raw=[];self.z=[];self.power=[]
        self.previous=tag;self.raw.append(raw)
        if len(self.raw)<5:return None,None
        z,p=lag4_product(self.raw[-5],raw);self.z.append(z);self.power.append(p);i=len(self.power)-1
        energy=sum(self.power[i-504 if i>=504 else i%4:i+1:4])
        assert 0<=energy<=127*(1<<63) and energy<1<<70
        result={'tag':tag,'generation':self.generation,'start':len(self.z)==1,'z':z,'power':p,
                'energy':energy,'window_valid':len(self.z)>=505,'fences':self.fences}
        if len(self.z)<505:return result,None
        c=direct_correlation(self.z[-505::4]);assert all(sum(v*v for v in c[4*s:4*s+4])<=DENOMINATOR_FACTOR*energy for s in (0,1))
        return result,Candidate(self.generation,(tag&~MASK48)|((tag-508)&MASK48),tag,c,energy)


def direct_array_oracle(adc):
    """Direct convolution; power/E use arbitrary precision at full rails."""
    adc=np.asarray(adc)
    if adc.ndim!=2 or adc.shape[1]!=4 or len(adc)<5 or not np.issubdtype(adc.dtype,np.integer) or np.any(adc< -32768) or np.any(adc>32767):raise ValueError('signed16[N,4] required')
    adc=adc.astype(np.int64);old=adc[:-4];new=adc[4:];z=np.empty_like(new)
    for rx in (0,1):
        ai,aq=old[:,2*rx],old[:,2*rx+1];bi,bq=new[:,2*rx],new[:,2*rx+1]
        z[:,2*rx]=ai*bi+aq*bq;z[:,2*rx+1]=ai*bq-aq*bi
    power=[sum(int(v)**2 for v in row) for row in z]
    energy=[sum(power[i-504 if i>=504 else i%4:i+1:4]) for i in range(len(power))]
    c=np.zeros((len(z),8),dtype=np.int64)
    for phase in range(4):
        seq=z[phase::4]
        if len(seq)<127:continue
        indices=np.arange(phase+504,len(z),4)
        for rx in (0,1):
            rr=np.convolve(seq[:,2*rx],np.array(WR[::-1],dtype=np.int64),mode='valid')
            ii=np.convolve(seq[:,2*rx+1],np.array(WI[::-1],dtype=np.int64),mode='valid')
            ir=np.convolve(seq[:,2*rx+1],np.array(WR[::-1],dtype=np.int64),mode='valid')
            ri=np.convolve(seq[:,2*rx],np.array(WI[::-1],dtype=np.int64),mode='valid')
            c[indices,2*rx]=rr+ii;c[indices,2*rx+1]=ir-ri
            c[indices,4+2*rx]=rr-ii;c[indices,5+2*rx]=ir+ri
    return z,power,energy,c


def synthetic_segments():
    rng=np.random.default_rng(170345);out=[]
    out.append({'name':'all-minimum-rails','adc':np.full((1200,4),-32768,dtype=np.int16)})
    out.append({'name':'all-zero','adc':np.zeros((1200,4),dtype=np.int16)})
    rail=np.array([-32768,-32767,-1,0,1,32766,32767],dtype=np.int64)
    a=rng.integers(-32768,32768,(1600,4),dtype=np.int64);a[:len(rail)**2]=np.array([(x,y,y,x) for x in rail for y in rail])
    out.append({'name':'rails-and-random','adc':a.astype(np.int16)})
    pre=public_prefix()[256:384,0];signs=np.column_stack((np.where(pre.real>0,1,-1),np.where(pre.imag>0,1,-1)))
    for conjugated in (False,True):
        for strong in (0,1):
            values=np.zeros((2600,4),dtype=np.int64)
            for n in range(len(values)):
                i,q=signs[(n//4)%128];q=-q if conjugated else q
                for rx in (0,1):values[n,2*rx:2*rx+2]=np.array([i,q])*(12000 if rx==strong else 3000)
            out.append({'name':f'public-repetition-sense{int(conjugated)}-RX{strong}','adc':values.astype(np.int16)})
    return out


def load_actual_fixture(path):
    path=Path(path).resolve();meta=json.loads(path.with_suffix('.json').read_text())
    with np.load(path,allow_pickle=False) as archive:arrays={k:archive[k].copy() for k in archive.files}
    return {'path':str(path),'crc32':f'{zlib.crc32(path.read_bytes()):08x}','metadata':meta,'arrays':arrays}


def selector_domain_cases():
    """Directed selector inputs, NOT claimed as signed16 ADC waveforms."""
    boundary=[]
    for extra in (9,8,7):
        rows=[(wr+240,wi+64,24,extra) for wr,wi in zip(WR,WI)]
        boundary.append(Candidate(1,0,508,direct_correlation(rows),sum(sum(v*v for v in row) for row in rows)))
    assert [c.exceeds_threshold(0) for c in boundary]==[False,False,True]
    assert 5*boundary[1].numerator(0)==DENOMINATOR_FACTOR*boundary[1].energy
    cases=[{'name':'strict-threshold-equal-does-not-trigger','candidates':[boundary[1]]*600,'expected_complete':False}]
    candidates=[]
    for i in range(544):
        c=boundary[0] if i<16 else boundary[1] if i<32 else boundary[2]
        candidates.append(Candidate(1,i,i+508,c.correlations,c.energy))
    cases.append({'name':'strict-threshold-first-above','candidates':candidates,'expected_complete':True})
    tie=[];ratio=[]
    for i in range(512):
        c,e=((0,0,0,0,1000,0,0,0),1) if i==0 else (((1000,0,1000,0,0,0,0,0),2) if i==10 else ((0,)*8,0))
        tie.append(Candidate(1,i,i+508,c,e))
        c,e=((1000000,0,0,0,0,0,0,0),1000000) if i==0 else ((1000000,1000000,0,0,0,0,0),4000000)
        ratio.append(Candidate(1,i,i+508,c,e))
    cases.extend([{'name':'sense-major-tie-RX0-tie-zero-energy','candidates':tie,'expected_complete':True},
                  {'name':'larger-numerator-lower-ratio','candidates':ratio,'expected_complete':True}])
    for case in cases:
        peak=PeakWindowOracle();winner=None
        for i,c in enumerate(case['candidates']):
            c=Candidate(1,i,i+508,c.correlations,c.energy);case['candidates'][i]=c
            winner=peak.accept(c) or winner
        assert (winner is not None)==case['expected_complete'];case['expected']=winner
    w=cases[2]['expected'];assert (w['start_tag'],w['sense'],w['selected_rx'])==(10,0,0)
    assert cases[3]['expected']['start_tag']==0
    return cases


def prepare_vectors(out,fixture_directory):
    segments=synthetic_segments()
    template=segments[3]['adc'].astype(np.int64)
    for direction in (-1,1):
        a=[]
        for n,row in enumerate(template):
            angle=direction*2*math.pi*n/16;c,s=math.cos(angle),math.sin(angle)
            values=[]
            for rx in (0,1):
                i,q=map(int,row[2*rx:2*rx+2]);values.extend((round(i*c-q*s),round(i*s+q*c)))
            a.append(values)
        segments.append({'name':f'synthetic-CFO-{direction}-250kHz','adc':np.array(a,dtype=np.int16)})
    for path in sorted(Path(fixture_directory).glob('packet-air-g12-long-r*.npz')):
        f=load_actual_fixture(path);m=f['metadata'];a=f['arrays']
        segments.append({'name':path.stem,'adc':a['adc'],'arm':4,'base':int(a['adc_tag'][0]),'capture_start':int(m['source']['capture_slice'][0]),'fixture':f})
    if len(segments)!=12:raise ValueError('three actual fixtures required')
    initial=[];metadata=[];offset=0
    with (out/'adc.hex').open('w') as af,(out/'z-energy.hex').open('w') as zf,(out/'correlation.hex').open('w') as cf:
        for number,segment in enumerate(segments):
            a=segment['adc'];z,p,e,c=direct_array_oracle(a);peak=PeakWindowOracle();winner=None;arm=segment.get('arm',0)
            for k in range(arm,len(a)-508):
                winner=peak.accept(Candidate(1,k,k+508,tuple(c[k+504]),e[k+504]))
                if winner:break
            for i,row in enumerate(a):
                af.write(f'{pack(row,16):016x}\n')
                zf.write(f'{((int(e[i-4])<<132)|pack(z[i-4],33)) if i>=4 else 0:051x}\n')
                cf.write(f'{pack(c[i-4],49) if i>=508 else 0:098x}\n')
            if 'fixture' in segment:
                m=segment['fixture']['metadata'];old=m['integer_candidates'][0];start=m['source']['capture_slice'][0]
                assert winner is not None and winner['first_hit_tag']+start==old['first_hit_index'] and winner['start_tag']+start==old['selected_start_index']
                assert winner['C']==old['C_s49'] and winner['energy']==old['E_u70'] and winner['numerator']==old['numerator']
                assert winner['angle']['phase_u32']==old['phase']['phase_u32'] and winner['coarse_NCO_step_u32']==old['phase']['coarse_NCO_step_u32']
            w=winner or {'first_hit_tag':0,'start_tag':0,'sense':0,'selected_rx':0,'angle':{'phase_u32':0,'shift':0},'coarse_NCO_step_u32':0,'selected_complex':[0,0],'energy':0,'numerator':0}
            initial.append(f"capture_starts[{number}]=48'd{segment.get('capture_start',0)&MASK48};offsets[{number}]={offset};lengths[{number}]={len(a)};arms[{number}]={arm};has_peak[{number}]={int(winner is not None)};firsts[{number}]={w['first_hit_tag']};peaks[{number}]={w['start_tag']};senses[{number}]={w['sense']};rxs[{number}]={w['selected_rx']};phases[{number}]=32'h{w['angle']['phase_u32']:08x};ncos[{number}]=32'h{w['coarse_NCO_step_u32']:08x};res[{number}]=49'h{w['selected_complex'][0]&((1<<49)-1):013x};ims[{number}]=49'h{w['selected_complex'][1]&((1<<49)-1):013x};nums[{number}]=100'd{w['numerator']};energies[{number}]=70'd{w['energy']};shifts[{number}]={w['angle']['shift']};")
            metadata.append({'name':segment['name'],'offset':offset,'length':len(a),'arm':arm,'winner':winner,
                'provenance':segment.get('fixture',{}).get('metadata',{'synthetic':True}),
                'fixture_path':segment.get('fixture',{}).get('path'),'fixture_crc32':segment.get('fixture',{}).get('crc32')})
            offset+=len(a)
    (out/'vectors.json').write_text(json.dumps(metadata,indent=2)+'\n')
    return offset,'\n'.join(initial),metadata


TB=r'''`timescale 1ns/1ps
module tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,iv=0,istart=0,cv=0,ready=0;
 reg [63:0] it=0,data=0,search=0;reg [31:0] cid=0,cgen=0;reg [47:0] origin=0;wire [31:0] raw_initial_phase;

 wire ir,cr,ov,os,orr,hr;wire [31:0] oid,og,raw_phase,nco,ag,nf,cf;
 wire [63:0] first_tag,raw_peak,last_tag,known_tag;wire signed [48:0] re,im;
 wire [99:0] num;wire [69:0] raw_energy;wire [5:0] shift;wire [2:0] error;
 integer fault=0;
 wire [31:0] initial_phase=raw_initial_phase ^ ((fault==4 && ov && !error)?32'd1:32'd0);
 wire [31:0] phase=raw_phase ^ ((fault==1 && ov && !error)?32'd1:32'd0);
 wire [69:0] energy=raw_energy ^ ((fault==2 && ov && !error)?70'd1:70'd0);
 wire [63:0] peak_tag=raw_peak ^ ((fault==3 && ov && !error)?64'd1:64'd0);
 differential_acquisition_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.in_valid(iv),.in_ready(ir),.in_start(istart),.in_tag(it),.in_data(data),
 .cfg_valid(cv),.cfg_ready(cr),.cfg_id(cid),.cfg_generation(cgen),.cfg_search_tag(search),.cfg_phase_origin_index(origin),
 .out_valid(ov),.out_ready(ready),.out_id(oid),.out_generation(og),.out_first_tag(first_tag),.out_peak_tag(raw_peak),.out_last_sample_tag(last_tag),.out_known_tag(known_tag),
 .out_sense(os),.out_rx(orr),.out_raw_phase(raw_phase),.out_nco_step(nco),.out_nco_phase(raw_initial_phase),.out_re(re),.out_im(im),.out_n(num),.out_energy(raw_energy),.out_shift(shift),.out_error(error),
 .active_generation(ag),.history_restart(hr),.input_fences(nf),.correlation_fences(cf));
 reg [63:0] adc[0:@LAST@];reg [201:0] ze[0:@LAST@];reg [391:0] cg[0:@LAST@];
 integer offsets[0:11],lengths[0:11],arms[0:11],firsts[0:11],peaks[0:11];reg has_peak[0:11],senses[0:11],rxs[0:11];
 reg [47:0] capture_starts[0:11];reg [31:0] phases[0:11],ncos[0:11];reg [48:0] res[0:11],ims[0:11];reg [99:0] nums[0:11];reg [69:0] energies[0:11];reg [5:0] shifts[0:11];
 reg [299:0] zq[0:131071];reg [557:0] cq[0:131071];reg [566:0] sq[0:131071];
 integer zh=0,zt=0,ch=0,ct=0,sh=0,st=0,cycle=0,segment=0,source=0,ordinal=0,cadence=8,stop_feed=0,feed_done=0;
 integer feed_begin=0,current_arms=0,next_arms=0,cfg_restart_valid=0,cfg_restart_invalid=0,epoch_errors=0,ignored_old_tails=0;
 integer cases=0,configs=0,terminals=0,consumed=0,success=0,errors=0,aborted=0,held_revoked=0;
 integer inputs=0,zgenerated=0,zchecked=0,cgenerated=0,cchecked=0,schecked=0,zdiscard=0,cdiscard=0,sdiscard=0;
 integer input_stalls=0,interval8=0,interval1213=0,last_input=0,held_cycles=0,held_window=0;
 integer local_fences=0,model_fences=0;reg [31:0] mg=0;reg have_tag=0;reg [63:0] previous_tag=0;
 integer known_cycle=-1,last_dep_cycle=-1,result_cycle=-1,first_cfg_cycle=0,angle_result_restart=0,angle_result_cancel=0;
 integer measurement_rows=0,old_generation_scores=0,phase_finish_restart=0,phase_finish_cancel=0,normalize_prep_cancel=0,origin_mutations=0;integer mf,n,which;
 integer compare_started=0,compare_committed=0,compare_aborted=0,compare_fenced=0;
 integer compare_cancel_mask=0,compare_reset_mask=0,compare_restart_mask=0,compare_overlap_checks=0;
 reg [63:0] base=0;reg job_active=0,seen=0,held=0,new_window;
 reg [694:0] expected=0,held_output=0;
 wire [694:0] observed={oid,og,first_tag,peak_tag,last_tag,known_tag,os,orr,phase,nco,initial_phase,re,im,num,energy,shift,error};
 reg [31:0] owner_generation=0;reg [63:0] owner_base=0;integer owner_segment=0;
 reg [557:0] cref;reg [99:0] n0,n1,chosen;reg [98:0] pair0;reg sense,rx,hit;
 reg [101:0] five;reg [91:0] den;reg [2:0] wordno;reg [48:0] er,ei;
 function [99:0] norm4(input [195:0] w);
  reg signed [48:0] a;reg signed [97:0] v;reg [99:0] total;integer k;
  begin total=0;for(k=0;k<4;k=k+1) begin a=w[k*49+:49];v=a*a;total=total+v;end norm4=total;end
 endfunction
 function [98:0] norm2(input [97:0] w);
  reg signed [48:0] a,b;reg signed [97:0] x,y;
  begin a=w[48:0];b=w[97:49];x=a*a;y=b*b;norm2={1'b0,x}+{1'b0,y};end
 endfunction
 always @(posedge clk) begin
  cycle=cycle+1;
  if(rst || cancel) begin
   if(dut.compare_phase!=0) compare_aborted=compare_aborted+1;
   if(job_active) begin if(seen) held_revoked=held_revoked+1;else aborted=aborted+1;end
   job_active=0;seen=0;held=0;zdiscard=zdiscard+(zt-zh);cdiscard=cdiscard+(ct-ch);sdiscard=sdiscard+(st-sh);
   zh=0;zt=0;ch=0;ct=0;sh=0;st=0;have_tag=0;last_input=0;
   if(rst) begin mg=0;model_fences=0;end else mg=mg+1;
  end else begin
   if(dut.owner_lost && dut.compare_phase!=0) compare_fenced=compare_fenced+1;
   if(!dut.owner_lost && dut.compare_phase==3) compare_committed=compare_committed+1;
   if(ag!==mg || nf!==model_fences) $fatal(1,"input generation/fence mismatch");
   if(held && (!ov || observed!==held_output)) $fatal(1,"held result changed");
   if(ov&&!seen) begin
    if(!job_active || observed!==expected) $fatal(1,"candidate mismatch case%0d error%0d got%h expected%h",cases,error,observed,expected);
    seen=1;terminals=terminals+1;result_cycle=cycle;
    if(error) errors=errors+1;else success=success+1;
   end
   held=ov&&!ready;if(held) begin held_output=observed;held_cycles=held_cycles+1;end
   if(ov&&ready) begin consumed=consumed+1;seen=0;held=0;job_active=0;end
   if(cv&&cr) begin configs=configs+1;job_active=1;owner_generation=cgen;first_cfg_cycle=cycle;end
   if(iv&&!ir) input_stalls=input_stalls+1;
   if(iv&&ir) begin
    new_window=!have_tag || istart || it!={previous_tag[63:48],previous_tag[47:0]+48'd1};
    if(hr!==new_window) $fatal(1,"same-edge history restart mismatch");
    if(new_window) begin
     if(have_tag) begin model_fences=model_fences+1;local_fences=local_fences+1;end
     mg=mg+1;
     if(held) held_window=held_window+1;
    end
    have_tag=1;previous_tag=it;inputs=inputs+1;
    if(last_input && ordinal!=0) begin
     if(cadence==8) begin if(cycle-last_input!=8) $fatal(1,"saturated ADC service");interval8=interval8+1;end
     if(cadence==12) begin if(cycle-last_input!=12+((ordinal-1)%2)) $fatal(1,"ADC12/13 missed");interval1213=interval1213+1;end
    end
    last_input=cycle;
    if(ordinal>=4) begin
     zq[zt]={mg,it,1'(ordinal==4),1'(ordinal>=508),ze[source][201:132],ze[source][131:0]};zt=zt+1;zgenerated=zgenerated+1;
    end
    if(ordinal>=508) begin
     cq[ct]={mg,{it[63:48],it[47:0]-48'd508},ze[source][201:132],cg[source]};ct=ct+1;cgenerated=cgenerated+1;
    end
    if(segment==owner_segment && base==owner_base && has_peak[segment]) begin
     if(ordinal==firsts[segment]+1019) last_dep_cycle=cycle;
     if(ordinal==firsts[segment]+1152) known_cycle=cycle;
    end
   end
   if(dut.frontend.z_valid && dut.frontend.z_ready) begin
    if(zh>=zt || {dut.frontend.generation,dut.frontend.endpoint,dut.frontend.z_start,dut.frontend.window_valid,dut.frontend.energy,dut.frontend.z}!==zq[zh]) $fatal(1,"lag4/power/energy oracle mismatch");
    zh=zh+1;zchecked=zchecked+1;
   end
   if(dut.metric_valid && dut.metric_ready) begin
    if(ch>=ct || {dut.metric_generation,dut.metric_tag,dut.metric_energy,dut.metric_c}!==cq[ch]) $fatal(1,"C/E alignment oracle mismatch");
    cref=cq[ch];ch=ch+1;cchecked=cchecked+1;
    if(dut.frontend.metadata[dut.frontend.head][165:102]!==cref[525:462]) $fatal(1,"FIFO metadata tag mismatch");
    n0=norm4(cref[195:0]);n1=norm4(cref[391:196]);sense=n1>n0;chosen=sense?n1:n0;
    pair0=norm2(sense?cref[293:196]:cref[97:0]);rx={1'b0,pair0}<(chosen-{1'b0,pair0});wordno={sense,rx,1'b0};
    er=cref[wordno*49+:49];ei=cref[(wordno+1)*49+:49];five=({2'b0,chosen}<<2)+{2'b0,chosen};den=cref[461:392]*22'd1979168;
    hit=cref[461:392]!=0 && five>{{10{1'b0}},den};
    sq[st]={n0,n1,chosen,cref[461:392],cref[525:462],cref[557:526],sense,rx,hit,er,ei};st=st+1;
   end
   if(dut.score_valid) begin
    if(sh>=st || {dut.score_n0,dut.score_n1,dut.score_n,dut.score_energy,dut.score_tag,dut.score_generation,dut.score_sense,dut.score_rx,dut.score_hit,dut.score_re,dut.score_im}!==sq[sh]) $fatal(1,"score exact integer oracle mismatch");
    sh=sh+1;schecked=schecked+1;
    if(job_active && dut.score_generation!=owner_generation) old_generation_scores=old_generation_scores+1;
    if(dut.state==1 && dut.score_generation!=owner_generation) ignored_old_tails=ignored_old_tails+1;
    if(dut.state==2 && !dut.owner_lost && dut.score_generation==dut.owner_generation &&
       dut.score_tag[63:48]==dut.search_tag[63:48]) begin
     if(dut.compare_phase!=0) $fatal(1,"shared cross multiplier still busy at next score");
     compare_started=compare_started+1;compare_overlap_checks=compare_overlap_checks+1;
    end
   end
  end
  #1;if((rst||cancel)&&(ov||cr||ir)) $fatal(1,"reset/cancel gates");
 end
 task clear_all;
  begin iv=0;istart=0;cv=0;ready=0;rst=1;repeat(2) @(negedge clk);rst=0;stop_feed=0;feed_done=0;feed_begin=0;known_cycle=-1;last_dep_cycle=-1;result_cycle=-1;end
 endtask
 function [31:0] exact_initial_phase(input [31:0] raw_phase,input sense,input [47:0] offset);
  reg signed [32:0] directed;reg signed [48:0] positive_offset;reg signed [81:0] product;
  begin directed={raw_phase[31],raw_phase};if(!sense) directed=-directed;positive_offset={1'b0,offset};product=directed*positive_offset;exact_initial_phase=(product+82'sd2)>>>2;end
 endfunction
 task set_expected(input integer code);
  reg [63:0] a,b,last,known;
  begin
   a={base[63:48],base[47:0]+48'(firsts[segment])};b={base[63:48],base[47:0]+48'(peaks[segment])};
   last={base[63:48],a[47:0]+48'd1019};known={base[63:48],a[47:0]+48'd1152};
   expected=code?{cid,cgen,628'd0,3'(code)}:{cid,cgen,a,b,last,known,senses[segment],rxs[segment],phases[segment],ncos[segment],exact_initial_phase(phases[segment],senses[segment],known[47:0]-origin),res[segment],ims[segment],nums[segment],energies[segment],shifts[segment],3'd0};
  end
 endtask
 task configure(input integer generation_offset,input integer code);
  begin
   cid=32'hfffffff0+configs*13;cgen=mg+generation_offset;search={base[63:48],base[47:0]+48'(arms[segment])};
   origin=base[47:0]-capture_starts[segment];owner_segment=segment;owner_base=base;set_expected(code);cv=1;
   @(posedge clk);while(!cr) @(posedge clk);
   @(negedge clk);cv=0;origin=~origin;origin_mutations=origin_mutations+1;
  end
 endtask
 task feed(input integer limit,input integer start_flag);
  integer k,delay;
  begin
   feed_done=0;
   for(k=feed_begin;k<limit && !stop_feed;k=k+1) begin
    source=offsets[segment]+k;ordinal=k;it={base[63:48],base[47:0]+48'(k)};data=adc[source];istart=(k==0)&&start_flag;iv=1;
    @(posedge clk);while(!ir) @(posedge clk);
    @(negedge clk);iv=0;istart=0;
    delay=cadence==12?12+(k%2):8;if(!stop_feed) repeat(delay-1) @(negedge clk);
   end
   feed_done=1;
  end
 endtask
 task accept_result;
  begin while(!ov) @(negedge clk);repeat(19) @(negedge clk);ready=1;@(posedge clk);@(negedge clk);ready=0;end
 endtask
 task revoke(input integer do_reset);
  begin stop_feed=1;while(!feed_done) @(negedge clk);iv=0;cv=0;if(do_reset) rst=1;else cancel=1;
   repeat(2) @(negedge clk);rst=0;cancel=0;repeat(5) @(negedge clk);if(ov) $fatal(1,"stale result after revoke");end
 endtask
 task normal(input integer s,input integer cad,input integer wrap,input integer hold_restart);
  begin
   clear_all;segment=s;cadence=cad;cases=cases+1;base={16'(cases+7),48'(wrap?48'hffffffffff80:1024)};configure(1,0);
   feed(lengths[segment],0);while(!ov) @(negedge clk);
   if(known_cycle<0 || last_dep_cycle<0 || result_cycle<0 || result_cycle>=known_cycle) $fatal(1,"coarse budget/dependency missing");
   $fwrite(mf,"%0d %0d %0d %0d %0d %0d %0d %0d %0d\n",cases,segment,cadence,last_dep_cycle,result_cycle,known_cycle,raw_initial_phase,nco,raw_phase);measurement_rows=measurement_rows+1;
   if(hold_restart) begin base={base[63:48],base[47:0]+48'd17};feed(650,1);end
   accept_result;repeat(30) @(negedge clk);
  end
 endtask
 task control(input integer mode);
  begin
   clear_all;segment=3;cadence=8;cases=cases+1;base={16'(cases+7),48'd1024};
   if(mode==0) begin configure(9,2);accept_result;end
   else begin
    configure(1,0);
    fork
     feed(mode>=12?1020:lengths[segment],0);
     begin
      if(mode==1) while(dut.state!=2 || dut.window_count<20) @(negedge clk);
      else if(mode==2) while(dut.state!=6) @(negedge clk);
      else if(mode==3 || mode==4) while(dut.state!=2 || dut.window_count<100) @(negedge clk);
      else if(mode==5 || mode==6) while(!ov) @(negedge clk);
      else if(mode>=12) while(dut.state!=7) @(negedge clk);
      else if(mode>=7) while(dut.state!=6) @(negedge clk);
      stop_feed=1;while(!feed_done) @(negedge clk);
      if(mode==1 || mode==2 || mode==9) begin
       if(mode==2) while(!dut.angle_valid) @(negedge clk);
       if(mode==9) while(dut.state!=8) @(negedge clk);
       set_expected(1);base={base[63:48],base[47:0]+48'd100};source=offsets[segment];ordinal=0;it=base;data=adc[source];istart=1;iv=1;last_input=0;
       @(posedge clk);if(!ir) $fatal(1,"restart not accepted");if(mode==2) begin if(!dut.angle_valid) $fatal(1,"publication restart overlap");angle_result_restart=angle_result_restart+1;end
       if(mode==9) begin if(dut.state!=8) $fatal(1,"PHASE_FINISH restart missing");phase_finish_restart=phase_finish_restart+1;end
       @(negedge clk);iv=0;istart=0;accept_result;
      end else begin
       if(mode==7 || mode==8) begin while(!dut.angle_valid) @(negedge clk);angle_result_cancel=angle_result_cancel+1;end
       if(mode==10 || mode==11) begin while(dut.state!=8) @(negedge clk);phase_finish_cancel=phase_finish_cancel+1;end
       if(mode>=12) normalize_prep_cancel=normalize_prep_cancel+1;
       revoke(mode==4 || mode==6 || mode==8 || mode==11 || mode==13);
      end
     end
    join
   end
   repeat(30) @(negedge clk);
  end
 endtask
 task compare_control(input integer target_phase,input integer action);
  begin
   clear_all;segment=3;cadence=8;cases=cases+1;base={16'(cases+70),48'd1027};configure(1,0);
   fork
    feed(lengths[segment],0);
    begin
     while(dut.state!=2 || dut.window_count<20) @(negedge clk);
     stop_feed=1;while(!feed_done) @(negedge clk);
     while(dut.compare_phase!=target_phase || (action==2 && !ir)) @(negedge clk);
     if(action==2) begin
      set_expected(1);base={base[63:48],base[47:0]+48'd100};
      source=offsets[segment];ordinal=0;it=base;data=adc[source];istart=1;iv=1;last_input=0;
      @(posedge clk);
      if(!ir || !hr || dut.compare_phase!=target_phase) $fatal(1,"shared compare/restart overlap missing");
      compare_restart_mask=compare_restart_mask|(1<<target_phase);
      @(negedge clk);iv=0;istart=0;accept_result;
     end else begin
      if(action==0) compare_cancel_mask=compare_cancel_mask|(1<<target_phase);
      else compare_reset_mask=compare_reset_mask|(1<<target_phase);
      revoke(action==1);
     end
    end
   join
   repeat(30) @(negedge clk);
  end
 endtask
 task generation_arming(input integer next_owner);
  begin
   clear_all;segment=3;cadence=8;cases=cases+1;base={16'(cases+7),48'd1024};
   feed(600,0);last_input=0;
   if(next_owner) begin
    configure(1,0);next_arms=next_arms+1;feed_begin=0;feed(lengths[segment],1);
   end else begin
    arms[3]=1024;firsts[3]=1024;peaks[3]=1024;
    configure(0,0);current_arms=current_arms+1;feed_begin=600;feed(lengths[segment],0);
   end
   accept_result;arms[3]=0;firsts[3]=0;peaks[3]=0;repeat(30) @(negedge clk);
  end
 endtask
 task simultaneous_cfg_restart(input integer valid_next);
  begin
   clear_all;segment=3;cadence=8;cases=cases+1;base={16'(cases+7),48'd1024};
   cid=32'hfffffff0+configs*13;cgen=valid_next?1:0;search=base;origin=base[47:0]-capture_starts[segment];owner_segment=segment;owner_base=base;set_expected(valid_next?0:2);
   cv=1;iv=1;source=offsets[segment];ordinal=0;it=base;data=adc[source];istart=1;
   @(posedge clk);if(!cr || !ir || !hr) $fatal(1,"cfg/restart coincidence missing");
   @(negedge clk);cv=0;origin=~origin;origin_mutations=origin_mutations+1;iv=0;istart=0;last_input=0;
   if(valid_next) begin cfg_restart_valid=cfg_restart_valid+1;feed_begin=1;feed(lengths[segment],0);end
   else cfg_restart_invalid=cfg_restart_invalid+1;
   accept_result;repeat(30) @(negedge clk);
  end
 endtask
 task epoch_mismatch;
  begin
   clear_all;segment=3;cadence=8;cases=cases+1;base={16'(cases+7),48'd1024};
   cid=32'hfffffff0+configs*13;cgen=1;search={base[63:48]+16'd1,base[47:0]};origin=base[47:0]-capture_starts[segment];owner_segment=segment;owner_base=base;set_expected(3);cv=1;
   @(posedge clk);if(!cr) $fatal(1,"epoch descriptor not ready");@(negedge clk);cv=0;origin=~origin;origin_mutations=origin_mutations+1;
   feed(650,0);accept_result;epoch_errors=epoch_errors+1;repeat(30) @(negedge clk);
  end
 endtask
 initial begin
  @INIT@
  n=$value$plusargs("FAULT=%d",fault);
  $readmemh("adc.hex",adc);$readmemh("z-energy.hex",ze);$readmemh("correlation.hex",cg);
  mf=$fopen(fault?"fault-timing.txt":"timing.txt","w");repeat(2) @(negedge clk);
  normal(3,8,0,1);normal(4,8,0,0);normal(5,8,0,0);normal(6,8,1,0);normal(7,8,0,0);normal(8,8,0,0);
  normal(9,12,0,0);normal(10,12,0,0);normal(11,12,0,0);
  for(which=0;which<14;which=which+1) control(which);
  for(which=1;which<=3;which=which+1) begin compare_control(which,0);compare_control(which,1);compare_control(which,2);end
  generation_arming(0);generation_arming(1);simultaneous_cfg_restart(1);simultaneous_cfg_restart(0);epoch_mismatch;
  clear_all;segment=0;cadence=8;cases=cases+1;base={16'd40,48'd1024};configure(1,0);feed(lengths[segment],0);repeat(50) @(negedge clk);if(ov || dut.state!=1) $fatal(1,"DC rail falsely acquired");revoke(0);
  clear_all;segment=1;cadence=8;cases=cases+1;base={16'd41,48'd1024};configure(1,0);feed(lengths[segment],0);repeat(50) @(negedge clk);if(ov || dut.state!=1) $fatal(1,"zero falsely acquired");revoke(1);
  if(configs!=terminals+aborted || terminals!=consumed+held_revoked || zgenerated!=zchecked+zdiscard+(zt-zh) || cgenerated!=cchecked+cdiscard+(ct-ch) || cchecked!=schecked+sdiscard+(st-sh)) $fatal(1,"conservation failed");
  if(compare_started!=compare_committed+compare_aborted+compare_fenced ||
     compare_cancel_mask!=14 || compare_reset_mask!=14 || compare_restart_mask!=14 || compare_overlap_checks<10000)
   $fatal(1,"shared multiply ownership/coverage started%0d commit%0d abort%0d fence%0d masks%0d/%0d/%0d",compare_started,compare_committed,compare_aborted,compare_fenced,compare_cancel_mask,compare_reset_mask,compare_restart_mask);
  if(success!=14 || errors!=9 || current_arms!=1 || next_arms!=1 || cfg_restart_valid!=1 || cfg_restart_invalid!=1 || epoch_errors!=1 || ignored_old_tails<1 || aborted!=16 || held_revoked!=2 || angle_result_restart!=1 || angle_result_cancel!=2 || phase_finish_restart!=1 || phase_finish_cancel!=2 || normalize_prep_cancel!=2 || origin_mutations!=configs || measurement_rows!=9 || interval8<10000 || interval1213<200000 || held_window<1) $fatal(1,"coverage success%0d errors%0d abort%0d/%0d",success,errors,aborted,held_revoked);
  $display("G0_DIFFERENTIAL_ACQUISITION_PASS cases=%0d configs=%0d terminal=%0d success=%0d errors=%0d consumed=%0d aborted=%0d held_revoked=%0d ADC=%0d zgenerated=%0d zchecked=%0d cgenerated=%0d cchecked=%0d schecked=%0d zdiscard=%0d cdiscard=%0d sdiscard=%0d ADC8_intervals=%0d ADC1213_intervals=%0d input_stalls=%0d held_cycles=%0d held_window=%0d angle_result_restart=%0d angle_result_cancel=%0d measurement_rows=%0d old_generation_scores=%0d current_arms=%0d next_arms=%0d cfg_restart_valid=%0d cfg_restart_invalid=%0d epoch_errors=%0d ignored_old_tails=%0d phase_finish_restart=%0d phase_finish_cancel=%0d normalize_prep_cancel=%0d origin_mutations=%0d",cases,configs,terminals,success,errors,consumed,aborted,held_revoked,inputs,zgenerated,zchecked,cgenerated,cchecked,schecked,zdiscard,cdiscard,sdiscard,interval8,interval1213,input_stalls,held_cycles,held_window,angle_result_restart,angle_result_cancel,measurement_rows,old_generation_scores,current_arms,next_arms,cfg_restart_valid,cfg_restart_invalid,epoch_errors,ignored_old_tails,phase_finish_restart,phase_finish_cancel,normalize_prep_cancel,origin_mutations);
  $display("G0_SHARED_CROSS_PASS started=%0d committed=%0d aborted=%0d fenced=%0d cancel_mask=%0d reset_mask=%0d restart_mask=%0d score_overlap_checks=%0d",compare_started,compare_committed,compare_aborted,compare_fenced,compare_cancel_mask,compare_reset_mask,compare_restart_mask,compare_overlap_checks);
  $finish;
 end
 initial begin #2000000000;$fatal(1,"timeout");end
endmodule
'''



def concat_fields(fields):
    result=0
    for value,width in fields:result=(result<<width)|(int(value)&((1<<width)-1))
    return result


def score_word(c):
    n0,n1=c.numerator(0),c.numerator(1);sense=int(n1>n0);rx=c.selected_rx(sense)
    r,i=c.correlations[4*sense+2*rx:4*sense+2*rx+2]
    return concat_fields([(n0,100),(n1,100),(max(n0,n1),100),(c.energy,70),(c.start_tag,64),
                          (c.generation,32),(sense,1),(rx,1),(c.exceeds_threshold(sense),1),(r,49),(i,49)])


def initial_nco_phase(raw_phase,sense,offset):
    from fractions import Fraction
    p=int(raw_phase);p=p if p<1<<31 else p-(1<<32)
    directed=p if sense else -p;offset=int(offset)&MASK48
    exact=(directed*offset+2)//4
    assert exact==math.floor(Fraction(directed*offset,4)+Fraction(1,2))
    return exact&MASK32


def result_word(w,identity,origin):
    first=w['first_hit_tag'];base=first&~MASK48
    return concat_fields([(identity,32),(w['generation'],32),(first,64),(w['start_tag'],64),
      (base|((first+1019)&MASK48),64),(base|((first+1152)&MASK48),64),(w['sense'],1),(w['selected_rx'],1),
      (w['angle']['phase_u32'],32),(w['coarse_NCO_step_u32'],32),(initial_nco_phase(w['angle']['phase_u32'],w['sense'],((first+1152)&MASK48)-origin),32),
      (w['selected_complex'][0],49),(w['selected_complex'][1],49),(w['numerator'],100),(w['energy'],70),(w['angle']['shift'],6),(0,3)])


def prepare_auxiliary(out):
    # C/E-port range tests are separately labelled, not ADC-realizability claims.
    rng=np.random.default_rng(524431)
    cases=selector_domain_cases()
    vectors=[]
    for case in cases:
        for c in case['candidates'][:40]:vectors.append(c)
    rails=[-(1<<48),-(1<<48)+1,-1,0,1,(1<<48)-2,(1<<48)-1]
    for a in rails:
        for b in rails:
            vectors.append(Candidate(0,0,0,(a,b,b,a,a,a,b,b),(1<<70)-1))
            vectors.append(Candidate(0,0,0,(a,b,b,a,a,a,b,b),0))
    for k in range(160):vectors.append(Candidate(0,0,0,tuple(int(v) for v in rng.integers(-(1<<48),1<<48,8,dtype=np.int64)),int(rng.integers(0,1<<63,dtype=np.int64))<<7))
    vectors=[Candidate((0xf1234567+k)&MASK32,(0xabcd<<48)|((MASK48-100+k)&MASK48),0,c.correlations,c.energy) for k,c in enumerate(vectors)]
    for name,words,width in [('score-input.hex',[concat_fields([(c.generation,32),(c.start_tag,64),(c.energy,70),(pack(c.correlations,49),392)]) for c in vectors],558),
                              ('score-gold.hex',[score_word(c) for c in vectors],567)]:
        (out/name).write_text(''.join(f'{word:0{(width+3)//4}x}\n' for word in words))
    (out/'score_tb.sv').write_text(SCORE_TB.replace('@LAST@',str(len(vectors)-1)).replace('@COUNT@',str(len(vectors))))
    # Last candidate is included; a stronger 513th candidate must not extend a window.
    tail=[Candidate(0,k,k+508,(1000,0,0,0,0,0,0,0),1) for k in range(513)]
    tail[511]=Candidate(0,511,1019,(1000,1000,0,0,0,0,0,0),1)
    tail[512]=Candidate(0,512,1020,(10000,10000,0,0,0,0,0,0),1)
    cases.append({'name':'last-of-512-included-extra-513th-ignored','candidates':tail,'expected_complete':True})
    extremes=[Candidate(0,k,k+508,(-(1<<48),(1<<48)-1,0,0,0,0,0,0),(1<<69)) for k in range(512)]
    cases.append({'name':'signed49-negative-positive-rails-normalization','candidates':extremes,'expected_complete':True})
    for sense in (0,1):
        for p in [-(1<<31),-(1<<31)+1,-3,-2,-1,0,1,2,3,(1<<31)-1]:
            for distance in [0,1,2,3,4,1152,(1<<34)-1,1<<34,(1<<47)+3,MASK48]:
                c=[0]*8;c[4*sense]=1000
                cs=[Candidate(0,n,n+508,tuple(c),1) for n in range(512)]
                cases.append({'name':f'phase-domain-sense{sense}-raw{p}-offset{distance}','candidates':cs,'expected_complete':True,'phase_override':p&MASK32,'phase_offset':distance})
    inputs=[];initial=[];records=[];offset=0
    for number,case in enumerate(cases):
        base=(0x1234<<48)|((MASK48-100) if number in (2,4) else 1024)
        cs=[Candidate(0,(base&~MASK48)|((base+k)&MASK48),0,c.correlations,c.energy) for k,c in enumerate(case['candidates'])]
        peak=PeakWindowOracle();winner=None
        for c in cs:winner=peak.accept(c) or winner
        assert bool(winner)==case['expected_complete']
        if number==4:assert winner['best_ordinal']==511
        identity=0xffff0000+number
        origin=((base+1152-case['phase_offset'])&MASK48) if 'phase_offset' in case else ((base-83644)&MASK48)
        if 'phase_override' in case:
            winner['angle']['phase_u32']=case['phase_override']
            p=case['phase_override'];p=p if p<1<<31 else p-(1<<32)
            winner['coarse_NCO_step_u32']=(((p if winner['sense'] else -p)+2)//4)&MASK32
        initial.append(f"offsets[{number}]={offset};lengths[{number}]={len(cs)};bases[{number}]=64'h{base:016x};identities[{number}]=32'h{identity:08x};complete[{number}]={int(bool(winner))};origins[{number}]=48'h{origin:012x};inject_phase[{number}]={int('phase_override' in case)};injected_phase[{number}]=32'h{case.get('phase_override',0):08x};gold[{number}]=695'h{result_word(winner,identity,origin) if winner else 0:0174x};")
        inputs.extend(concat_fields([(c.generation,32),(c.start_tag,64),(c.energy,70),(pack(c.correlations,49),392)]) for c in cs)
        offset+=len(cs);records.append({'name':case['name'],'candidate_count':len(cs),'winner':winner,'phase_origin_index':origin,'injected_angle_phase':case.get('phase_override'),'scope':'direct C/E or angle-domain, not ADC-realizable waveform claim'})
    (out/'selector-input.hex').write_text(''.join(f'{word:0140x}\n' for word in inputs))
    (out/'selector_tb.sv').write_text(SELECTOR_TB.replace('@LAST@',str(len(inputs)-1)).replace('@CASES@',str(len(cases))).replace('@INIT@','\n'.join(initial)))
    (out/'component-vectors.json').write_text(json.dumps({'score_vectors':len(vectors),'selector_cases':records},indent=2)+'\n')
    return {'score_vectors':len(vectors),'selector_cases':len(cases),'angle_domain_initial_phase_cases':200,'scope':'direct C/E arithmetic boundary tests supplement, not replace, input-derived joint ADC checks'}


SCORE_TB=r'''`timescale 1ns/1ps
module score_tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,iv=0,ready=1;reg [557:0] input_word=0;
 wire ir,ov,sense,rx,hit;wire [99:0] n0,n1,n;wire [69:0] energy;wire [63:0] tag;wire [31:0] gen;wire signed[48:0] re,im;
 differential_score_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.in_valid(iv),.in_ready(ir),.in_c(input_word[391:0]),.in_energy(input_word[461:392]),.in_tag(input_word[525:462]),.in_generation(input_word[557:526]),
 .out_valid(ov),.out_ready(ready),.out_n0(n0),.out_n1(n1),.out_n(n),.out_energy(energy),.out_tag(tag),.out_generation(gen),.out_sense(sense),.out_rx(rx),.out_hit(hit),.out_re(re),.out_im(im));
 reg [557:0] vectors[0:@LAST@];reg [566:0] gold[0:@LAST@];reg [566:0] held_bits;
 wire [566:0] observed={n0,n1,n,energy,tag,gen,sense,rx,hit,re,im};
 integer cycle=0,sent=0,received=0,held_cycles=0,stalls=0,last_fire=0,ii8=0,k;reg held=0;
 always @(posedge clk) begin
  cycle=cycle+1;
  if(!rst && !cancel) begin
   if(held && (!ov || observed!==held_bits)) $fatal(1,"score held output changed");
   held=ov&&!ready;if(held) begin held_bits=observed;held_cycles=held_cycles+1;end
   if(ov&&ready) begin if(received>=sent || observed!==gold[received]) $fatal(1,"score Python exact oracle index%0d",received);received=received+1;end
   if(iv&&!ir) stalls=stalls+1;
   if(iv&&ir) begin
    if(sent>0 && sent<200) begin if(cycle-last_fire!=8) $fatal(1,"score saturated II mismatch");ii8=ii8+1;end
    last_fire=cycle;sent=sent+1;
   end
  end
 end
 always @(negedge clk) if(!rst) ready=(sent<200 || cycle%29>=11);
 initial begin
  $readmemh("score-input.hex",vectors);$readmemh("score-gold.hex",gold);repeat(3) @(negedge clk);rst=0;
  for(k=0;k<@COUNT@;k=k+1) begin input_word=vectors[k];iv=1;@(posedge clk);while(!ir) @(posedge clk);@(negedge clk);end
  iv=0;while(received<@COUNT@) @(negedge clk);
  if(sent!=@COUNT@ || received!=@COUNT@ || ii8!=199 || held_cycles<20 || stalls<100) $fatal(1,"score coverage");
  $display("G0_DIFFERENTIAL_SCORE_PASS inputs=%0d outputs=%0d saturated_II8_intervals=%0d held_cycles=%0d input_stalls=%0d",sent,received,ii8,held_cycles,stalls);$finish;
 end
 initial begin #2000000;$fatal(1,"score timeout");end
endmodule
'''


SELECTOR_TB=r'''`timescale 1ns/1ps
module selector_tb;
 reg clk=0;always #10 clk=~clk;
 reg rst=1,cancel=0,cv=0,ready=0,mv=0;reg [31:0] cid=0;reg [63:0] search=0;reg [47:0] origin=0;reg [31:0] phase_override=0;reg [557:0] input_word=0;wire [31:0] initial_phase;
 wire cr,ir,ov,sense,rx,hr;wire [31:0] oid,og,phase,nco,ag,nf,cf;wire [63:0] first_tag,peak_tag,last_tag,known_tag;wire signed[48:0] re,im;wire [99:0] num;wire[69:0] energy;wire[5:0] shift;wire[2:0] error;
 differential_acquisition_cost_probe dut(.clk(clk),.rst(rst),.cancel(cancel),.in_valid(1'b0),.in_ready(ir),.in_start(1'b0),.in_tag(64'b0),.in_data(64'b0),.cfg_valid(cv),.cfg_ready(cr),.cfg_id(cid),.cfg_generation(32'b0),.cfg_search_tag(search),.cfg_phase_origin_index(origin),
 .out_valid(ov),.out_ready(ready),.out_id(oid),.out_generation(og),.out_first_tag(first_tag),.out_peak_tag(peak_tag),.out_last_sample_tag(last_tag),.out_known_tag(known_tag),.out_sense(sense),.out_rx(rx),.out_raw_phase(phase),.out_nco_step(nco),.out_nco_phase(initial_phase),.out_re(re),.out_im(im),.out_n(num),.out_energy(energy),.out_shift(shift),.out_error(error),.active_generation(ag),.history_restart(hr),.input_fences(nf),.correlation_fences(cf));
 wire [694:0] observed={oid,og,first_tag,peak_tag,last_tag,known_tag,sense,rx,phase,nco,initial_phase,re,im,num,energy,shift,error};
 reg [557:0] vectors[0:@LAST@];integer offsets[0:@CASES@-1],lengths[0:@CASES@-1];reg [63:0] bases[0:@CASES@-1];reg[31:0] identities[0:@CASES@-1];reg complete[0:@CASES@-1];reg [694:0] gold[0:@CASES@-1],held_bits;reg [47:0] origins[0:@CASES@-1];reg inject_phase[0:@CASES@-1];reg [31:0] injected_phase[0:@CASES@-1];
 integer c,k,inputs=0,outputs=0,held_checks=0,nohit=0;
 initial begin
  @INIT@
  $readmemh("selector-input.hex",vectors);
  force dut.metric_valid=mv;force dut.metric_c=input_word[391:0];force dut.metric_energy=input_word[461:392];force dut.metric_tag=input_word[525:462];force dut.metric_generation=input_word[557:526];
  if(dut.corrective_step(32'd2,0)!==32'd0 || dut.corrective_step(-32'sd2,0)!==32'd1 || dut.corrective_step(32'd2,1)!==32'd1 || dut.corrective_step(-32'sd2,1)!==32'd0 || dut.corrective_step(32'h80000000,0)!==32'h20000000 || dut.corrective_step(32'h80000000,1)!==32'he0000000) $fatal(1,"signed quarter-turn ties/rail");
  for(c=0;c<@CASES@;c=c+1) begin
   rst=1;cv=0;ready=0;mv=0;repeat(3) @(negedge clk);rst=0;
   origin=origins[c];phase_override=injected_phase[c];if(inject_phase[c]) force dut.angle_phase=phase_override;else release dut.angle_phase;
   cid=identities[c];search=bases[c];cv=1;@(posedge clk);if(!cr) $fatal(1,"selector cfg not ready");@(negedge clk);cv=0;origin=~origin;
   for(k=0;k<lengths[c];k=k+1) begin
    input_word=vectors[offsets[c]+k];mv=1;@(posedge clk);while(!dut.metric_ready) @(posedge clk);inputs=inputs+1;@(negedge clk);
   end
   mv=0;
   if(complete[c]) begin
    while(!ov) @(negedge clk);
    if(observed!==gold[c]) $fatal(1,"selector Python oracle case%0d got%h expected%h",c,observed,gold[c]);outputs=outputs+1;held_bits=observed;
    repeat(31) begin @(negedge clk);if(!ov || observed!==held_bits || cr) $fatal(1,"selector held result");held_checks=held_checks+1;end
    ready=1;@(negedge clk);ready=0;
   end else begin repeat(80) @(negedge clk);if(ov || dut.state!=1) $fatal(1,"equal threshold acquired");nohit=nohit+1;end
  end
  if(outputs!=@CASES@-1 || nohit!=1) $fatal(1,"selector coverage");
  $display("G0_DIFFERENTIAL_SELECTOR_PASS cases=%0d direct_metric_inputs=%0d outputs=%0d equal_threshold_nohit=%0d held_checks=%0d quarter_step_boundaries=6",@CASES@,inputs,outputs,nohit,held_checks);$finish;
 end
 initial begin #20000000;$fatal(1,"selector timeout");end
endmodule
'''



def route_receipt(out,strict_parser):
    """Keep failed routing measurements as evidence, including singular Endpoint."""
    import re
    def warnings(result):
        from collections import Counter
        log=(out/'ooc.log').read_text()
        result['tool_warning_counts']=dict(sorted(Counter(re.findall(r'^WARNING: \[([^\]]+)\]',log,re.M)).items()))
        result['tool_critical_warning_count']=len(re.findall(r'^CRITICAL WARNING:',log,re.M))
        result['tool_error_count']=len(re.findall(r'^ERROR:',log,re.M))
        result['OOC_warning_scope']='External port partial routes and clock source/skew remain unverified (HD.PARTPIN_LOCS/HD.CLK_SRC); DSP pipeline and unused/trimmed register warnings retained. This is not full-board I/O/CDC sign-off.'
        return result
    try:
        result=strict_parser(out);result['accepted']=True;return warnings(result)
    except ValueError as error:
        utilization=(out/'utilization.rpt').read_text();timing=(out/'timing.rpt').read_text();drc=(out/'drc.rpt').read_text()
        result={'part':'xc7z020clg484-1','accepted':False,'validation_error':str(error),'full_design_IO_CDC_and_timing_verified':False}
        for label,key in [('Slice LUTs','LUT'),('Slice Registers','FF'),('LUT as Logic','LUT_logic'),('LUT as Memory','LUT_memory'),('RAMB36/FIFO*','RAMB36'),('RAMB18','RAMB18'),('DSPs','DSP')]:
            match=re.search(r'\|\s*'+re.escape(label)+r'\s*\|\s*(\d+)\s*\|',utilization)
            if match:result[key]=int(match[1])
        result['BRAM36_equivalent']=result['RAMB36']+result['RAMB18']/2
        for kind,slack,total in [('Setup','WNS_ns','TNS_ns'),('Hold','WHS_ns','THS_ns')]:
            match=re.search(kind+r'\s*:\s*(\d+)\s+Failing Endpoints?\s*,\s+Worst Slack\s+([-.\d]+)ns,\s+Total Violation\s+([-.\d]+)ns',timing)
            if match:result.update({kind+'_failing_endpoints':int(match[1]),slack:float(match[2]),total:float(match[3])})
        for check in ('unconstrained_internal_endpoints','no_input_delay','no_output_delay','no_clock'):
            match=re.search(r'checking '+check+r' \((\d+)\)',timing)
            result[check]=int(match[1]) if match else None
        result['DRC_warning_ids']=sorted(set(re.findall(r'^([A-Z0-9-]+)#\d+ Warning',drc,re.M)))
        result['severe_DRC']=len(re.findall(r'\|\s*(Critical Warning|Error)\s*\|',drc))
        return warnings(result)


def main(a):
    import re
    import shutil
    import subprocess
    from fec_inverse_cost_probe import routed_measurements
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    total,initial,metadata=prepare_vectors(out,a.fixture_directory)
    auxiliary=prepare_auxiliary(out)
    tb=TB.replace('@LAST@',str(total-1)).replace('@INIT@',initial)
    (out/'tb.sv').write_text(tb)
    project=Path(__file__).resolve().parents[1]
    names=('differential_input_cost_probe.sv','differential_corr_cost_probe.sv','differential_frontend_cost_probe.sv','differential_score_cost_probe.sv','phase_angle_cost_probe.sv','differential_acquisition_cost_probe.sv')
    sources=[]
    for name in names:
        p=out/name;shutil.copyfile(project/'sim/g0'/name,p);sources.append(p)
    bp=a.vivado_bin.resolve();commands=[]
    def run(name,args):
        commands.append(args);(out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
    run('compile',[str(bp/'xvlog'),'--sv',*[str(p) for p in sources],str(out/'tb.sv')])
    run('elaborate',[str(bp/'xelab'),'tb','-s','differential_acquisition_tb','-mt','off'])
    run('simulate',[str(bp/'xsim'),'differential_acquisition_tb','-runall','-testplusarg','FAULT=0'])
    log=(out/'simulate.log').read_text()
    if 'G0_DIFFERENTIAL_ACQUISITION_PASS' not in log or 'G0_SHARED_CROSS_PASS' not in log or 'Fatal:' in log:raise ValueError('joint simulation failed')
    measured={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',log.split('G0_DIFFERENTIAL_ACQUISITION_PASS')[-1].split('\n')[0])}
    shared_cross={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',log.split('G0_SHARED_CROSS_PASS')[-1].split('\n')[0])}
    timing=[]
    for line in (out/'timing.txt').read_text().splitlines():
        case,segment,cadence,last,result,known,initial_phase,nco_step,raw_phase=map(int,line.split())
        timing.append({'case':case,'segment':segment,'ADC_cadence':cadence,'last_required_input_cycle':last,'candidate_result_cycle':result,'software_known_input_cycle':known,'processing_cycles_after_last_input':result-last,'margin_before_known_cycles':known-result,'raw_phase_u32':raw_phase,'coarse_NCO_step_u32':nco_step,'initial_NCO_phase_u32':initial_phase})
    for row in timing:
        if row['segment']>=9:
            source=metadata[row['segment']];w=source['winner'];capture_start=source['provenance']['source']['capture_slice'][0]
            offset=capture_start+w['first_hit_tag']+1152
            expected_phase=initial_nco_phase(w['angle']['phase_u32'],w['sense'],offset)
            if row['initial_NCO_phase_u32']!=expected_phase or row['coarse_NCO_step_u32']!=w['coarse_NCO_step_u32']:raise AssertionError('actual fixture capture-origin phase/step mismatch')
            row['capture_origin_index']=0;row['capture_relative_apply_index']=offset
    if len(timing)!=measured['measurement_rows'] or len(timing)!=9:raise AssertionError('normal timing evidence missing')
    if any(row['margin_before_known_cycles']<=0 for row in timing):raise AssertionError('candidate missed known-index deadline')
    baseline=None;comparison=None
    if a.baseline_receipt:
        baseline=json.loads(a.baseline_receipt.read_text())
        old_rows={(row['case'],row['segment']):row for row in baseline['timing']}
        changes=[]
        for row in timing:
            old=old_rows[(row['case'],row['segment'])]
            for field in ('ADC_cadence','raw_phase_u32','coarse_NCO_step_u32','initial_NCO_phase_u32'):
                if row[field]!=old[field]:raise AssertionError('shared multiply changed '+field)
            delta=row['processing_cycles_after_last_input']-old['processing_cycles_after_last_input']
            if delta!=2:raise AssertionError('unexpected shared-comparison latency change')
            changes.append({'case':row['case'],'segment':row['segment'],'extra_cycles':delta,
                            'margin_before_known_cycles':row['margin_before_known_cycles']})
        comparison={'baseline_receipt':str(a.baseline_receipt.resolve()),
                    'normal_result_phase_step_and_initial_phase_bit_exact':True,
                    'all_other_result_fields_checked_against_unchanged_independent_oracle':True,
                    'timing_changes':changes,'routed_resource_delta':None}
    (out/'normal-timing.json').write_text(json.dumps(timing,indent=2)+'\n')
    for fault in (1,2,3,4):
        run('fault'+str(fault),[str(bp/'xsim'),'differential_acquisition_tb','-runall','-testplusarg','FAULT='+str(fault)])
        log=(out/('fault'+str(fault)+'.log')).read_text()
        if 'Fatal: candidate mismatch' not in log or 'G0_DIFFERENTIAL_ACQUISITION_PASS' in log:raise ValueError('negative control was not rejected')
    for top,token in [('score','G0_DIFFERENTIAL_SCORE_PASS'),('selector','G0_DIFFERENTIAL_SELECTOR_PASS')]:
        run(top+'-compile',[str(bp/'xvlog'),'--sv',str(out/(top+'_tb.sv'))])
        run(top+'-elaborate',[str(bp/'xelab'),top+'_tb','-s',top+'_tb','-mt','off'])
        run(top+'-simulate',[str(bp/'xsim'),top+'_tb','-runall'])
        log=(out/(top+'-simulate.log')).read_text()
        if token not in log or 'Fatal:' in log:raise ValueError(top+' arithmetic simulation failed')
        auxiliary[top+'_measured']={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',log.split(token)[-1])}
    receipt={'scope':__doc__,'component_tests':auxiliary,'clock_hz':50000000,'measured':measured,'timing':timing,'actual_calibrated_prefixes':3,
        'shared_cross_multiplier':{'operand_bits':[100,70],'product_bits':170,'multiply_expressions':1,
                                  'score_service_interval':8,'comparison_commit_cycles_after_score':3,
                                  'measured':shared_cross},'comparison_to_baseline':comparison,
        'input_contract':'signed16 after floating calibration/notch for actual fixtures; synthetic rails also tested; not unchanged raw ADC',
        'oracle':'input-derived lag4 products/direct127 correlation/stride4 energy/exact rational512 peak; independent full-window sums, no DUT values supplied as gold',
        'negative_controls_rejected':['raw phase bit','energy bit','peak tag bit','initial NCO phase bit'],'routed_OOC':None,
        'limitations':['coarse candidate only, no fine-training quality/retry acceptance','no actual NCO dispatch/coherent timing/header/payload consumer','calibration/notch remains floating'],
        'g0_passed':False,'new_RF_transmissions':False}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    if not a.sim_only:
        (out/'ooc.tcl').write_text('set_param general.maxThreads 4\n'+''.join(f'read_verilog -sv {{{p}}}\n' for p in sources)+'''synth_design -top differential_acquisition_cost_probe -part xc7z020clg484-1 -mode out_of_context
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
write_checkpoint -force differential_acquisition_cost_probe.dcp
''')
        run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal'])
        receipt['routed_OOC']=route_receipt(out,routed_measurements);(out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        if not receipt['routed_OOC']['accepted']:raise ValueError('routed timing/validation failed; exact measurements retained in receipt')
        if baseline:
            comparison['routed_resource_delta']={key:receipt['routed_OOC'][key]-baseline['routed_OOC'][key]
                                                for key in ('LUT','FF','DSP','BRAM36_equivalent')}
            (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--fixture-directory',type=Path,required=True)
    p.add_argument('--vivado-bin',type=Path,required=True);p.add_argument('--sim-only',action='store_true')
    p.add_argument('--baseline-receipt',type=Path,help='Compare unchanged normal outputs and +2-cycle latency to pre-sharing phase-r3 evidence')
    main(p.parse_args())
