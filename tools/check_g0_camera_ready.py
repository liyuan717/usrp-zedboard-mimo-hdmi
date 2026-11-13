#!/usr/bin/env python3
"""Independent actual-camera SOURCE/NAL/capture conservation and old wire check.

Offline oracle, not the receiver's completeness/recovery policy or RF signoff.
"""
import argparse,hashlib,json,struct,subprocess,sys
from pathlib import Path
from g0_video_air_stream import VideoSchedule,HEADER
from codec_sweep import annexb_units,access_units

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False);base=a.capture.resolve()
 m=json.loads((base/'availability.json').read_text());data=(base/'source-symbols.bin').read_bytes()
 units={};owners={};seen=set();starts=ends=padding=0
 for (p,) in struct.iter_unpack('512s',data):
  if p==bytes(512):padding+=1;continue
  f,t,u,total,off,n,idx,count,kind,flags,duration,res=HEADER.unpack_from(p)
  assert t==m['capture_times_us'][f] and (u,idx) not in seen and duration==50000 and res==0
  seen.add((u,idx));owner=(f,t,total,count,kind)
  assert u not in owners or owners[u]==owner
  owners[u]=owner;units.setdefault(u,{})[off]=p[40:40+n]
  starts+=bool(flags&1);ends+=bool(flags&2)
 raw=bytearray()
 for u,parts in sorted(units.items()):
  pos=0
  for off,part in sorted(parts.items()):assert off==pos;raw.extend(part);pos+=len(part)
  assert pos==owners[u][2]
 assert hashlib.sha256(raw).hexdigest()==m['AnnexB_sha256'] and starts==ends==len(m['capture_times_us']) and padding==m['terminal_padding_symbols']
 assert len(access_units(annexb_units(raw)))==starts
 (out/'camera-rebuilt.h264').write_bytes(raw)
 probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-count_frames',
  '-show_entries','stream=width,height,r_frame_rate,nb_read_frames','-of','json',str(out/'camera-rebuilt.h264')],text=True))
 s=probe['streams'][0];assert (s['width'],s['height'],int(s['nb_read_frames']))==(1024,600,starts)
 # Raw AnnexB r_frame_rate is a demuxer guess (40 here); inspect actual VUI.
 # Progressive frames: time_scale/(2*num_units_in_tick) ==20.
 trace=subprocess.run(['ffmpeg','-v','verbose','-i',str(out/'camera-rebuilt.h264'),'-c:v','copy',
  '-bsf:v','trace_headers','-frames:v','1','-f','null','-'],capture_output=True,text=True,check=True)
 import re
 ticks=[int(x) for x in re.findall(r'num_units_in_tick\s+[01]+ = (\d+)',trace.stderr)]
 scales=[int(x) for x in re.findall(r'time_scale\s+[01]+ = (\d+)',trace.stderr)]
 progressive=[int(x) for x in re.findall(r'frame_mbs_only_flag\s+[01]+ = (\d+)',trace.stderr)]
 assert ticks and len(ticks)==len(scales) and all(t>0 and scale==40*t for t,scale in zip(ticks,scales)) and progressive and all(progressive)
 (out/'SPS-trace.log').write_text(trace.stderr)
 old=subprocess.check_output(['git','-c','gc.auto=0','show','6c54ab2:video_radio_v3/tools/g0_video_air_stream.py'],text=True)
 ns={'__name__':'previous_schedule'};exec(compile(old,'previous_schedule','exec'),ns)
 left=ns['VideoSchedule'](a.default_symbols,0x56334601);right=VideoSchedule(a.default_symbols,0x56334601)
 try:
  assert left.plan==right.plan;h=hashlib.sha256()
  for sf in range(left.plan['periods']):
   lw,lm=left.period(sf);rw,rm=right.period(sf);assert lw==rw and lm==rm
   for wire in rw:h.update(wire)
 finally:left.close();right.close()
 r=dict(completed=True,source_bytes=len(data),AnnexB_bytes=len(raw),AnnexB_sha256=m['AnnexB_sha256'],
  capture_times_exact_manifest=True,AU_starts=starts,AU_ends=ends,padding=padding,ffprobe=s,
  progressive_SPS_time_scale=scales,SPS_num_units_in_tick=ticks,SPS_fps=20,
  default_schedule_reference='6c54ab2',default_plan_and_all_wire_bytes_equal=True,default_wire_sha256=h.hexdigest(),
  RF_started=False,source_clock_or_age_passed=False)
 (out/'receipt.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--capture',type=Path,required=True)
 p.add_argument('--default-symbols',type=Path,required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
