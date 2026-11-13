#!/usr/bin/env python3
"""Actual qualified C++ IQ reader + paced FIFO controls for finite video IQ.
Never passes --enable-tx; no USRP, RF or native-video performance claim.
"""
import argparse,hashlib,json,os,select,subprocess,time,zlib
from pathlib import Path
from g0_video_air_stream import preflight
from workspace_storage_guard import check,GIB

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 before=check('before-video-IQ-FIFO-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 reference=json.loads((v/'docs/evidence/g0-stream-TX-offline-20261104.json').read_text())
 assert hashlib.sha256(a.reader.read_bytes()).hexdigest()==reference['candidate_binary_sha256']
 assert hashlib.sha256((v/'pc/g0_uhd_probe.cpp').read_bytes()).hexdigest()==reference['source_sha256']['pc/g0_uhd_probe.cpp']
 plan=preflight(a.symbols);expected_bytes=plan['TX_samples']*16;results=[]
 result={'completed':False,'RF_started':False,'USRP_opened':False,'IQ_file_bytes_saved':0,'plan':plan}
 try:
  for mode in ['qualified_CXX','paced_4MSPS']:
   wave=out/mode;log=(out/(mode+'.producer.log')).open('w');reader=None;fd=None;peak=0
   args=['/usr/bin/python3',str(v/'tools/g0_video_air_stream.py'),'--symbols',str(a.symbols.resolve()),'--output',str(wave),'--session','0x56334601']
   producer=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT)
   def sample():
    nonlocal peak
    try:
     for line in Path('/proc/'+str(producer.pid)+'/status').read_text().splitlines():
      if line.startswith('VmHWM:'):peak=max(peak,int(line.split()[1]))
    except FileNotFoundError:pass
   try:
    deadline=time.monotonic()+20
    while not (wave/'iq.fifo').exists():
     if producer.poll() is not None:raise RuntimeError('producer stopped before FIFO')
     if time.monotonic()>deadline:raise TimeoutError('video FIFO ready')
     time.sleep(.02)
    started=time.monotonic();deadline=started+plan['TX_duration_s']+60
    if mode=='qualified_CXX':
     command=[str(a.reader.resolve()),'--iq',str(wave/'iq.fifo')]
     (out/'CXX-command.json').write_text(json.dumps(command,indent=2)+'\n')
     with (out/'CXX.log').open('w') as output:
      reader=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT)
      while reader.poll() is None:
       sample()
       if time.monotonic()>deadline:raise TimeoutError('finite C++ IQ read')
       time.sleep(.05)
     assert reader.returncode==0
     assert f'IQ input valid: {plan["TX_samples"]} dual-stream samples. TX disabled;' in (out/'CXX.log').read_text()
     consumer={}
    else:
     fd=os.open(wave/'iq.fifo',os.O_RDONLY|os.O_NONBLOCK);count=crc=0;digest=hashlib.sha256();first=None
     while True:
      sample()
      if time.monotonic()>deadline:raise TimeoutError('finite paced IQ read')
      if not select.select([fd],[],[],.2)[0]:continue
      try:data=os.read(fd,65536)
      except BlockingIOError:continue
      if not data:
       if count or producer.poll() is not None:break
       time.sleep(.01);continue
      if first is None:first=time.monotonic()
      count+=len(data);digest.update(data);crc=zlib.crc32(data,crc)
      remaining=first+count/(4000000*16)-time.monotonic()
      if remaining>0:time.sleep(remaining)
     assert count==expected_bytes
     elapsed=time.monotonic()-first;assert plan['TX_duration_s']<=elapsed<plan['TX_duration_s']+5
     consumer={'bytes':count,'IQ_sha256':digest.hexdigest(),'IQ_crc32':f'{crc:08x}','paced_span_s':elapsed}
    assert producer.wait(timeout=10)==0
    receipt=json.loads((wave/'receipt.json').read_text());assert receipt['completed'] and receipt['bytes_written']==expected_bytes
    assert receipt['coded_packets_sent']==plan['coded_packets'] and receipt['SOURCE_cache_peak_blocks']<=2 and receipt['FEC_cache_peak_blocks']<=1
    if consumer:assert consumer['IQ_sha256']==receipt['IQ_sha256'] and consumer['IQ_crc32']==receipt['IQ_crc32']
    results.append({'mode':mode,'wall_s':time.monotonic()-started,'sampled_producer_VmHWM_KiB':peak,'producer_receipt':receipt,'consumer':consumer})
   finally:
    if fd is not None:os.close(fd)
    for process in [reader,producer]:
     if process is not None and process.poll() is None:
      process.terminate()
      try:process.wait(timeout=5)
      except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
    log.close()
  assert results[0]['producer_receipt']['IQ_sha256']==results[1]['producer_receipt']['IQ_sha256']
  result.update(completed=True,runs=results,all_actual_CXX_and_paced_bytes_equal=True)
 finally:
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-video-IQ-FIFO-controls'),indent=2)+'\n')
 print(json.dumps({k:r for k,r in result.items() if k!='runs'},indent=2),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--symbols',type=Path,required=True);p.add_argument('--reader',type=Path,required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
