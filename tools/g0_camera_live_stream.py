#!/usr/bin/env python3
"""Finite real camera -> bounded SOURCE -> dual-stream IQ FIFO G0 prototype.

The reader controls backpressure. No radio/board is opened by this program.
Source overflow/expiry drops unsent blocks, not the camera or session. A finite
offline RS/CRC audit is enabled here; it is not production receiver circuitry.
"""
import argparse,collections,hashlib,json,os,select,signal,struct,subprocess,sys,threading,time,zlib
from pathlib import Path
from codec_sweep import distribution
from g0_camera_encode_probe import CONTROL
from g0_camera_ready_probe import capture
from g0_video_air_stream import VideoSchedule,inspect_block,HEADER,K,S,swap_coded_lanes,blackout_window,INITIAL_SOURCE_HOLD_US
from g0_fec_session_waveform import modulate_packets,public_prefix,rrc,PERIOD,SPS,RATE
from g0_forward_timebase import TxClockBracket,load_clock_bracket,PROFILE_REPEAT_PERIODS,unpack as unpack_timebase
from g0_packet_fec import recover
from hardware_lease import HardwareLease
from workspace_storage_guard import check,GIB

SOURCE_QUEUE_BLOCKS=6
ADMISSION_AGE_US=500000
DIAGNOSTIC_QUEUE_BLOCKS=96

class Mailbox:
 def __init__(self,hold_initial=False,raw_source_clock=False):
  self.hold_initial=hold_initial
  self.raw_source_clock=raw_source_clock
  self.capacity=DIAGNOSTIC_QUEUE_BLOCKS if hold_initial else SOURCE_QUEUE_BLOCKS
  self.lock=threading.Lock();self.items=collections.deque();self.closed=False
  self.produced=0;self.overflow=[];self.expired=[];self.peak=0
 def put(self,data,ready,epoch,ordinal,capture_metadata=None):
  if len(data)!=10240:raise ValueError('whole live SOURCE block')
  times={};uncertainties={} if self.raw_source_clock else None
  for (p,) in struct.iter_unpack('512s',data):
   if p!=bytes(512):
    frame,capture_us=struct.unpack_from('<IQ',p)
    if frame in times and times[frame]!=capture_us:raise ValueError('inconsistent live capture time')
    times[frame]=capture_us
    if uncertainties is not None:
     width=struct.unpack_from('<I',p,36)[0]
     if frame in uncertainties and uncertainties[frame]!=width:raise ValueError('inconsistent live capture uncertainty')
     uncertainties[frame]=width
  meta=inspect_block(data,capture_times=times,capture_uncertainties=uncertainties)
  if self.raw_source_clock:
   if not isinstance(capture_metadata,dict) or capture_metadata.get('source_clock')!='MONOTONIC_RAW':
    raise ValueError('RAW SOURCE requires original capture admission metadata')
   low=capture_metadata.get('original_min_capture_us');high=capture_metadata.get('original_max_capture_us')
   source_epoch=capture_metadata.get('source_capture_epoch_us')
   if not (type(low) is int and type(high) is int and 0<=low<=high<=ready and
           (source_epoch is None or type(source_epoch) is int and 0<source_epoch<1<<63)):
    raise ValueError('bounded original capture admission metadata')
   if source_epoch is None and any(w!=0xffffffff for w in uncertainties.values()):raise ValueError('known capture without RAW epoch')
   meta.update(min_original_capture_us=low,max_original_capture_us=high,source_capture_epoch_us=source_epoch)
  meta.update(ready_monotonic_us=ready+epoch,capture_epoch_us=epoch,source_ordinal=ordinal)
  with self.lock:
   if self.closed:raise InterruptedError('closed live mailbox')
   if len(self.items)==self.capacity:
    if self.hold_initial:raise ValueError('diagnostic SOURCE hold exceeded its fixed capacity')
    self.overflow.append(self.items.popleft()[1]['source_ordinal'])
   self.items.append((data,meta));self.produced+=1;self.peak=max(self.peak,len(self.items))
 def take(self,now):
  with self.lock:
   while self.items:
    data,meta=self.items[0]
    if self.hold_initial and now<meta['capture_epoch_us']+INITIAL_SOURCE_HOLD_US:return None
    self.items.popleft()
    if not self.hold_initial and now-meta['capture_epoch_us']-meta['min_original_capture_us']>ADMISSION_AGE_US:
     self.expired.append(meta['source_ordinal']);continue
    return data,meta
  return None
 def finish(self):
  with self.lock:self.closed=True
 def drained(self):
  with self.lock:return self.closed and not self.items
 def clear(self):
  with self.lock:
   ordinals=[meta['source_ordinal'] for _,meta in self.items];self.items.clear();self.closed=True
   return ordinals

