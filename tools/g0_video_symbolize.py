#!/usr/bin/env python3
"""Finite G0 video payload/40B-header candidate and exact S512 load accounting; not a frozen profile."""
import argparse,hashlib,json,math,struct,subprocess
from fractions import Fraction
from pathlib import Path
from codec_sweep import annexb_units,access_units
HEADER=struct.Struct('<IQIIIHHHBBII')
assert HEADER.size==40
S=512;PAYLOAD=472;K=20;N=32

def main(a):
 if not 1<=a.repeat_count<=6:raise ValueError('finite1..6 repetitions of the measured10s source required')
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
 timing=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-count_frames','-show_packets','-show_entries','stream=width,height,r_frame_rate,avg_frame_rate,nb_read_frames:packet=pts_time,duration_time','-of','json',str(a.timing_video)],text=True))
 stream=timing['streams'][0]
 assert (stream['width'],stream['height'])==(1024,600) and Fraction(stream['avg_frame_rate'])==20 and int(stream['nb_read_frames'])==200
 pts=[int(Fraction(p['pts_time'])*1000000) for p in timing['packets']]
 assert pts==list(range(0,10000000,50000)) and all(Fraction(p['duration_time'])==Fraction(1,20) for p in timing['packets'])
 raw=a.input.read_bytes();aus=access_units(annexb_units(raw));assert len(aus)==200
 demux=subprocess.check_output(['ffmpeg','-v','error','-i',str(a.timing_video),'-map','0:v:0','-c:v','copy','-bsf:v','h264_mp4toannexb','-f','h264','pipe:1'])
 assert demux==raw,'Timestamp-bearing MP4 must contain the identical measured AnnexB bytes'
 # Fixed-material acceptance loops retain original compressed bytes. Frame,
 # unit and capture-time identities advance across loops; this is not camera.
 aus=aus*a.repeat_count
 symbols=[];frames=[];unit_id=0;max_unit=0;unit_count=0
 for frame_id,au in enumerate(aus):
  before=len(symbols)
  for unit_index,(kind,unit) in enumerate(au):
   assert 0<len(unit)<=1048576 and kind in (1,5,6,7,8,9)
   fragments=math.ceil(len(unit)/PAYLOAD);assert fragments<=65535
   for index in range(fragments):
    offset=index*PAYLOAD;part=unit[offset:offset+PAYLOAD]
    flags=(1 if unit_index==0 and index==0 else 0)|(2 if unit_index==len(au)-1 and index==fragments-1 else 0)|(4 if kind==5 else 0)
    h=HEADER.pack(frame_id,frame_id*50000,unit_id,len(unit),offset,len(part),index,fragments,kind,flags,50000,0)
    symbols.append(h+part+bytes(PAYLOAD-len(part)))
   max_unit=max(max_unit,len(unit));unit_id+=1;unit_count+=1
  frames.append({'frame_id':frame_id,'capture_time_us':frame_id*50000,'source_symbols':len(symbols)-before,'NAL_units':len(au)})
 real_count=len(symbols);padding=(-real_count)%K;symbols.extend([bytes(S)]*padding)
 packed=b''.join(symbols);(out/'video-source-symbols.bin').write_bytes(packed)
 # Independent clean round-trip oracle, not a production completeness/exit policy.
 rebuilt={};owners={};seen=set();empty=0;starts=ends=0
 for p in struct.iter_unpack('512s',packed):
  p=p[0];f,t,u,total,offset,length,index,count,kind,flags,duration,reserved=struct.unpack_from('<IQIIIHHHBBII',p)
  if p==bytes(S):empty+=1;continue
  assert total<=1048576 and length<=472 and offset+length<=total and index<count and duration==50000 and reserved==0
  assert t==f*50000 and (u,index) not in seen and not any(p[40+length:]);seen.add((u,index))
  assert offset==index*472 and count==math.ceil(total/472)
  starts+=bool(flags&1);ends+=bool(flags&2)
  owner=(f,total,count,kind);assert u not in owners or owners[u]==owner;owners[u]=owner
  parts=rebuilt.setdefault(u,{});assert offset not in parts;parts[offset]=p[40:40+length]
 recovered=[]
 for u in sorted(rebuilt):
  parts=rebuilt[u];position=0;pieces=[]
  for offset,part in sorted(parts.items()):assert offset==position;pieces.append(part);position+=len(part)
  assert position==owners[u][1];recovered.append(b''.join(pieces))
 assert b''.join(recovered)==raw*a.repeat_count and empty==padding and len(rebuilt)==unit_count
 assert starts==ends==len(aus)
 duration=len(aus)/20;source_bytes=len(packed);source_Bps=source_bytes/duration
 # Actual finite G0 waveform: 16 link slots/20ms, two carry descriptors, 14 carry RS coded symbols.
 nominal_source_Bps=14/.020*K/N*S
 finish=0;largest_delay=0;max_queued_source_bytes=0
 for frame in frames:
  t=frame['capture_time_us']/1e6;work=frame['source_symbols']*S
  max_queued_source_bytes=max(max_queued_source_bytes,max(0,finish-t)*nominal_source_Bps+work)
  finish=max(t,finish)+work/nominal_source_Bps;largest_delay=max(largest_delay,finish-t)
 receipt={'scope':__doc__,'production_use_allowed':False,'profile_frozen':False,'g0_or_RF_video_passed':False,
 'input':str(a.input),'input_sha256':hashlib.sha256(raw).hexdigest(),'input_bytes':len(raw),'frames':len(aus),'fps':20,'resolution':[1024,600],'ffprobe_stream':stream,'timing_video':str(a.timing_video),'timestamped_demux_AnnexB_identical':True,'all200_packet_PTS_and_durations_exact20fps':True,
 'candidate_wire':{'format':'<IQIIIHHHBBII','header_bytes':40,'symbol_bytes':512,'fragment_payload_max':472,'unit_type':'H264 NAL type1/5/6/7/8/9;0 all-zero FEC tail padding','flags':{'AU_start':1,'AU_end':2,'IDR_slice':4},'NAL_encapsulation':'AnnexB including start code','receiver_policy':'Complete slice first; local missing unit must not gate wholeAU or terminate reception; this tool only checks clean payload conservation'},
 'NAL_units':unit_count,'largest_NAL_bytes':max_unit,'data_source_symbols':real_count,'empty_tail_padding_symbols':padding,'RS20_32_blocks':len(symbols)//K,'coded_packets':len(symbols)//K*N,'source_bytes_including_video_headers_and_all_padding':source_bytes,
 'video_header_bytes':real_count*40,'NAL_fragment_tail_padding_bytes':real_count*472-len(raw)*a.repeat_count,'FEC_final_block_padding_bytes':padding*512,
 'source_repeat_count':a.repeat_count,'transmitted_AnnexB_bytes':len(raw)*a.repeat_count,
 'transmitted_AnnexB_sha256':hashlib.sha256(raw*a.repeat_count).hexdigest(),
 'clean_roundtrip_original_AnnexB_byteexact':a.repeat_count==1,
 'clean_roundtrip_transmitted_AnnexB_byteexact':True,
 'camera_or_real_source_clock_passed':False,'symbol_fixture_sha256':hashlib.sha256(packed).hexdigest(),
 'source_load_Bps':source_Bps,'minimum_measured_reliable_source_Bps_for70percent_load':source_Bps/.7,'current_waveform_nominal_source_Bps_calculated_only':nominal_source_Bps,
 'fits_30percent_margin_only_if_nominal_service_were_reliably_measured':source_Bps/.7<=nominal_source_Bps,'maximum_capacity_reduction_from_nominal_before30percent_margin_fails':1-source_Bps/.7/nominal_source_Bps,
 'optimistic_fluid_queue_calculated_only':{'maximum_queued_source_bytes':max_queued_source_bytes,'maximum_capture_to_SOURCE_service_seconds':largest_delay,'assumptions':'All encoded fragments available at capture; constant average nominal all-good service; excludes encoding, real PHY loss/FEC/period scheduling, DMA, decoder, clock mapping and HDMI. NOT source-age acceptance.'},'per_frame':frames}
 assert source_bytes==len(raw)*a.repeat_count+receipt['video_header_bytes']+receipt['NAL_fragment_tail_padding_bytes']+receipt['FEC_final_block_padding_bytes']
 (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps({k:val for k,val in receipt.items() if k!='per_frame'},indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--timing-video',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--repeat-count',type=int,default=1,help='finite1..6 loops of measured10s material; identities/time advance, no camera claim');main(p.parse_args())
