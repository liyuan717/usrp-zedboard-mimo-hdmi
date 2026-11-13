#!/usr/bin/env python3
"""Finite live camera -> encoder pipe -> S512 blocks with measured readiness.

No radio or board access. Capture uses original V4L2 monotonic timestamps,
never normalized encoder PTS. AU completion waits for the next AUD (or EOF),
so readiness includes that bounded parser delay. Not a live RF/age signoff.
"""
import argparse,collections,hashlib,json,math,os,re,select,subprocess,time
from pathlib import Path
from codec_sweep import annexb_units,distribution
from g0_camera_encode_probe import CONTROL
from g0_video_air_stream import HEADER,VideoSchedule
from hardware_lease import HardwareLease
from g0_capture_clock import CaptureClockBridge
from workspace_storage_guard import check,GIB

MAX_AU=1048576
AUD=re.compile(b'\x00\x00\x00\x01\x09|\x00\x00\x01\x09')

class CameraBlocks:
 def __init__(self,stream,on_block=None,capture_clock=None,raw_source_clock=False):
  self.stream=stream;self.partial=bytearray();self.ready=[];self.captures=[];self.unit=0
  self.epoch=None;self.raw_hash=hashlib.sha256();self.symbol_hash=hashlib.sha256()
  self.observed_age=[];self.symbols=0;self.peak_partial=0;self.on_block=on_block
  self.capture_clock=capture_clock
  self.raw_capture_bounds=[] if capture_clock is not None or raw_source_clock else None
  self.raw_capture_brackets=[] if capture_clock is not None or raw_source_clock else None
  self.raw_resolution_ns=capture_clock.raw_resolution_ns if capture_clock is not None else None
  self.raw_source_clock=raw_source_clock
  self.source_epoch_us=(capture_clock.observations[0][1]//1000
   if raw_source_clock and capture_clock is not None and capture_clock.observations else None)
  self.wire_captures=[] if raw_source_clock else None
  self.wire_uncertainties=[] if raw_source_clock else None
  self.partial_first_capture=None;self.partial_last_capture=None
 def append(self,symbol,ready,original_capture=None):
  if original_capture is not None:
   if self.partial_first_capture is None:self.partial_first_capture=original_capture
   self.partial_last_capture=original_capture
  self.partial.extend(symbol);self.symbols+=1;self.peak_partial=max(self.peak_partial,len(self.partial))
  if len(self.partial)==10240:
   if self.on_block is not None:
    extra=({'source_capture_epoch_us':self.source_epoch_us,'source_clock':'MONOTONIC_RAW',
     'original_min_capture_us':self.partial_first_capture,'original_max_capture_us':self.partial_last_capture}
     if self.raw_source_clock else None)
    if extra is None:self.on_block(bytes(self.partial),ready,self.epoch,len(self.ready))
    else:self.on_block(bytes(self.partial),ready,self.epoch,len(self.ready),capture_metadata=extra)
   self.stream.write(self.partial);self.symbol_hash.update(self.partial);self.partial.clear();self.ready.append(ready)
   self.partial_first_capture=self.partial_last_capture=None
 def au(self,raw,capture,observed):
  if not 0<len(raw)<=MAX_AU:raise ValueError('bounded encoded AU')
  if self.epoch is None:self.epoch=capture
  if self.captures and capture-self.epoch<=self.captures[-1]:raise ValueError('non-increasing original capture')
  if not 0<=observed-capture<=2000000:raise ValueError('capture is not recent host monotonic microseconds')
  self.captures.append(capture-self.epoch);self.observed_age.append(observed-capture)
  interval=self.capture_clock.capture_interval(capture) if self.capture_clock is not None else None
  if self.raw_capture_bounds is not None:
   self.raw_capture_bounds.append((interval.lower_ns,interval.upper_ns) if interval is not None else None)
   self.raw_capture_brackets.append((interval.before,interval.after) if interval is not None else None)
  wire_capture=capture-self.epoch;uncertainty=0
  if self.raw_source_clock:
   wire_capture=0;uncertainty=0xffffffff
   if interval is not None and self.source_epoch_us is not None:
    low=interval.lower_ns//1000;high=(interval.upper_ns+999)//1000
    if 0<=low-self.source_epoch_us<=64000000 and 1<=high-low<=50000:
     wire_capture=low-self.source_epoch_us;uncertainty=high-low
   self.wire_captures.append(wire_capture);self.wire_uncertainties.append(uncertainty)
  units=annexb_units(raw)
  if units[0][0]!=9 or not any(k in (1,5) for k,_ in units):raise ValueError('AUD/VCL required')
  self.raw_hash.update(raw)
  for pos,(kind,unit) in enumerate(units):
   if kind not in (1,5,6,7,8,9):raise ValueError('candidate NAL type')
   count=math.ceil(len(unit)/472)
   for index in range(count):
    part=unit[index*472:(index+1)*472]
    flags=int(pos==0 and index==0)|2*int(pos==len(units)-1 and index==count-1)|4*int(kind==5)
    if self.raw_source_clock:flags|=8
    header=HEADER.pack(len(self.captures)-1,wire_capture,self.unit,len(unit),index*472,len(part),index,count,kind,flags,50000,uncertainty)
    self.append(header+part+bytes(472-len(part)),observed-self.epoch,capture-self.epoch)
   self.unit+=1
 def finish(self,observed):
  padding=(-self.symbols)%20
  for _ in range(padding):self.append(bytes(512),observed-self.epoch)
  manifest=dict(schema='G0-camera-ready-v1',capture_times_us=self.captures,block_ready_us=self.ready,
   source_sha256=self.symbol_hash.hexdigest(),capture_epoch_host_monotonic_us=self.epoch,
   readiness='complete AU observed on live encoder pipe; includes next-AUD/EOF delay',
   terminal_padding_symbols=padding,AnnexB_sha256=self.raw_hash.hexdigest())
  if self.raw_capture_bounds is not None:
   manifest['capture_RAW_intervals_ns']=self.raw_capture_bounds
   manifest['capture_clock_brackets_ns']=self.raw_capture_brackets
   manifest['RAW_clock_resolution_ns']=self.raw_resolution_ns
   manifest['SOURCE_timestamp_clock']='original_V4L2_MONOTONIC'
  if self.raw_source_clock:
   manifest.update(SOURCE_timestamp_clock='MONOTONIC_RAW',capture_epoch_host_RAW_us=self.source_epoch_us,
    wire_capture_times_us=self.wire_captures,wire_capture_uncertainty_us=self.wire_uncertainties)
  return manifest