class LiveSchedule(VideoSchedule):
 def __init__(self,mailbox,session,periods,clock_path=None):
  if not 0<=session<=0xffffffff:raise ValueError('u32 session')
  self.mailbox=mailbox;self.session=session;self.plan={'periods':periods};self.admitted=[]
  self.clock_path=clock_path;self.clock=None;self.clock_rejected=False;self.capture_epoch_us=None
  self.raw_source_clock=mailbox.raw_source_clock;self.source_capture_epoch_us=None;self.source_epoch_set=False
  self.last_profile=None;self.timebases=0
  self.initialize()
 def fill_pending(self):
  now=time.monotonic_ns()//1000
  while len(self.pending)<2:
   item=self.mailbox.take(now)
   if item is None:break
   data,meta=item
   if meta['ready_monotonic_us']>now:raise ValueError('future live block')
   if self.capture_epoch_us is None:self.capture_epoch_us=meta['capture_epoch_us']
   elif self.capture_epoch_us!=meta['capture_epoch_us']:raise ValueError('changed live capture epoch')
   if self.raw_source_clock:
    if not self.source_epoch_set:self.source_capture_epoch_us=meta['source_capture_epoch_us'];self.source_epoch_set=True
    elif self.source_capture_epoch_us!=meta['source_capture_epoch_us']:raise ValueError('changed RAW capture epoch')
   self.admitted.append(dict(wire_block=self.block+len(self.pending),source_ordinal=meta['source_ordinal'],
    capture_epoch_us=meta['capture_epoch_us'],min_capture_us=meta['min_capture_us'],max_capture_us=meta['max_capture_us'],
    ready_monotonic_us=meta['ready_monotonic_us'],admitted_monotonic_us=now,
    source_sha256=hashlib.sha256(data).hexdigest()))
   if self.raw_source_clock:self.admitted[-1].update(source_capture_epoch_us=self.source_capture_epoch_us,
    min_original_capture_us=meta['min_original_capture_us'],max_original_capture_us=meta['max_original_capture_us'])
   self.pending.append(item)
 def period(self,sf):
  wires,stats=super().period(sf)
  if self.clock_path is None or self.range is None:return wires,stats
  if self.clock is None and not self.clock_rejected:
   try:
    with self.clock_path.open('rb') as f:data=f.read(257)
    if len(data)>256:raise ValueError('bounded timed TX observation')
    values=json.loads(data)
    self.clock=load_clock_bracket(values,self.raw_source_clock)
   except FileNotFoundError:
    pass
   except (ValueError,OSError):
    self.clock_rejected=True
  if self.clock is None or self.timebase_capture_epoch_us is None:return wires,stats
  publish=self.last_profile is None or sf-self.last_profile>=PROFILE_REPEAT_PERIODS
  if publish:
   profile=bytearray(wires[0][:-4])
   struct.pack_into('<H',profile,38,2)
   wires[0]=profile+struct.pack('<I',zlib.crc32(profile))
   self.last_profile=sf
  else:
   header=bytearray(wires[1][:32])
   header[5]=5
   body=self.clock.at_training(sf,self.timebase_capture_epoch_us).pack()+bytes(480)
   wire=header+body
   wires[1]=wire+struct.pack('<I',zlib.crc32(wire))
   stats[1]['kind']=5
   self.controls+=1;self.descriptors-=1;self.timebases+=1
  return wires,stats
 @property
 def timebase_capture_epoch_us(self):
  return self.source_capture_epoch_us if self.raw_source_clock else self.capture_epoch_us
 def is_ready(self,meta,sf):return meta['ready_monotonic_us']<=time.monotonic_ns()//1000
 def finished(self):return self.mailbox.drained() and not self.pending and self.coded is None and self.index==0
 def close(self):self.pending.clear();self.coded=None;self.index=0

