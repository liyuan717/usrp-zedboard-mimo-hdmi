#!/usr/bin/env python3
"""Actual live camera/IQ FIFO consumer measurements; never enable TX or USRP.

C++ mode exercises the existing qualified reader. Paced mode optionally
pauses consumption to measure bounded SOURCE drops and continued reception.
"""
import argparse,hashlib,json,os,select,subprocess,time,zlib
from bisect import bisect_left
from pathlib import Path

SOURCE_BLOCK_BYTES=20*512

def workload_windows(timestamps):
 # Use half-open windows so a block on a boundary is counted exactly once.
 times=sorted(timestamps)
 if not times:return None
 def blocks(begin,end):
  return bisect_left(times,end)-bisect_left(times,begin)
 start=times[0]+5_000_000
 five_second_rates=None
 if times[-1]>=start+30_000_000:
  five_second_rates=[blocks(begin,begin+5_000_000)*SOURCE_BLOCK_BYTES/5
                     for begin in range(start,start+30_000_000,5_000_000)]
 # A maximum containing at least one block can start at its first block.
 maximum_rates={str(seconds):max(blocks(t,t+seconds*1_000_000) for t in times)*SOURCE_BLOCK_BYTES/seconds
                for seconds in (1,5,30)}
 # Count fully observed windows on a uniform 20ms grid for burst percentiles.
 percentiles={}
 for seconds in (1,5):
  width=seconds*1_000_000
  samples=sorted(blocks(t,t+width) for t in range(times[0],times[-1]-width+1,20_000))
  if samples:
   rank=(99*len(samples)+99)//100
   percentiles[str(seconds)]=dict(windows=len(samples),p99_Bps=samples[rank-1]*SOURCE_BLOCK_BYTES/seconds)
 return dict(fixed_window_start_us=start,fixed_five_second_Bps=five_second_rates,
             maximum_rolling_Bps_by_seconds=maximum_rates,p99_grid_step_us=20_000,
             rolling_p99_by_seconds=percentiles)

