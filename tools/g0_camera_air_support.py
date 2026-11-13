"""Finite live-camera RF measurement support; no production profile/clock claim."""
import hashlib,json,struct,time
from pathlib import Path
from g0_video_air_stream import HEADER,inspect_block

def plan_camera(a):
 frames=a.live_camera_frames
 if frames not in (200,1200):raise ValueError('finite camera200 or1200 frames')
 proof=json.loads((a.camera_control/'receipt.json').read_text())
 p=proof['producer_receipt']
 if not (proof['completed'] and proof['mode']=='paced' and proof['frames']==1200 and
         proof['live_camera_to_FIFO_subcondition_passed'] and p['camera']['unique_capture_timestamps']==1200 and
         not p['overflow_dropped_unadvertised_blocks'] and not p['expired_unadvertised_blocks']):
  raise ValueError('actual live1200 camera/FIFO qualification required')
 periods=(frames//20+3)*50
 return dict(live_camera=True,video_frames=frames,periods_upper_bound=periods,
  TX_samples=periods*80000,TX_duration_s=periods/50,
  note='finite upper bound; actual IQ sample count and pixels are measured after live capture')

def reference_source(wave,out,session,frames):
 """Clean comparison only; never used as live receiver completeness policy."""
 manifest=json.loads((wave/'camera/availability.json').read_text())
 source=wave/'camera/source-symbols.bin'
 if source.stat().st_size>64*1024*1024:raise ValueError('bounded SOURCE oracle')
 raw=source.read_bytes()
 if hashlib.sha256(raw).hexdigest()!=manifest['source_sha256']:raise ValueError('captured SOURCE digest')
 times=manifest['capture_times_us'];capture=dict(enumerate(times))
 wire_capture=dict(enumerate(manifest.get('wire_capture_times_us',times)))
 widths=manifest.get('wire_capture_uncertainty_us');widths=None if widths is None else dict(enumerate(widths))
 if len(times)!=frames or len(set(times))!=frames:raise ValueError('original capture identities')
 owners={};pieces={};starts=ends=0;tail=False
 for pos in range(0,len(raw),10240):
  meta=inspect_block(raw[pos:pos+10240],tail,wire_capture,widths,capture);tail=meta['seen_padding']
  for (symbol,) in struct.iter_unpack('512s',raw[pos:pos+10240]):
   if symbol==bytes(512):continue
   f,t,u,total,offset,length,index,count,kind,flags,duration,reserved=HEADER.unpack_from(symbol)
   owner=(f,t,total,count,kind,reserved)
   if u in owners and owners[u]!=owner:raise ValueError('NAL ownership conflict')
   owners[u]=owner;p=pieces.setdefault(u,{})
   if offset in p:raise ValueError('duplicate captured fragment')
   p[offset]=symbol[40:40+length];starts+=bool(flags&1);ends+=bool(flags&2)
 aus=[bytearray() for _ in range(frames)];annexb=hashlib.sha256()
 for u in sorted(pieces):
  position=0;unit=bytearray()
  for offset,p in sorted(pieces[u].items()):
   if offset!=position:raise ValueError('clean oracle fragment hole')
   unit.extend(p);position+=len(p)
  f,t,total,count,kind,reserved=owners[u]
  if position!=total:raise ValueError('clean oracle NAL truncation')
  aus[f].extend(unit);annexb.update(unit)
 if starts!=frames or ends!=frames or annexb.hexdigest()!=manifest['AnnexB_sha256']:raise ValueError('clean AU/AnnexB conservation')
 path=out/'clean-camera.aus'
 with path.open('wb') as target:
  for frame,payload in enumerate(aus):
   if not 0<len(payload)<=1048576:raise ValueError('bounded clean AU')
   target.write(struct.pack('<4sHHQQII',b'V3AU',1,32,(session<<32)|frame,times[frame],len(payload),0));target.write(payload)
 proof=dict(completed=True,frames=frames,source_sha256=manifest['source_sha256'],
  AnnexB_sha256=annexb.hexdigest(),clean_AU_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
  capture_times_preserved=True,scope='offline clean native pixel oracle, not receiver whole-AU gating')
 (out/'reference-source-review.json').write_text(json.dumps(proof,indent=2)+'\n')
 return path

def review_camera(base,camera,physical):
 frames=camera['frames']
 quality=base.get('physical_ACK_quality',{})
 physical_fps=quality['new_ACK_span_fps'] if quality.get('valid') else physical['derived_new_frame_fps']
 base['finite_live_camera_RF_DMA_decode_HDMI_clean_join_passed']=base.pop('finite_prerecorded_RF_DMA_decode_HDMI_clean_join_passed')
 base['real_new_camera_20fps_passed']=bool(camera['unique_capture_timestamps']==frames and
  camera['measured_new_capture_fps']>=20 and physical_fps>=20 and
  base['bounded_release_and_display_stop_passed'] and base['physical_ACK_identity_CRC_join_passed'] and
  base['clean_DMA_ingress_observed'] and base['physical_new_frames']==frames and
  not base['missing_frames'] and not base['duplicate_output_frames'] and not base['unknown_identities'])
 base['real_camera_fps']=camera['measured_new_capture_fps'];base['physical_new_frame_fps']=physical_fps
 base['source_clock_age_passed']=False;base['full_G0_or_video_passed']=False
 return base
