#!/usr/bin/env python3
"""Finite paced-video payload candidate over existing dual-TX G0 PHY.

Reads one RS20/S512 source block at a time. Default capture+one50ms interval
is MODELED. Optional measured camera readiness is finite replay only.
No radio is opened and no production profile or source-age pass is implied.
"""
import argparse,errno,hashlib,json,math,os,select,struct,time,zlib
from pathlib import Path
import numpy as np
from g0_fec_session_waveform import descriptor,modulate_packets,public_prefix,rrc,PERIOD,SPS,RATE
from g0_packet_fec import encode
from workspace_storage_guard import check,GIB

S,K,N=512,20,32
HEADER=struct.Struct('<IQIIIHHHBBII')
PERIOD_US=PERIOD*SPS*1000000//RATE
STARTUP_TRAINING_PERIODS=8
INITIAL_SOURCE_HOLD_US=3_400_000
MAX_SOURCE_BYTES=64*1024*1024

def blackout_window(plan,periods):
 if periods is None:return None
 start,count=periods
 end=start+count
 if (start*PERIOD_US<2000000 or not 1200000<=count*PERIOD_US<=2000000 or
     (plan['periods']-end)*PERIOD_US<3500000):
  raise ValueError('finite dual-channel silence needs 2s before, 1.2..2s of silence and 3.5s after')
 return dict(first_period=start,period_count=count,end_period_exclusive=end,
             start_us=start*PERIOD_US,duration_us=count*PERIOD_US,
             zero_IQ_samples=count*PERIOD*SPS)

def inspect_block(data,seen_padding=False,capture_times=None,capture_uncertainties=None,original_times=None):
 if len(data)!=K*S:raise ValueError('whole RS20 source block required')
 capture=[];original=[];frames=[];padding=0
 for (symbol,) in struct.iter_unpack('512s',data):
  if symbol==bytes(S):seen_padding=True;padding+=1;continue
  if seen_padding:raise ValueError('only terminal FEC zero padding is allowed')
  f,t,u,total,offset,length,index,count,kind,flags,duration,reserved=HEADER.unpack_from(symbol)
  expected=f*50000 if capture_times is None else capture_times.get(f)
  interval=capture_uncertainties is not None
  valid_width=(reserved==capture_uncertainties.get(f) and
   (1<=reserved<=50000 or reserved==0xffffffff)) if interval else reserved==0
  if (f>=1200 or t!=expected or not 0<total<=1048576 or not 0<length<=472 or
      not count or index>=count or offset!=index*472 or count!=math.ceil(total/472) or
      length!=min(472,total-offset) or kind not in (1,5,6,7,8,9) or flags&~(15 if interval else 7) or
      bool(flags&8)!=interval or duration!=50000 or not valid_width or any(symbol[40+length:])):
   raise ValueError('bounded G0 video source header/padding')
  capture.append(t);frames.append(f)
  value=t if original_times is None else original_times.get(f)
  if type(value) is not int or not 0<=value<=64000000:raise ValueError('original capture time required')
  original.append(value)
 return {'max_capture_us':max(capture,default=0),'min_capture_us':min(capture,default=0),
  'max_original_capture_us':max(original,default=0),'min_original_capture_us':min(original,default=0),
  'frames':set(frames),'padding':padding,'seen_padding':seen_padding}