def review_source(wave,p):
 availability=json.loads((wave/'camera/availability.json').read_text())
 epoch=availability['capture_epoch_host_monotonic_us']
 ready=availability['block_ready_us']
 admissions=json.loads((wave/'admissions.json').read_text());recovered=hashlib.sha256();ordinals=[]
 original_path=wave/'camera/source-symbols.bin'
 assert len(ready)==p['produced_SOURCE_blocks']
 assert original_path.stat().st_size==len(ready)*SOURCE_BLOCK_BYTES
 assert len(admissions)==p['transmitted_SOURCE_blocks']
 assert all(a<=b for a,b in zip(ready,ready[1:]))
 with original_path.open('rb') as original:
  for block,entry in enumerate(admissions):
   assert entry['wire_block']==block and entry['admitted_monotonic_us']>=entry['ready_monotonic_us']
   ordinal=entry['source_ordinal']
   assert 0<=ordinal<len(ready)
   ordinals.append(ordinal);original.seek(ordinal*SOURCE_BLOCK_BYTES);data=original.read(SOURCE_BLOCK_BYTES)
   assert len(data)==SOURCE_BLOCK_BYTES and hashlib.sha256(data).hexdigest()==entry['source_sha256']
   assert entry['capture_epoch_us']==epoch and entry['ready_monotonic_us']==epoch+ready[ordinal]
   recovered.update(data)
 assert recovered.hexdigest()==p['audit_recovered_source_sha256'] and len(ordinals)==len(set(ordinals))
 assert sorted(ordinals+p['overflow_dropped_unadvertised_blocks']+p['expired_unadvertised_blocks'])==list(range(p['produced_SOURCE_blocks']))
 dropped=len(p['overflow_dropped_unadvertised_blocks'])+len(p['expired_unadvertised_blocks'])
 if not dropped:assert p['audit_recovered_source_sha256']==availability['source_sha256']
 return dict(all_admitted_blocks_exact_capture_SOURCE_bytes=True,all_ready_timestamps_exact_live_measurement=True,
  full_produced_transmitted_dropped_block_accounting=True,all_produced_bytes_transmitted=dropped==0,
  source_sha256=availability['source_sha256'],transmitted_source_sha256=recovered.hexdigest(),
  produced_SOURCE_ready_workload=workload_windows(epoch+t for t in ready),
  scheduler_admission_workload=workload_windows(x['admitted_monotonic_us'] for x in admissions),
  maximum_ready_to_admission_us=max((x['admitted_monotonic_us']-x['ready_monotonic_us'] for x in admissions),default=None))

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False);v=Path(__file__).resolve().parents[1]
 result=dict(completed=False,RF_started=False,USRP_opened=False,mode=a.mode,frames=a.frames,forced_sink_pause_seconds=a.stall_seconds)
 reference=json.loads((v/'docs/evidence/g0-stream-TX-offline-20261104.json').read_text())
 if a.mode=='CXX':
  assert a.stall_seconds==0 and a.reader is not None
  assert hashlib.sha256(a.reader.read_bytes()).hexdigest()==reference['candidate_binary_sha256']
 wave=out/'producer';args=['/usr/bin/python3',str(v/'tools/g0_camera_live_stream.py'),'--frames',str(a.frames),'--output',str(wave)]
 raw_source_clock=getattr(a,'raw_source_clock',False)
 if raw_source_clock:
  if not a.wait_go:raise ValueError('synthetic RAW FIFO control requires explicit GO')
  clock_path=out/'synthetic-raw-tx-clock.json'
  args+=['--raw-source-clock','--tx-clock',str(clock_path)]
  result['synthetic_RAW_TX_clock_not_device_measurement']=True
 if a.wait_go:args.append('--wait-go')
 log=(out/'producer.log').open('w');producer=subprocess.Popen(args,stdin=subprocess.PIPE if a.wait_go else None,stdout=log,stderr=subprocess.STDOUT);reader=None;fd=None;peak=0
 def go():
  if not a.wait_go:return
  end=time.monotonic()+10
  while not (wave/'waiting-for-GO.json').exists():
   if producer.poll() is not None:raise RuntimeError('producer exited before GO')
   if time.monotonic()>end:raise TimeoutError('live GO readiness')
   time.sleep(.02)
  marker=json.loads((wave/'waiting-for-GO.json').read_text())
  assert marker['camera_started'] is False and marker['IQ_bytes']==0
  assert not (wave/'camera').exists()
  if raw_source_clock:
   now=time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
   clock_path.write_text(json.dumps(dict(clock_domain='MONOTONIC_RAW',values=[now,now+1,0,1000000]))+'\n')
  producer.stdin.write(b'GO\n');producer.stdin.flush()
 def sample():
  nonlocal peak
  try:
   for line in Path('/proc/'+str(producer.pid)+'/status').read_text().splitlines():
    if line.startswith('VmHWM:'):peak=max(peak,int(line.split()[1]))
  except FileNotFoundError:pass
 try:
  deadline=time.monotonic()+20
  while not (wave/'iq.fifo').exists():
   if producer.poll() is not None:raise RuntimeError('live producer stopped before FIFO')
   if time.monotonic()>deadline:raise TimeoutError('live FIFO readiness')
   time.sleep(.02)
  deadline=time.monotonic()+a.frames/20+20;started=time.monotonic();paused=False
  if a.mode=='CXX':
   command=[str(a.reader.resolve()),'--iq',str(wave/'iq.fifo')]
   (out/'reader-command.json').write_text(json.dumps(command,indent=2)+'\n')
   with (out/'reader.log').open('w') as output:
    reader=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT)
    go()
    while reader.poll() is None:
     sample()
     if time.monotonic()>deadline:raise TimeoutError('finite live C++ reader')
     time.sleep(.05)
   if reader.returncode:raise RuntimeError('C++ live IQ validation')
   consumer={}
  else:
   fd=os.open(wave/'iq.fifo',os.O_RDONLY|os.O_NONBLOCK);go();count=crc=0;digest=hashlib.sha256();first=None
   while True:
    sample()
    if time.monotonic()>deadline:raise TimeoutError('finite paced live IQ')
    if a.stall_seconds and not paused and first is not None and time.monotonic()-first>=3:
     time.sleep(a.stall_seconds);paused=True
    if not select.select([fd],[],[],.1)[0]:continue
    data=os.read(fd,65536)
    if not data:
     if count or producer.poll() is not None:break
     time.sleep(.01);continue
    if first is None:first=time.monotonic()
    count+=len(data);crc=zlib.crc32(data,crc);digest.update(data)
    remaining=first+count/64000000-time.monotonic()
    if remaining>0:time.sleep(remaining)
   consumer=dict(bytes=count,IQ_sha256=digest.hexdigest(),IQ_crc32=f'{crc:08x}',paced_span_seconds=time.monotonic()-first,pause_applied=paused)
  if producer.wait(timeout=10):raise RuntimeError('live producer exited with error')
  p=json.loads((wave/'receipt.json').read_text());camera=p['camera']
  assert p['completed'] and camera['completed'] and not p['camera_worker_alive_after_cleanup']
  if a.wait_go:assert p['explicit_GO_received']
  for field in ['queued_SOURCE_owners_after_cleanup','scheduler_SOURCE_owners_after_cleanup','coded_owners_after_cleanup','audit_equation_owners_after_cleanup']:
   assert p[field]==0
  assert p['original_dynamic_framerate']==p['restored_dynamic_framerate']
  assert camera['frames']==camera['unique_capture_timestamps']==a.frames and camera['measured_new_capture_fps']>=20
  assert camera['actual_readiness_replay'] is None
  assert p['mailbox_peak_blocks']<=6 and p['scheduler_SOURCE_cache_peak_blocks']<=2 and p['FEC_cache_peak_blocks']<=1
  assert p['IQ_bytes']==p['periods']*1280000 and p['source_blocks_after12_CRC_erasures_byteexact']==p['transmitted_SOURCE_blocks']
  assert p['dual_coded_packet_counts'][0]>0 and p['dual_coded_packet_counts'][1]>0 and p['dual_payload_sha256'][0]!=p['dual_payload_sha256'][1]
  dropped=len(p['overflow_dropped_unadvertised_blocks'])+len(p['expired_unadvertised_blocks'])
  assert p['produced_SOURCE_blocks']==p['transmitted_SOURCE_blocks']+dropped
  source_review=review_source(wave,p)
  if a.stall_seconds:assert dropped>0 and paused
  else:assert dropped==0
  if consumer:assert consumer['bytes']==p['IQ_bytes'] and consumer['IQ_sha256']==p['IQ_sha256'] and consumer['IQ_crc32']==p['IQ_crc32']
  else:assert f'IQ input valid: {p["IQ_samples"]} dual-stream samples. TX disabled;' in (out/'reader.log').read_text()
  result.update(completed=True,producer_receipt=p,consumer=consumer,source_review=source_review,sampled_producer_VmHWM_KiB=peak,wall_seconds=time.monotonic()-started,
   live_camera_to_FIFO_subcondition_passed=True,live_camera_RF_source_clock_HDMI_age_or_G0_passed=False)
 finally:
  if fd is not None:os.close(fd)
  for process in [reader,producer]:
   if process is not None and process.poll() is None:
    process.terminate()
    try:process.wait(timeout=8)
    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
  log.close();(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps({k:v for k,v in result.items() if k!='producer_receipt'}),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--frames',type=int,choices=(200,1200),default=200);p.add_argument('--mode',choices=('CXX','paced'),default='paced')
 p.add_argument('--reader',type=Path);p.add_argument('--stall-seconds',type=float,choices=(0,.8),default=0)
 p.add_argument('--wait-go',action='store_true')
 p.add_argument('--raw-source-clock',action='store_true',help='capture RAW intervals with a synthetic typed TX bracket; no device clock claim')
 main(p.parse_args())