class WireAudit:
 def __init__(self,schedule,coded_lanes_swapped=False):
  self.coded_lanes_swapped=coded_lanes_swapped
  self.schedule=schedule;self.received={};self.blocks=0;self.packets=0;self.erasures=0
  self.source=hashlib.sha256();self.dual=[hashlib.sha256(),hashlib.sha256()];self.dual_count=[0,0]
  self.index=0;self.declared=None;self.serial=None;self.capture_to_tail=[];self.opened={};self.max_bank_age=0
 def period(self,wires,sf):
  for slot,wire in enumerate(wires):
   if len(wire)!=548 or zlib.crc32(wire[:-4])!=struct.unpack_from('<I',wire,544)[0]:raise ValueError('wire CRC/boundary')
   magic,major,kind,flags,sid,frame,seq,hint,block,index,size=struct.unpack_from('<4sBBHIIIIIHH',wire)
   if (magic,major,flags,sid,frame,seq,hint,size)!=(b'VRL3',1,0,self.schedule.session,sf,sf*16+slot,0xffffffff,512):raise ValueError('wire identity')
   body=wire[32:544]
   if kind==5:
    if body[:4]==b'VRT3':
     anchor=unpack_timebase(body[:32])
     if (slot!=1 or self.schedule.clock is None or self.declared is None or any(body[32:]) or
         anchor!=self.schedule.clock.at_training(sf,self.schedule.timebase_capture_epoch_us)):
      raise ValueError('invalid live timebase control')
     continue
    if sf>=8 and self.declared is not None:raise ValueError('unexpected control after descriptor')
    continue
   if kind==3:
    first,count=struct.unpack_from('<IB',body,20);serial=struct.unpack_from('<I',body,28)[0]
    if body[:4]!=b'VRS3' or not 1<=count<=2 or first+count>len(self.schedule.admitted):raise ValueError('unowned live descriptor')
    if self.serial==serial and self.declared!=(first,count):raise ValueError('changed descriptor without serial')
    if self.serial is not None and ((serial-self.serial)&0xffffffff)>=0x80000000:raise ValueError('backward serial')
    self.serial=serial;self.declared=(first,count)
    for b in range(first,first+count):self.opened.setdefault(b,time.monotonic_ns()//1000)
    continue
   if (sf<8 or kind not in (1,2) or self.declared is None or
       not self.declared[0]<=block<sum(self.declared) or block!=self.blocks or index!=(self.index^self.coded_lanes_swapped)):raise ValueError('live coding order/range')
   if kind!=(1 if index<20 else 2):raise ValueError('SOURCE/PARITY type')
   self.index+=1;self.packets+=1;self.dual[slot&1].update(body);self.dual_count[slot&1]+=1
   # Offline audit discards twelve CRC-bad SOURCE equations per block.
   bad=bytearray(wire)
   if index<12:bad[-1]^=1;self.erasures+=1
   if zlib.crc32(bad[:-4])==struct.unpack_from('<I',bad,544)[0]:self.received[index]=body
   if self.index==32:
    restored=recover(self.received)
    if restored is None or hashlib.sha256(restored.tobytes()).hexdigest()!=self.schedule.admitted[block]['source_sha256']:raise ValueError('live RS audit bytes')
    self.source.update(restored.tobytes());self.received={};self.blocks+=1;self.index=0
    now=time.monotonic_ns()//1000;meta=self.schedule.admitted[block]
    self.capture_to_tail.append(now-meta['capture_epoch_us']-meta.get('min_original_capture_us',meta['min_capture_us']))
    self.max_bank_age=max(self.max_bank_age,now-self.opened[block])

def stream(a,out,result,cancel,blackout):
 fifo=out/'iq.fifo';os.mkfifo(fifo,0o600)
 print('G0_CAMERA_FIFO_READY '+str(fifo),flush=True)
 fd=None;deadline=time.monotonic()+30
 while fd is None:
  if cancel.is_set():raise InterruptedError('cancelled before reader')
  try:fd=os.open(fifo,os.O_WRONLY|os.O_NONBLOCK)
  except OSError as e:
   import errno
   if e.errno!=errno.ENXIO:raise
   if time.monotonic()>deadline:raise TimeoutError('finite live IQ reader connection')
   cancel.wait(.02)
 if a.wait_go:
  (out/'waiting-for-GO.json').write_text(json.dumps(dict(camera_started=False,IQ_bytes=0,monotonic_us=time.monotonic_ns()//1000))+'\n')
  print('G0_CAMERA_READY WAIT_GO',flush=True);deadline=time.monotonic()+30;line=bytearray()
  try:
   while b'\n' not in line:
    if cancel.is_set():raise InterruptedError('cancelled before camera GO')
    if time.monotonic()>deadline:raise TimeoutError('finite camera GO deadline')
    if not select.select([sys.stdin],[],[],.1)[0]:continue
    data=os.read(sys.stdin.fileno(),16)
    if not data:raise ValueError('camera GO input closed')
    line.extend(data)
    if len(line)>3:raise ValueError('only explicit GO line is accepted')
   if line!=b'GO\n':raise ValueError('explicit camera GO required')
   result['explicit_GO_received']=True
  except BaseException:
   os.close(fd);raise
 raw_source_clock=getattr(a,'raw_source_clock',False)
 mailbox=Mailbox(getattr(a,'hold_initial_source',False),raw_source_clock);camera={};errors=[];camera_out=out/'camera';camera_out.mkdir()
 def producer():
  try:capture(camera_out,a.frames,camera,mailbox.put,cancel,raw_source_clock=raw_source_clock)
  except BaseException as e:errors.append(repr(e));camera['error']=repr(e)
  finally:mailbox.finish()
 worker=threading.Thread(target=producer,name='finite-camera-producer')
 schedule=LiveSchedule(mailbox,a.session,min(3200,(a.frames//20+3)*50),a.tx_clock)
 swap=getattr(a,'swap_coded_lanes',False);audit=WireAudit(schedule,swap)
 zero_bytes=0;masked_packets=0
 total=0;digest=hashlib.sha256();crc=0;periods=0;write_spans=[];max_late=0;started=time.monotonic();prefix=public_prefix();h=rrc()
 try:
  worker.start()
  for sf in range(schedule.plan['periods']):
   target=started+sf*.020
   if cancel.wait(max(0,target-time.monotonic())):raise InterruptedError('live IQ cancelled')
   if errors:raise RuntimeError('camera producer: '+errors[0])
   max_late=max(max_late,time.monotonic()-target)
   wires,metadata=schedule.period(sf)
   if swap:swap_coded_lanes(wires,metadata)
   audit.period(wires,sf)
   if blackout and blackout['first_period']<=sf<blackout['end_period_exclusive']:
    data=bytes(PERIOD*SPS*16)
    zero_bytes+=len(data);masked_packets+=sum(packet['kind'] in (1,2) for packet in metadata)
   else:
    packed,_=modulate_packets(a.session,sf,lambda slot:(wires[slot],metadata[slot]),prefix,h)
    if abs(packed).max()>.125:raise ValueError('G0 IQ amplitude limit')
    data=packed.tobytes()
   digest.update(data);crc=zlib.crc32(data,crc);view=memoryview(data);begin=time.monotonic();deadline=begin+2
   while view:
    if cancel.is_set():raise InterruptedError('live FIFO cancelled')
    if time.monotonic()>deadline:raise TimeoutError('live IQ sink stalled beyond2s')
    if not select.select([],[fd],[],.05)[1]:continue
    try:n=os.write(fd,view)
    except BlockingIOError:continue
    view=view[n:];total+=n
   write_spans.append(time.monotonic()-begin);periods+=1
   if sf>=8 and schedule.finished():break
  if not schedule.finished():raise TimeoutError('finite live SOURCE drain deadline')
  worker.join(timeout=3)
  if worker.is_alive() or errors or not camera.get('completed'):raise RuntimeError('finite camera did not finish cleanly')
  if audit.blocks!=len(schedule.admitted) or audit.index or audit.received:raise ValueError('live audit incomplete')
  if mailbox.produced!=audit.blocks+len(mailbox.overflow)+len(mailbox.expired):raise ValueError('unaccounted SOURCE ownership')
  if blackout:
   if zero_bytes!=blackout['zero_IQ_samples']*16 or not masked_packets or periods-blackout['end_period_exclusive']<175:
    raise ValueError('Live silence did not cover the full interval and recovery tail')
   result['controlled_dual_IQ_silence']={**blackout,'zero_IQ_bytes':zero_bytes,
      'masked_coded_packets':masked_packets,'audit_checks_the_pre_mask_schedule':True}
  if mailbox.hold_initial:
   first=schedule.admitted[0]
   observed=first['admitted_monotonic_us']-first['capture_epoch_us']
   if observed<INITIAL_SOURCE_HOLD_US or not schedule.timebases:
    raise ValueError('initial SOURCE hold or forward timebase was not observed')
   result['controlled_initial_SOURCE_hold']={
    'requested_us':INITIAL_SOURCE_HOLD_US,'first_admission_since_capture_epoch_us':observed,
    'original_capture_times_preserved':True}
  result['completed']=True
 finally:
  cancel.set();worker.join(timeout=8) if worker.ident else None
  if fd is not None:os.close(fd)
  result.update(camera=camera,camera_worker_alive_after_cleanup=worker.is_alive(),
   produced_SOURCE_blocks=mailbox.produced,transmitted_SOURCE_blocks=audit.blocks,
   overflow_dropped_unadvertised_blocks=mailbox.overflow,expired_unadvertised_blocks=mailbox.expired,
   admission_age_limit_us=None if mailbox.hold_initial else ADMISSION_AGE_US,mailbox_capacity_blocks=mailbox.capacity,mailbox_peak_blocks=mailbox.peak,
   scheduler_SOURCE_cache_peak_blocks=schedule.peak_source_blocks,FEC_cache_peak_blocks=schedule.peak_coded_blocks,
   forward_timebase_requested=a.tx_clock is not None,forward_TX_clock_bracket_accepted=schedule.clock is not None,
   SOURCE_clock_domain='MONOTONIC_RAW' if raw_source_clock else 'MONOTONIC',
   forward_TX_clock_bracket_rejected=schedule.clock_rejected,forward_timebases_transmitted=schedule.timebases,
   IQ_bytes=total,IQ_samples=total//16,IQ_sha256=digest.hexdigest(),IQ_crc32=f'{crc:08x}',periods=periods,
   wall_seconds=time.monotonic()-started,maximum_period_start_lateness_s=max_late,
   period_FIFO_write_seconds=distribution(write_spans),source_blocks_after12_CRC_erasures_byteexact=audit.blocks,
   audit_CRC_erasures=audit.erasures,audit_recovered_source_sha256=audit.source.hexdigest(),
   coded_lanes_swapped=swap,dual_coded_packet_counts=audit.dual_count,dual_payload_sha256=[x.hexdigest() for x in audit.dual],
   PC_observed_capture_to_coded_tail_us=distribution(audit.capture_to_tail),
   maximum_observed_bank_open_to_coded_tail_us=audit.max_bank_age)
  (out/'camera-result.json').write_text(json.dumps(camera,indent=2)+'\n')
  (out/'admissions.json').write_text(json.dumps(schedule.admitted,indent=2)+'\n')
  result['cancelled_queued_SOURCE_ordinals']=mailbox.clear()
  result['cancelled_scheduler_SOURCE_ordinals']=[meta['source_ordinal'] for _,meta in schedule.pending]
  result['audit_partial_equations_at_cleanup']=len(audit.received)
  schedule.close();audit.received.clear()
  result.update(queued_SOURCE_owners_after_cleanup=len(mailbox.items),scheduler_SOURCE_owners_after_cleanup=len(schedule.pending),
   coded_owners_after_cleanup=int(schedule.coded is not None),audit_equation_owners_after_cleanup=len(audit.received))
  if worker.is_alive():raise RuntimeError('camera thread cleanup did not finish')

def main(a):
 blackout=blackout_window({'periods':a.frames*50//20},getattr(a,'blackout_periods',None))
 if getattr(a,'hold_initial_source',False) and (a.frames!=200 or a.tx_clock is None or blackout or a.swap_coded_lanes):
  raise ValueError('initial SOURCE hold requires a 200-frame canonical forward-timebase diagnostic')
 out=a.output.resolve();out.mkdir(exist_ok=False);cancel=threading.Event()
 for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:cancel.set())
 result=dict(completed=False,RF_started=False,USRP_opened=False,SD_written=False,feedback_enabled=False,TDD_enabled=False,
  fixed_FEC_banks=8,resolution=[1024,600],real_source_fps_target=20,source_age_mapping_or_camera_RF_or_G0_passed=False)
 result['delegated_hardware_lease']=a.lease_fd is not None
 result['implementation_sha256_at_start']={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
  for name in ['g0_camera_live_stream.py','g0_camera_ready_probe.py','g0_video_air_stream.py',
               'g0_fec_session_waveform.py','hardware_lease.py']}
 before=check('before-live-camera-IQ');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise RuntimeError('storage threshold')
 (out/'control.c').write_text(CONTROL)
 subprocess.run(['gcc','-std=c11','-O2','-Wall','-Wextra','-Werror',str(out/'control.c'),'-o',str(out/'control')],check=True)
 def control(*values):return subprocess.check_output([str(out/'control'),*values],text=True,timeout=5).strip()
 try:
  with HardwareLease(a.lease_fd):
   old=control()
   if old not in ('0','1'):raise ValueError('camera control')
   result['original_dynamic_framerate']=int(old)
   try:
    if control('0')!='0':raise ValueError('disable camera dynamic FPS')
    stream(a,out,result,cancel,blackout)
   finally:result['restored_dynamic_framerate']=int(control(old))
 except BaseException as e:result['error']=repr(e);raise
 finally:
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-live-camera-IQ'),indent=2)+'\n')
 print(json.dumps({k:v for k,v in result.items() if k!='camera'}),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--frames',type=int,choices=(200,1200),default=200);p.add_argument('--session',type=lambda s:int(s,0),default=0x56334601)
 p.add_argument('--wait-go',action='store_true',help='open FIFO then wait for explicit stdin GO before camera/IQ; finite coordinator hook')
 p.add_argument('--swap-coded-lanes',action='store_true',help='diagnostic coded-payload permutation with physical training unchanged')
 p.add_argument('--blackout-periods',type=int,nargs=2,metavar=('START','COUNT'),help='bounded zero IQ on both channels during a live probe')
 p.add_argument('--hold-initial-source',action='store_true',help='finite diagnostic: hold actual camera SOURCE before initial radio admission')
 p.add_argument('--tx-clock',type=Path,help='atomic local UHD timed-SOB observation from the coordinator')
 p.add_argument('--raw-source-clock',action='store_true',help='send bounded RAW capture intervals and require matching RAW UHD timebase')
 p.add_argument('--lease-fd',type=int,help='operation.lock FD inherited via parent pass_fds; inode and lock verified')
 main(p.parse_args())