def command(frames):
 vf="select='lt(mod(n,3),2)',showinfo,setpts=N/(20*TB),scale=1066:600:out_color_matrix=bt601:out_range=tv,crop=1024:600,setsar=1,format=yuv420p"
 return ['ffmpeg','-nostdin','-hide_banner','-loglevel','info','-copyts','-f','v4l2','-input_format','mjpeg',
  '-video_size','1280x720','-framerate','30','-i','/dev/video0','-an','-vf',vf,'-frames:v',str(frames),
  '-enc_time_base','1:20','-fps_mode','passthrough','-c:v','libx264','-profile:v','baseline','-level:v','3.1',
  '-preset','veryfast','-tune','zerolatency','-threads','2','-pix_fmt','yuv420p','-b:v','900000',
  '-maxrate','900000','-bufsize','900000','-bf','0','-refs','1','-g','20','-keyint_min','20',
  '-x264-params','slices=2:sliced-threads=0:scenecut=0:open-gop=0:weightp=0:8x8dct=0:aud=1:repeat-headers=1:intra-refresh=0:fps=20/1',
  '-flush_packets','1','-f','h264','pipe:1']

def capture(out,frames,result,on_block=None,cancel=None,observe_capture_clock=False,raw_source_clock=False):
 args=command(frames);(out/'command.json').write_text(json.dumps(args,indent=2)+'\n')
 proc=None;pending=collections.deque();lines=bytearray();raw=bytearray();count=0;peak_raw=peak_pending=0;tb=False
 started=time.monotonic();deadline=started+frames/20+15
 try:
  with (out/'source-symbols.bin').open('wb') as source,(out/'encode.log').open('wb') as log:
   bridge=None
   if observe_capture_clock or raw_source_clock:
    try:
     bridge=CaptureClockBridge();bridge.sample()
    except (AttributeError,OSError,ValueError) as error:
     result['capture_clock_observation_unavailable']=repr(error)
     bridge=None
   blocks=CameraBlocks(source,on_block,bridge,raw_source_clock)
   proc=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
   pipes={proc.stdout:'raw',proc.stderr:'log'}
   while pipes:
    if cancel is not None and cancel.is_set():raise InterruptedError('live camera cancellation')
    if time.monotonic()>deadline:raise TimeoutError('finite camera pipe deadline')
    ready=select.select(list(pipes),[],[],.005 if blocks.capture_clock is not None else .2)[0]
    if blocks.capture_clock is not None:
     try:blocks.capture_clock.sample()
     except (OSError,ValueError) as error:
      result['capture_clock_observation_unavailable']=repr(error)
      blocks.capture_clock=None
    for pipe in ready:
     data=os.read(pipe.fileno(),65536);which=pipes[pipe]
     if not data:del pipes[pipe];pipe.close();continue
     if which=='log':
      log.write(data);lines.extend(data)
      while b'\n' in lines:
       line,_,rest=lines.partition(b'\n');lines=bytearray(rest)
       if b'config in time_base: 1/1000000' in line:tb=True
       m=re.search(rb'Parsed_showinfo_1[^\n]*?n:\s*(\d+)\s+pts:\s*(\d+)',line)
       if m:
        frame,pts=map(int,m.groups())
        if frame!=count:raise ValueError('capture metadata sequence')
        pending.append(pts);count+=1
      if len(lines)>65536:raise ValueError('bounded ffmpeg log line')
     else:raw.extend(data)
    # stderr and stdout are independent pipes: retain at most eight unmatched
    # capture records; never guess metadata by current time or encoder PTS.
    peak_pending=max(peak_pending,len(pending));peak_raw=max(peak_raw,len(raw))
    if len(pending)>8 or len(raw)>MAX_AU+65536:raise ValueError('bounded live camera pairing cache')
    while pending:
     matches=list(AUD.finditer(raw))
     if not matches or matches[0].start()!=0:
      if len(raw)>5:raise ValueError('encoder stream must begin with AUD')
      break
     if len(matches)<2:break
     end=matches[1].start();unit=bytes(raw[:end]);del raw[:end]
     blocks.au(unit,pending.popleft(),time.monotonic_ns()//1000)
   if proc.wait(timeout=3):raise RuntimeError('camera encoder failed')
   if raw:
    if len(pending)!=1:raise ValueError('final AU/capture metadata mismatch')
    blocks.au(bytes(raw),pending.popleft(),time.monotonic_ns()//1000)
   if pending or count!=frames or len(blocks.captures)!=frames or not tb:raise ValueError('capture/AU census/timebase')
   manifest=blocks.finish(time.monotonic_ns()//1000)
   if blocks.raw_capture_bounds is not None:
    known=[bounds for bounds in blocks.raw_capture_bounds if bounds is not None]
    result['capture_RAW_mapping']=dict(mapped=len(known),unknown=frames-len(known),
     max_interval_ns=max((bounds[1]-bounds[0] for bounds in known),default=0),
     retained_clock_observations=len(bridge.observations) if bridge is not None else 0,observation_capacity=CaptureClockBridge.capacity,
     SOURCE_timestamps_changed=raw_source_clock)
   if raw_source_clock:
    result['RAW_SOURCE_clock']=dict(known=sum(x!=0xffffffff for x in blocks.wire_uncertainties),
     unknown=sum(x==0xffffffff for x in blocks.wire_uncertainties),epoch_us=blocks.source_epoch_us,
     original_capture_timestamps_preserved_in_manifest=True)
  (out/'availability.json').write_text(json.dumps(manifest,indent=2)+'\n')
  schedule=None
  sent=0
  try:
   if on_block is None:
    schedule=VideoSchedule(out/'source-symbols.bin',0x56334601,out/'availability.json')
    for sf in range(schedule.plan['periods']):
     wires,metadata=schedule.period(sf)
     for wire,meta in zip(wires,metadata):
      if meta['kind'] in (1,2):
       if manifest['block_ready_us'][meta['block']]>sf*20000:raise ValueError('future block admitted')
       sent+=1
    if not schedule.finished():raise ValueError('finite actual-readiness replay did not drain')
  finally:
   if schedule is not None:schedule.close()
  result.update(completed=True,frames=frames,unique_capture_timestamps=len(set(blocks.captures)),
   measured_new_capture_fps=(frames-1)*1000000/(blocks.captures[-1]-blocks.captures[0]),
   observed_capture_to_complete_AU_us=distribution(blocks.observed_age),
   camera_pipe_peak_bytes=peak_raw,capture_pairing_peak_records=peak_pending,
   source_partial_peak_bytes=blocks.peak_partial,source_bytes=(out/'source-symbols.bin').stat().st_size,
   actual_readiness_replay=schedule.plan if schedule is not None else None,coded_packets_sent=sent if schedule is not None else None,
   source_cache_peak_blocks=schedule.peak_source_blocks if schedule is not None else None,
   coded_cache_peak_blocks=schedule.peak_coded_blocks if schedule is not None else None,wall_seconds=time.monotonic()-started)
 finally:
  if proc is not None and proc.poll() is None:
   proc.terminate()
   try:proc.wait(timeout=3)
   except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=3)

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False);result=dict(completed=False,RF_started=False,SD_written=False,
  live_camera_RF_or_source_clock_mapping_or_HDMI_age_or_G0_passed=False)
 before=check('before-camera-actual-ready');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise RuntimeError('storage threshold')
 (out/'control.c').write_text(CONTROL)
 subprocess.run(['gcc','-std=c11','-O2','-Wall','-Wextra','-Werror',str(out/'control.c'),'-o',str(out/'control')],check=True)
 def control(*values):return subprocess.check_output([str(out/'control'),*values],text=True,timeout=5).strip()
 try:
  with HardwareLease():
   old=control()
   if old not in ('0','1'):raise ValueError('unexpected camera control')
   result['original_dynamic_framerate']=int(old)
   try:
    if control('0')!='0':raise RuntimeError('camera dynamic FPS control')
    capture(out,a.frames,result,observe_capture_clock=getattr(a,'observe_capture_clock',False),raw_source_clock=getattr(a,'raw_source_clock',False))
   finally:result['restored_dynamic_framerate']=int(control(old))
 except BaseException as e:result['error']=repr(e);raise
 finally:
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-camera-actual-ready'),indent=2)+'\n')
 print(json.dumps(result),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--frames',type=int,choices=(200,1200),default=200)
 p.add_argument('--observe-capture-clock',action='store_true',help='record bounded RAW intervals for original capture timestamps')
 p.add_argument('--raw-source-clock',action='store_true',help='send bounded RAW capture intervals; retain original V4L2 timestamps in manifest')
 main(p.parse_args())
