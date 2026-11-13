#!/usr/bin/env python3
"""Independent full-precision payload phase/rotation/ML candidate from passed RRC/H.
Produces expected values only, no production profile or full RF/video acceptance.
"""
import argparse,hashlib,json,math
from pathlib import Path
import numpy as np
from scipy.signal import savgol_filter
from bpsk_header_probe import unpack
from g0_packet_probe import parse_link,PACKET_BYTES
from workspace_storage_guard import check as disk_check,GIB
Q=1<<32
SIGNS=np.array([[1-2*((c>>b)&1) for b in range(4)] for c in range(16)],dtype=np.int64)
COS=np.rint(65536*np.cos(np.arange(4096)*2*np.pi/4096)).astype(np.int64)
SIN=np.rint(65536*np.sin(np.arange(4096)*2*np.pi/4096)).astype(np.int64)

def signed(v,b):return (int(v)+(1<<(b-1)))%(1<<b)-(1<<(b-1))
def round_ratio(n,d):return (np.asarray(n,dtype=np.int64)+d//2)//d

def interpolate_phase(left,right,tx,last=False):
 # Exact convex interpolation. TX1 pilot is one symbol after TX0's pilot.
 n=np.arange(2-tx,66-tx,dtype=np.int64)
 if last:return np.full(64,left,dtype=np.int64)
 return round_ratio(left*66+(right-left)*n,66)

def rotate(h,y,phases):
 # H is [TX,RX,IQ] signed26/scale64, y is [symbol,RX,IQ] signed21/scale4.
 bins=(((phases.astype(np.uint64)+0x80000)&0xffffffff)>>20).astype(np.int64)
 co=COS[bins];si=SIN[bins];effective=np.empty((len(y),2,2,2),dtype=np.int64)
 for tx in (0,1):
  for rx in (0,1):
   effective[:,rx,tx,0]=round_ratio(h[tx,rx,0]*co[:,tx]-h[tx,rx,1]*si[:,tx],65536)
   effective[:,rx,tx,1]=round_ratio(h[tx,rx,0]*si[:,tx]+h[tx,rx,1]*co[:,tx],65536)
 scaled=round_ratio(y*92682,4096) # sqrt(2)*16, Q12 constant, no H precision drop.
 assert np.all(effective>=-(1<<26)) and np.all(effective<(1<<26))
 assert np.all(scaled>=-(1<<25)) and np.all(scaled<(1<<25))
 return scaled.reshape(-1,4),effective.reshape(-1,8)

def ml(scaled,effective):
 h=effective.reshape(-1,2,2,2);y=scaled.reshape(-1,2,2);metrics=np.zeros((len(y),16),dtype=np.int64)
 for c,s in enumerate(SIGNS):
  pr=h[:,:,0,0]*s[0]-h[:,:,0,1]*s[1]+h[:,:,1,0]*s[2]-h[:,:,1,1]*s[3]
  pi=h[:,:,0,1]*s[0]+h[:,:,0,0]*s[1]+h[:,:,1,1]*s[2]+h[:,:,1,0]*s[3]
  er=y[:,:,0]-pr;ei=y[:,:,1]-pi;assert np.max(abs(er))<1<<29 and np.max(abs(ei))<1<<29
  metrics[:,c]=np.sum(er*er+ei*ei,axis=1)
 d=np.argmin(metrics,axis=1);return d,metrics[np.arange(len(d)),d]

def float_ml(y,h,phases):
 effective=(h[:,:,0]+1j*h[:,:,1]).T[None,:,:]*np.exp(1j*phases)[:,None,:]/64
 samples=(y[:,:,0]+1j*y[:,:,1])/4;metrics=[]
 for s in SIGNS:
  x=np.array([s[0]+1j*s[1],s[2]+1j*s[3]])/np.sqrt(2)
  residual=samples-np.sum(effective*x[None,None,:],axis=2);metrics.append(np.sum(abs(residual)**2,axis=1))
 return np.argmin(np.array(metrics).T,axis=1)

def packet_results(decisions,session,sf):
 result=[];payloads=[]
 for tx in (0,1):
  bits=np.stack([(decisions>>(2*tx))&1,(decisions>>(2*tx+1))&1],axis=1).astype(np.uint8).ravel();data=np.packbits(bits,bitorder='big').tobytes();assert len(data)==4384;payloads.append(data)
  for slot in range(8):
   seq=sf*16+slot*2+tx;packet=data[slot*PACKET_BYTES:(slot+1)*PACKET_BYTES];payload,error=parse_link(packet,session,sf,seq);result.append({'seq':seq,'tx':tx,'slot':slot,'accepted':payload is not None,'error':error})
 return result,payloads

def frames(reference,phase_reference,header_reference):
 meta=json.loads((reference/'fixture_metadata.json').read_text());filtered=np.memmap(reference/'filtered.hex',dtype='S21',mode='r');curves=np.array([int(s,16) for s in (phase_reference/'pilot_smooth.hex').read_text().splitlines()],dtype=np.uint64);headers=json.loads((header_reference/'receipt.json').read_text())['actual_capture_headers']
 for m,model in enumerate(meta['models']):
  for w in range(1,4):
   e=model['windows'][w];h=np.array(unpack(e['H']&((1<<104)-1),26)+unpack(e['H']>>104,26),dtype=np.int64).reshape(2,2,2);steps=np.array([signed(e['phase_steps']>>(32*t),32) for t in (0,1)],dtype=np.int64);sg=np.zeros((274,2),dtype=np.int64)
   for v in curves[(m*4+w-1)*548:(m*4+w)*548]:
    v=int(v);i=(v>>41)&511;tx=(v>>59)&1;sg[i,tx]=signed(v,41)
   offsets=(1538+66*np.arange(274)[:,None]+np.arange(64)[None,:]).ravel();coordinates=[e['peak']+int(i)*e['step'] for i in offsets];y=[]
   for c in coordinates:
    tag,mu=divmod((c+32768)//65536,65536);a=unpack(int(filtered[model['offset']+tag+20],16),20);b=unpack(int(filtered[model['offset']+tag+21],16),20);y.append([(2*(x*(65536-mu)+z*mu)+32768)//65536 for x,z in zip(a,b)])
   y=np.array(y,dtype=np.int64).reshape(-1,2,2);residual=np.column_stack([np.concatenate([interpolate_phase(sg[g,t],sg[min(g+1,273),t],t,g==273) for g in range(274)]) for t in (0,1)])
   phases=(offsets[:,None]*steps[None,:]+residual)&0xffffffff
   # Independent continuous-angle pilot estimator on the SAME integer upstream H/y.
   floating=[]
   for t in (0,1):
    raw=[]
    for g in range(274):
     k=1536+66*g+t;c=e['peak']+k*e['step'];tag,mu=divmod((c+32768)//65536,65536);a=unpack(int(filtered[model['offset']+tag+20],16),20);b=unpack(int(filtered[model['offset']+tag+21],16),20);r=np.array([(2*(x*(65536-mu)+z*mu)+32768)//65536 for x,z in zip(a,b)],dtype=np.int64).reshape(2,2);z=np.sum(np.conj(h[t,:,0]+1j*h[t,:,1])*(r[:,0]+1j*r[:,1]));raw.append(np.angle(z*np.exp(-2j*np.pi*steps[t]*k/Q)))
    smooth=savgol_filter(np.unwrap(raw),7,1);floating.append(offsets*steps[t]*2*np.pi/Q+np.interp(offsets,1536+66*np.arange(274)+t,smooth))
   scaled,heff=rotate(h,y,phases);d,metric=ml(scaled,heff);df=float_ml(y,h,np.column_stack(floating));dp=float_ml(y,h,phases*2*np.pi/Q)
   head=next(a for a in headers if a['model']==m and a['window']==w);rawhead=bytes.fromhex(head['raw_header']);session=int.from_bytes(rawhead[4:8],'little');sf=int.from_bytes(rawhead[8:12],'little')
   yield {'model':m,'window':w,'entry':e,'H':h,'steps':steps,'sg':sg,'offsets':offsets,'samples':y,'phases':phases,'scaled':scaled,'effective':heff,'decision':d,'metric':metric,'float_decision':df,'fixed_phase_float_decision':dp,'session':session,'superframe':sf}

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);before=disk_check('before-payload-reference');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  rows=[];total=0;extra=[]
  for f in frames(a.reference.resolve(),a.phase_reference.resolve(),a.header_reference.resolve()):
   r,p=packet_results(f['decision'],f['session'],f['superframe']);rf,pf=packet_results(f['float_decision'],f['session'],f['superframe']);rp,pp=packet_results(f['fixed_phase_float_decision'],f['session'],f['superframe']);new=[v['seq'] for v,g in zip(r,rf) if g['accepted'] and not v['accepted']];extra.extend((f['model'],f['window'],seq) for seq in new)
   for v,g,raw,base in zip(r,rf,[p[v['tx']][v['slot']*548:(v['slot']+1)*548] for v in r],[pf[v['tx']][v['slot']*548:(v['slot']+1)*548] for v in r]):
    if v['accepted'] and g['accepted']:assert raw==base
   row={'model':f['model'],'window':f['window'],'superframe':f['superframe'],'symbols':len(f['decision']),'decision_differences_from_float':int(np.count_nonzero(f['decision']!=f['float_decision'])),'differences_from_fixed_phase_float_rotation':int(np.count_nonzero(f['decision']!=f['fixed_phase_float_decision'])),'float_good_packets':sum(r['accepted'] for r in rf),'fixed_phase_float_good_packets':sum(r['accepted'] for r in rp),'fixed_good_packets':sum(r['accepted'] for r in r),'additional_erasures':new};rows.append(row);total+=len(f['decision']);print(json.dumps(row),flush=True)
  result={'scope':__doc__,'frames':rows,'symbols':total,'additional_erasures':extra,'numeric_definition':{'Y_signed_bits':26,'rotated_H_signed_bits':27,'error_signed_bits':30,'metric_unsigned_bits':62,'Y_sqrt2_times16_Q12':92682,'phase_ROM_bins':4096,'rounding':'nearest ties +infinity','ML_tie':'lowest nibble TX0real bit0/TX0imag bit1/TX1real bit2/TX1imag bit3'},'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'g0_passed':False};(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-payload-reference'),indent=2)+'\n')
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--phase-reference',type=Path,required=True);p.add_argument('--header-reference',type=Path,required=True);main(p.parse_args())
