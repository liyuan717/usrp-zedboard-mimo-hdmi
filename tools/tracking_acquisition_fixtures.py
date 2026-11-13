#!/usr/bin/env python3
"""Independent integer fixtures for live coarse/NCO/RRC/shared-timing/training.
Expected public timing and channels are OUTPUT comparisons only, never DUT input.
"""
import argparse,hashlib,json,math,zlib
from pathlib import Path
import numpy as np
from g0_causal_acquisition import calibrated_input
from g0_fixed_differential_model import quantize_adc,coarse_nco_configuration
from g0_fixed_startup_model import estimate_startup
from g0_fixed_training_model import training_signs,estimate_training
from g0_packet_probe import public_prefix,PERIOD,HEADER_START,HEADER_SYMBOLS,DATA_START
from g0_sounder import rrc
from coherent_window_probe import metric,pack
from coherent_peak_probe import refine
from coherent_timing_probe import unpack
from causal_sfo_probe import Oracle,Q,MASK
from workspace_storage_guard import check as disk_check,GIB

MASK48=(1<<48)-1

def prepare(out,reference):
 old=json.loads((reference/'receipt.json').read_text());assert len(old['fixtures'])==6
 oldadc=(reference/'adc.hex').read_text().splitlines();oldmix=(reference/'mixed.hex').read_text().splitlines();oldrrc=(reference/'filtered.hex').read_text().splitlines()
 public=public_prefix();normal_signs=[training_signs(public,tx)[1] for tx in (0,1)]
 pre=public[256:448,0];csigns=np.column_stack((np.where(pre.real>0,1,-1),np.where(pre.imag>0,1,-1)))
 (out/'coherent_signs.hex').write_text(''.join(f'{int(i<0)+2*int(q<0):x}\n' for i,q in csigns))
 allsign=[]
 for tx,start in enumerate((384,896)):allsign.extend(int(v.real<0)+2*int(v.imag<0) for v in public[start:start+512,tx])
 (out/'training_signs.hex').write_text(''.join(f'{v:x}\n' for v in allsign))
 sine=np.array([round(65536*math.sin(2*math.pi*k/4096)) for k in range(4096)],dtype=np.int64)
 cosine=np.array([round(65536*math.cos(2*math.pi*k/4096)) for k in range(4096)],dtype=np.int64)
 taps=np.rint(rrc()*131072).astype(np.int64)
 (out/'sine.hex').write_text(''.join(f'{v:05x}\n' for v in sine[:1024]));(out/'rrc.hex').write_text(''.join(f'{v&0x3ffff:05x}\n' for v in taps[:21]))
 def mix(values,cfg,sense):
  phase=(np.arange(len(values),dtype=np.uint64)*np.uint64(cfg['step_u32'])+np.uint64(cfg['phase_u32']))&np.uint64(0xffffffff)
  bins=(((phase+np.uint64(0x80000))&np.uint64(0xffffffff))>>np.uint64(20)).astype(np.int64);co,si=cosine[bins],sine[bins]
  result=np.empty_like(values)
  for rx in (0,1):
   i=values[:,rx*2];q=values[:,rx*2+1]*(-1 if sense else 1)
   result[:,rx*2]=(i*co-q*si+16384)//32768;result[:,rx*2+1]=(i*si+q*co+16384)//32768
  return result
 def filter_rows(values):return np.column_stack([(np.convolve(values[:,l],taps,'full')[:len(values)]+65536)//131072 for l in range(4)])
 cache={};sources=[];models=[];initial=[];old_offset=0;adc_offset=0;model_offset=0;metric_rows=[];startup_rows=[];normal_rows=[];normal_tags=[];normal_mus=[]
 handles={n:(out/(n+'.hex')).open('w') for n in ['adc','mixed','filtered']}
 try:
  for m in range(7):
   f=old['fixtures'][m if m<6 else 0];sense=f['sense'];origin,oldend=f['source_slice'];k=f['coarse']['selected_start_index'];first=f['coarse']['first_hit_index'];known=first+1152;commit=k+2960;end=k+5*80000+7000
   capture=Path(f['capture'])
   if capture not in cache:
    raw=np.memmap(capture,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]),mode='r');assert f'{zlib.crc32(raw):08x}'==f['capture_crc32']
    iq=np.asarray(raw['iq'][:end],dtype=np.float64);y,_=calibrated_input(iq[:,0::2]+1j*iq[:,1::2],PERIOD);adc,_=quantize_adc(y);cache[capture]=adc
   values=cache[capture][origin:end].astype(np.int64).copy()
   if sense:values[:,1::2]*=-1
   assert np.max(values)<=32767 and np.min(values)>=-32768
   if m<6:
    prefix=unpack(oldadc[old_offset:old_offset+oldend-origin],16);assert np.array_equal(values[:len(prefix)],prefix)
    sources.append({'offset':adc_offset,'length':len(values),'origin':origin,'capture':str(capture),'capture_crc32':f['capture_crc32'],'sense':sense})
    for row in values:handles['adc'].write(f'{pack(row,16):016x}\n')
    adc_offset+=len(values)
   base=(1<<48)-71000;phase_origin=base if m==6 else (base-origin)&MASK48
   coarse=coarse_nco_configuration(f['coarse']['phase']['phase_u32'],sense,(base+known-origin)&MASK48,phase_origin)
   mixed=np.zeros_like(values);filtered=np.zeros_like(values)
   mixed[known-origin:commit-origin]=mix(values[known-origin:commit-origin],coarse,sense)
   filtered[known-origin:commit-origin]=filter_rows(mixed[known-origin:commit-origin])
   first_symbol=f['first_symbol'];count=f['count'];endpoints=k+20+4*(np.arange(first_symbol,864)-256)-origin
   startup=2*filtered[endpoints]
   fine=estimate_startup(startup,normal_signs[0][first_symbol-416:],first_symbol,coarse['step_u32'],(base+commit-origin)&MASK48,phase_origin)
   assert fine['accepted'],(m,fine)
   final=fine['final_nco_config'];mixed[commit-origin:]=mix(values[commit-origin:],final,sense);filtered[commit-origin:]=filter_rows(mixed[commit-origin:])
   assert np.max(filtered)<1<<19 and np.min(filtered)>=-(1<<19)
   if m<6:
    assert (coarse['step_u32'],coarse['phase_u32'])==(f['coarse_configuration']['step_u32'],f['coarse_configuration']['phase_u32'])
    assert (final['step_u32'],final['phase_u32'])==(f['final_configuration']['step_u32'],f['final_configuration']['phase_u32'])
    assert np.array_equal(mixed[:oldend-origin],unpack(oldmix[old_offset:old_offset+oldend-origin],18))
    assert np.array_equal(filtered[:oldend-origin],unpack(oldrrc[old_offset:old_offset+oldend-origin],20));old_offset+=oldend-origin
   for row in mixed:handles['mixed'].write(f'{pack(row,18):018x}\n')
   for row in filtered:handles['filtered'].write(f'{pack(row,20):020x}\n')
   so=len(startup_rows);startup_rows.extend(pack(row,21) for row in startup)
   retained=filtered[commit-origin+40:];first_center=commit-origin+20;next_peak=(k-origin+80000)*Q;oracle=Oracle();windows=[]
   for w in range(5):
    window=next_peak//Q-32;rows=[metric(retained,csigns,window-first_center+p) for p in range(65)];metric_offset=len(metric_rows);metric_rows.extend(rows)
    pairs=[((v>>116)&((1<<60)-1),v>>176) for v in rows];best,mu,error=refine(pairs);assert error==0,(m,w,best,error)
    peak=(window+best)*Q+(mu<<16);result=oracle.update(w,0,0,peak,0);assert result[-1]==0,(m,w,result)
    step=result[6];next_peak=result[5];entry={'window_first':window,'peak':peak,'training':result[4],'next':next_peak,'step':step,'used':result[8],
     'metric_offset':metric_offset,'normal_offset':len(normal_rows),'header_right':(peak+(HEADER_START-256)*step+32768)//Q+1,'data_right':(peak+(DATA_START-256)*step+32768)//Q+1}
    if w:
     columns=[]
     for tx in (0,1):
      lanes=[]
      for p in range(448):
       coordinate=peak+(160+512*tx+p)*step;tag,weight=divmod((coordinate+32768)//65536,65536)
       a,b=filtered[tag+20:tag+22];row=[(2*(int(x)*(65536-weight)+int(y)*weight)+32768)//65536 for x,y in zip(a,b)]
       lanes.append(row);normal_rows.append(pack(row,21));normal_tags.append(tag);normal_mus.append(weight)
      columns.append(estimate_training(np.array(lanes,dtype=np.int64),normal_signs[tx],tx))
     entry['H']=pack([v for col in columns for v in col['H_raw_s26']],26)
     entry['phase_steps']=pack([col['symbol_step_s32'] for col in columns],32)
     entry['energies']=pack([v for col in columns for v in col['energy_u50']],50)
     entry['selected']=columns[0]['selected_rx']+2*columns[1]['selected_rx']
    windows.append(entry)
   model={'source':m if m<6 else 0,'offset':model_offset,'length':len(values),'startup_offset':so,'origin':origin,'peak_relative':k-origin,'first_relative':first-origin,'known_relative':known-origin,'commit_relative':commit-origin,
    'first_symbol':first_symbol,'count':count,'coarse':coarse,'final':final,'fresh_phase_origin':m==6,'windows':windows,'prefix_identical_to_prior_RTL_fixture':m<6}
   models.append(model);model_offset+=len(values)
   initial.append(f"moffsets[{m}]={model['offset']};mlengths[{m}]={model['length']};sourceno[{m}]={model['source']};startup_offset[{m}]={so};origins[{m}]={origin};peaks[{m}]={k-origin};firsthits[{m}]={first-origin};knowns[{m}]={known-origin};commits[{m}]={commit-origin};counts[{m}]={count};first_symbols[{m}]={first_symbol};coarse_steps[{m}]=32'h{coarse['step_u32']:08x};coarse_phases[{m}]=32'h{coarse['phase_u32']:08x};final_steps[{m}]=32'h{final['step_u32']:08x};final_phases[{m}]=32'h{final['phase_u32']:08x};")
   for w,e in enumerate(windows):
    q=m*5+w;initial.append(f"wfirst[{q}]={e['window_first']};wpeak[{q}]=80'h{e['peak']:020x};wtrain[{q}]=80'h{e['training']:020x};wnext[{q}]=80'h{e['next']:020x};wstep[{q}]=35'd{e['step']};wused[{q}]={e['used']};metric_offset[{q}]={e['metric_offset']};normal_offset[{q}]={e['normal_offset']};header_right[{q}]={e['header_right']};data_right[{q}]={e['data_right']};")
    if w:initial.append(f"gold_h[{q}]=208'h{e['H']:052x};gold_phase[{q}]=64'h{e['phase_steps']:016x};gold_energy[{q}]=200'h{e['energies']:050x};gold_selected[{q}]={e['selected']};")
   print(json.dumps({'reference_model_prepared':m,'source':model['source'],'fresh_phase_origin':m==6,'ADC_rows':len(values),'normal_matrices':4}),flush=True)
 finally:
  for f in handles.values():f.close()
 for s,source in enumerate(sources):initial.append(f"adc_offsets[{s}]={source['offset']};adc_lengths[{s}]={source['length']};")
 for name,rows,width in [('metrics',metric_rows,57),('startup',startup_rows,21),('normal',normal_rows,21),('normal_tags',normal_tags,12),('normal_mus',normal_mus,4)]:
  (out/(name+'.hex')).write_text(''.join(f'{v:0{width}x}\n' for v in rows))
 metadata={'scope':__doc__,'sources':sources,'models':models,'total_ADC_vectors':adc_offset,'total_mixed_filtered_vectors':model_offset,'normal_vectors':len(normal_rows),'startup_vectors':len(startup_rows),'metric_vectors':len(metric_rows),'generator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'g0_passed':False}
 (out/'fixture_metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
 (out/'initialization.sv').write_text('\n'.join(initial)+'\n')
 return metadata,'\n'.join(initial)

def main(a):
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
 before=disk_check('before-live-tracking-fixtures');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  meta,_=prepare(out,a.startup_reference.resolve());print(json.dumps({k:meta[k] for k in ['total_ADC_vectors','total_mixed_filtered_vectors','normal_vectors','startup_vectors','metric_vectors','g0_passed']}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-tracking-fixtures'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--startup-reference',type=Path,required=True);main(p.parse_args())
