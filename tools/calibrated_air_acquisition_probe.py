#!/usr/bin/env python3
"""Raw captured ADC -> actual PL DC -> unchanged52RTL fixed8 receiver/batches.
No software DC/notch. Public constants/search arm remain configured as in52RTL.
No physical ADC/CSR/provider, new RF emission, notch or video/G0 signoff.
"""
import argparse,hashlib,json,os,shutil,subprocess,zlib
from pathlib import Path
import numpy as np
from batched_air_acquisition_probe import check_batches
from record_air_acquisition_probe import validate
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check,GIB
NEW=['adc_dc_calibration_cost_probe.sv','calibrated_batched_frontend_cost_probe.sv']

def main(a):
 r=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 c=check('before-raw-ADC-PL-DC-join');(out/'disk-before.json').write_text(json.dumps(c,indent=2));assert c['disk']['available_bytes']>=5*GIB
 try:
  previous=json.loads((ref/'receipt.json').read_text());assert previous['independent_SOURCE_validation']['SOURCE_records']==136
  names=[*previous['source_sha256'],*NEW];assert len(names)==54
  for n in names:
   p=r/'sim/g0'/n
   if n in previous['source_sha256']:assert hashlib.sha256(p.read_bytes()).hexdigest()==previous['source_sha256'][n]
   shutil.copyfile(p,out/n)
  for p in ref.glob('*.hex'):
   if p.name!='adc.hex':os.link(p,out/p.name)
  raw=np.memmap(a.capture,mode='r',dtype='<u8').reshape(-1,2);assert len(raw)==2097152
  crc=f'{zlib.crc32(a.capture.read_bytes()):08x}';assert crc=='0dcc732a'
  with (out/'adc.hex').open('w') as f:
   for value in raw[:,1]:f.write(f'{int(value):016x}\n')
  tb=(ref/'tb.sv').read_text().replace('dut.','dut.core.')
  def replace(old,new):
   nonlocal tb
   assert tb.count(old)==1,(old,tb.count(old));tb=tb.replace(old,new)
  replace('batched_acquiring_frontend_cost_probe dut(','calibrated_batched_frontend_cost_probe dut(')
  replace('adc[0:2013507]','adc[0:2097151]');replace('k<=2013507','k<=2097151');replace("BASE+48'(k)","BASE+48'(k-83644)")
  replace(' repeat(1000000)@(negedge clk);',' if(dut.calibration_faults || dut.calibration_range_drops || !dut.calibration_ready)$fatal(1,"raw DC active-input calibration fault");\n repeat(1000000)@(negedge clk);')
  replace(' if(checked<10240',' if(dut.calibration_faults!=1 || dut.calibration_range_drops || dut.calibration_ready)$fatal(1,"raw DC idle timeout fence");\n if(checked<10240')
  replace(' $display("G0_ADC_DMA_BATCH_PASS',' $display("G0_RAW_PL_DC_JOIN_PASS raw_ADC=2097152 no_software_calibration=1");\n $display("G0_ADC_DMA_BATCH_PASS')
  replace('G0_LIVE_AIR_FEC_PASS ADC=2013508','G0_LIVE_AIR_FEC_PASS raw_ADC=2097152')
  (out/'tb.sv').write_text(tb)
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run([str(x) for x in args],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  receipt={'scope':__doc__,'raw_capture_crc32':crc,'configured_search_input_sample':83648,'phase_origin_preserved_from52RTL':True,'raw_capture_samples':len(raw),'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in names},'original52RTL_sources_unchanged':True,'simulation':None,'routed_OOC':None,'physical_DMA_RF_video_or_G0_passed':False}
  if not a.ooc_only:
   xpm=bp.parent/'data/ip/xpm'
   run('compile',[bp/'xvlog','--sv',xpm/'xpm_cdc/hdl/xpm_cdc.sv',xpm/'xpm_memory/hdl/xpm_memory.sv',*names,'tb.sv']);run('elaborate',[bp/'xelab','tb','-s','raw_dc_tb','-mt','off']);run('simulate',[bp/'xsim','raw_dc_tb','-runall'])
   s=(out/'simulate.log').read_text();assert 'G0_RAW_PL_DC_JOIN_PASS' in s and 'Fatal:' not in s,s[-3000:]
   receipt.update(simulation=next(x for x in s.splitlines() if x.startswith('G0_ADC_DMA_BATCH_PASS')),independent_SOURCE_validation=validate(out,0x56334601),independent_batch_validation=check_batches(out))
  if a.timing_reference:
   t=a.timing_reference.resolve();s=(t/'ooc.tcl').read_text().replace(str(t),str(out));marker='synth_design -top batched_acquiring_frontend_cost_probe';assert s.count(marker)==1
   s=s.replace(marker,''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in NEW)+'synth_design -top calibrated_batched_frontend_cost_probe').replace('NAME =~ batch/*','NAME =~ *batch/*');(out/'ooc.tcl').write_text(s)
   run('ooc',[bp/'vivado','-mode','batch','-source','ooc.tcl','-nolog','-nojournal']);receipt['routed_OOC']=routed_measurements(out)
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps({k:v for k,v in receipt.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(check('after-raw-ADC-PL-DC-join'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--capture',type=Path,default=Path(__file__).resolve().parents[1]/'build/g0-fec-session-air-r2/adc.raw')
 p.add_argument('--timing-reference',type=Path);p.add_argument('--ooc-only',action='store_true');main(p.parse_args())