def availability(path,source):
 if path is None:return None,None,None,None
 if path.stat().st_size>1024*1024:raise ValueError('bounded readiness manifest')
 m=json.loads(path.read_text())
 digest=hashlib.sha256()
 with source.open('rb') as stream:
  while chunk:=stream.read(K*S):digest.update(chunk)
 if m.get('schema')!='G0-camera-ready-v1' or m.get('source_sha256')!=digest.hexdigest():
  raise ValueError('readiness manifest must describe identical SOURCE bytes')
 times=m['capture_times_us'];ready=m['block_ready_us']
 if (not 1<=len(times)<=1200 or times[0]!=0 or
     any(type(t)!=int or not 0<=t<=64000000 for t in times+ready) or
     any(a>=b for a,b in zip(times,times[1:])) or
     any(a>b for a,b in zip(ready,ready[1:])) or len(ready)!=source.stat().st_size//(K*S)):
  raise ValueError('bounded monotonic actual capture/readiness manifest')
 original=dict(enumerate(times));uncertainties=None;wire_times=times
 if m.get('SOURCE_timestamp_clock')=='MONOTONIC_RAW':
  wire_times=m['wire_capture_times_us'];widths=m['wire_capture_uncertainty_us'];epoch=m['capture_epoch_host_RAW_us']
  if (len(wire_times)!=len(times) or len(widths)!=len(times) or
      (epoch is not None and (type(epoch) is not int or not 0<epoch<1<<63)) or
      any(type(t) is not int or not 0<=t<=64000000 for t in wire_times) or
      any(type(w) is not int or not (1<=w<=50000 or w==0xffffffff) for w in widths) or
      any((w==0xffffffff and t!=0) or (w!=0xffffffff and epoch is None) for t,w in zip(wire_times,widths))):
   raise ValueError('bounded RAW SOURCE capture interval manifest')
  uncertainties=dict(enumerate(widths))
 return dict(enumerate(wire_times)),ready,original,uncertainties

def preflight(path,ready_path=None):
 delay_us=50000
 size=path.stat().st_size
 if not 0<size<=MAX_SOURCE_BYTES or size%(K*S):raise ValueError('bounded whole-block SOURCE file')
 capture_times,actual_ready,original_times,uncertainties=availability(ready_path,path)
 cursor=STARTUP_TRAINING_PERIODS*14;seen=set();padding=0;tail=False;blocks=0;max_delay=0
 with path.open('rb') as f:
  while data:=f.read(K*S):
   meta=inspect_block(data,tail,capture_times,uncertainties,original_times);tail=meta['seen_padding'];padding+=meta['padding'];seen|=meta['frames']
   if not meta['frames']:raise ValueError('empty whole tail block is unnecessary')
   ready_us=meta['max_original_capture_us']+delay_us if actual_ready is None else actual_ready[blocks]
   if ready_us<meta['max_original_capture_us']:raise ValueError('block cannot precede actual capture')
   ready_period=(ready_us+PERIOD_US-1)//PERIOD_US
   cursor=max(cursor,ready_period*14)+N;blocks+=1
   max_delay=max(max_delay,((cursor+13)//14)*PERIOD_US-meta['min_original_capture_us'])
   if max_delay>3000000:raise ValueError('modeled source queue exceeds3s; finite candidate cannot meet target')
 if not seen or seen!=set(range(max(seen)+1)):raise ValueError('contiguous finite frame identities required')
 if capture_times is not None and seen!=set(capture_times):raise ValueError('manifest frame census mismatch')
 periods=(cursor+13)//14
 if not 8<=periods<=3200:raise ValueError('finite8..3200 periods/64s limit')
 return {'source_bytes':size,'source_blocks':blocks,'video_frames':len(seen),'video_duration_s':len(seen)/20,
         'terminal_padding_symbols':padding,'coded_packets':blocks*N,'periods':periods,
         'TX_samples':periods*PERIOD*SPS,'TX_duration_s':periods*PERIOD*SPS/RATE,
         'availability_model':('measured complete-AU pipe observation/block completion; finite replay, not live RF' if actual_ready is not None else 'max capture_time in source block +50000us; not measured live encoder readiness'),
         'startup_training_periods':STARTUP_TRAINING_PERIODS,
         'startup_training_us':STARTUP_TRAINING_PERIODS*PERIOD_US,
         'descriptor_admission':'ready-only1..2 active blocks in fixed8 physical banks; repeated idle range never opens future blocks',
         'source_ready_delay_us':None if actual_ready is not None else delay_us,'maximum_modeled_capture_to_period_tail_us':max_delay}

def swap_coded_lanes(wires, metadata):
    """Swap only coded payloads, retaining physical training and control slots."""
    for left in range(2, len(wires), 2):
        right = left + 1
        coded = [metadata[slot]['kind'] in (1, 2) for slot in (left, right)]
        if not any(coded):
            continue
        if not all(coded):
            raise ValueError('Archived waveform has an unpaired coded slot')
        left_wire, right_wire = wires[left], wires[right]
        left_meta, right_meta = metadata[left], metadata[right]
        for slot, old, meta in ((left, right_wire, right_meta), (right, left_wire, left_meta)):
            wire = bytearray(old[:-4])
            wire[16:20] = wires[slot][16:20]
            wires[slot] = bytes(wire) + struct.pack('<I', zlib.crc32(wire))
            metadata[slot] = {**meta, 'physical_slot': slot}


class VideoSchedule:
 def __init__(self,path,session,ready_path=None):
  if not 0<=session<=0xffffffff:raise ValueError('u32 session')
  self.path=Path(path);self.plan=preflight(self.path,ready_path);self.file=self.path.open('rb');self.session=session
  self.capture_times,self.actual_ready,self.original_times,self.capture_uncertainties=availability(ready_path,self.path)
  self.initialize()
 def initialize(self):
  self.block=0;self.index=0;self.pending=[];self.next_read=0;self.coded=None
  self.range=None;self.serial=0;self.sent=0;self.idle=0;self.descriptors=0;self.controls=0
  self.peak_coded_blocks=0;self.peak_source_blocks=0;self.last_period=-1
 def close(self):self.file.close()
 def fill_pending(self):
  while len(self.pending)<2 and self.next_read<self.plan['source_blocks']:
   data=self.file.read(K*S);meta=inspect_block(data,capture_times=self.capture_times,capture_uncertainties=self.capture_uncertainties,original_times=self.original_times)
   meta['ready_us']=meta['max_capture_us']+50000 if self.actual_ready is None else self.actual_ready[self.next_read]
   self.pending.append((data,meta));self.next_read+=1
 def is_ready(self,meta,sf):return meta['ready_us']<=sf*PERIOD_US
 def period(self,sf):
  if sf!=self.last_period+1 or sf>=self.plan['periods']:raise ValueError('finite monotonic period sequence')
  self.last_period=sf;wires=[];stats=[];self.fill_pending()
  self.peak_source_blocks=max(self.peak_source_blocks,len(self.pending))
  ready=0
  for data,meta in self.pending:
   # Once per session: public training/control allows autonomous acquisition
   # before any useful FEC bank starts ageing. No RF feedback dependency.
   if sf<STARTUP_TRAINING_PERIODS or not self.is_ready(meta,sf):break
   ready+=1
  if ready:
   new_range=(self.block,ready)
   if new_range!=self.range:
    if self.range is not None:self.serial=(self.serial+1)&0xffffffff
    self.range=new_range
  # Hardware has8 banks. Only complete, currently available source blocks are
  # declared; repeated idle descriptions never reset deadlines or open futures.
  description=descriptor(*self.range,serial=self.serial)+bytes(480) if self.range is not None else bytes(S)
  declared_end=self.block+ready
  for physical_slot in range(16):
   kind=3 if self.range is not None else 5;block=0xffffffff;index=0;body=description
   if physical_slot>=2 and self.block<declared_end:
    if self.pending:
     if self.coded is None:
      self.coded=encode(np.frombuffer(self.pending[0][0],dtype=np.uint8).reshape(K,S));self.peak_coded_blocks=1
     block=self.block;index=self.index;kind=1 if index<K else 2;body=self.coded[index].tobytes()
     self.index+=1;self.sent+=1
     if self.index==N:self.block+=1;self.index=0;self.coded=None;self.pending.pop(0)
   elif physical_slot>=2:self.idle+=1
   if kind==3:self.descriptors+=1
   if kind==5:self.controls+=1
   header=struct.pack('<4sBBHIIIIIHH',b'VRL3',1,kind,0,self.session,sf,sf*16+physical_slot,0xffffffff,block,index,S)
   wire=header+body;wire+=struct.pack('<I',zlib.crc32(wire));wires.append(wire)
   stats.append({'kind':kind,'block':block,'symbol':index,'physical_slot':physical_slot})
  return wires,stats
 def finished(self):return self.sent==self.plan['coded_packets'] and self.block==self.plan['source_blocks'] and self.index==0

def main(a):
 schedule=VideoSchedule(a.symbols.resolve(),a.session,a.availability)
 blackout=blackout_window(schedule.plan,a.blackout_periods)
 out=a.output.resolve();out.mkdir(exist_ok=False)
 before=check('before-video-IQ-stream');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 assert before['disk']['available_bytes']>=5*GIB
 result={'scope':__doc__,'completed':False,'RF_started':False,'feedback_enabled':False,'TDD_enabled':False,
         'banks':8,'k':K,'n':N,'symbol_bytes':S,'session':a.session,'sample_rate':RATE,
         **schedule.plan,'full_packet_trace_enabled':False,'live_camera_or_RF_video_passed':False}
 fifo=out/'iq.fifo';os.mkfifo(fifo,0o600);fd=None;digest=hashlib.sha256();crc=0;written=0;started=time.monotonic()
 zero_bytes=0;masked_packets=0
 try:
  print('G0_VIDEO_FIFO_READY '+str(fifo),flush=True);deadline=time.monotonic()+60
  while fd is None:
   try:fd=os.open(fifo,os.O_WRONLY|os.O_NONBLOCK)
   except OSError as e:
    if e.errno!=errno.ENXIO:raise
    if time.monotonic()>deadline:raise TimeoutError('finite IQ reader connection deadline')
    time.sleep(.02)
  prefix=public_prefix();h=rrc();deadline=time.monotonic()+schedule.plan['TX_duration_s']+60
  for sf in range(schedule.plan['periods']):
   wires,metadata=schedule.period(sf)
   if blackout and blackout['first_period']<=sf<blackout['end_period_exclusive']:
    data=bytes(PERIOD*SPS*16)
    zero_bytes+=len(data);masked_packets+=sum(p['kind'] in (1,2) for p in metadata)
   else:
    packed,_=modulate_packets(a.session,sf,lambda slot:(wires[slot],metadata[slot]),prefix,h)
    if abs(packed).max()>.125:raise ValueError('existing G0 IQ amplitude bound')
    data=packed.tobytes()
   digest.update(data);crc=zlib.crc32(data,crc);view=memoryview(data)
   while view:
    if time.monotonic()>deadline:raise TimeoutError('finite IQ sink deadline')
    if not select.select([],[fd],[],.2)[1]:continue
    try:n=os.write(fd,view)
    except BlockingIOError:continue
    view=view[n:];written+=n
  assert schedule.finished()
  if blackout:
   assert zero_bytes==blackout['zero_IQ_samples']*16 and masked_packets>0
   result['controlled_dual_IQ_silence']={**blackout,'zero_IQ_bytes':zero_bytes,'masked_coded_packets':masked_packets}
  result['completed']=True
 except BaseException as e:result['error']=repr(e);raise
 finally:
  if fd is not None:os.close(fd)
  schedule.close();result.update(bytes_written=written,elapsed_s=time.monotonic()-started,IQ_sha256=digest.hexdigest(),IQ_crc32=f'{crc:08x}',
    coded_packets_sent=schedule.sent-masked_packets,idle_data_slots=schedule.idle,total_descriptors=schedule.descriptors,startup_control_packets=schedule.controls,
    FEC_cache_peak_blocks=schedule.peak_coded_blocks,SOURCE_cache_peak_blocks=schedule.peak_source_blocks)
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-video-IQ-stream'),indent=2)+'\n')
 print(json.dumps(result),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--symbols',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--availability',type=Path,help='measured camera readiness manifest; finite replay only')
 p.add_argument('--blackout-periods',type=int,nargs=2,metavar=('START','COUNT'),help='bounded zero IQ on both channels during a prerecorded probe')
 p.add_argument('--session',type=lambda s:int(s,0),default=0x56334601);main(p.parse_args())
