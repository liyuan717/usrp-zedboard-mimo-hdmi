#!/usr/bin/env python3
"""Finite actual ARM decode/SOURCE DMA/HDMI subconditions; never full RF/G0 acceptance."""
import argparse,ast,hashlib,http.server,json,re,socket,struct,subprocess,threading,zlib
from pathlib import Path
from board_command import command, require_boot_id
from hardware_lease import HardwareLease
from workspace_storage_guard import check
from g0_video_au_probe import aus
from codec_sweep import annexb_units
HOST,BOARD='169.254.209.83','169.254.209.84'
SERIAL='/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00'
SELECTED=['ordered_clean_reordered','ordered_loss_continue','ordered_output_busy_continue','missing_AU_end_continue']

def review_stream(out,au_native,baseline,result):
 rows=[]
 selected=['format_bound_recovery'] if (out/'format_bound_recovery.json').exists() else (['high_session_u32','overload_fast'] if (out/'high_session_u32.json').exists() else SELECTED)
 for name in selected:
  report=json.loads((out/(name+'.json')).read_text());old=json.loads((baseline/((name if name in SELECTED else SELECTED[0])+'.json')).read_text())
  assert report['machine']=='armv7l' and not report['reader_error']
  expected=[dict(p,capture_id=p['capture_id']|0x8000000000000000) for p in old['pixels']] if name=='high_session_u32' else old['pixels']
  if name=='overload_fast':
   assert report['dropped']>0 and report['accepted']+report['dropped']==report['input_aus']==200
   assert all(p['capture_id'] in {q['capture_id'] for q in expected} for p in report['pixels'])
  elif name=='format_bound_recovery':
   assert report['pixels']==expected and report['input_aus']==202 and report['accepted']==202
   assert report['coded_pixel_limit']==622592 and report['decode_errors']>0 and report['format_errors']==1
  else:
   assert report['pixels']==expected,(name,'stream pixels/identity differ')
   assert report['accepted']==report['input_aus']==old['input_aus']
  assert report['known_damaged_aus']==report['packet_corrupt_marked_aus']==(2 if name=='format_bound_recovery' else old['known_damaged_input_aus'])
  fields=['unknown_identity','format_errors','expired_queue_jobs','owners_after_codec_close','bytes_after_codec_close']
  if name!='overload_fast':fields.append('dropped')
  if name=='format_bound_recovery':fields.remove('format_errors')
  for field in fields:assert report[field]==0,(name,field)
  assert report['pool_peak_bytes']<=report['pool_payload_cap_bytes']==2*(1024*1024+64)
  assert report['pool_peak_owners']<=10 and report['peak_ready']<=8
  if name!='overload_fast':assert 9.9<=report['wall_seconds']<12
  rows.append({k:v for k,v in report.items() if k!='pixels'}|{'name':name,'all_output_CRC_and_identities_equal_baseline':name!='overload_fast'})
 result.update(completed=True,runs=rows,native_paced_bounded_queue_passed=True,
               finite_fixture_file_not_continuous_runtime=True,physical_freeze_duration_not_measured=True)
 return result

SOURCE_CASES=['ordered_clean_reordered','ordered_loss_continue','ordered_output_busy_continue','ordered_wrap','ordered_session_reentry','missing_AU_end_continue','invalid_fragments_continue','bounded_pressure_session_reentry','idle_gap_continue','AU_idle_gap_clean']
DMA_CASES={'DMA_padding_32':32,'DMA_reentry_5':5}
DMA_COUNTS={**DMA_CASES,'DMA_external_stop':65536}
def review_hdmi(out,result):
 physical=[]
 for row in result['runs']:
  name=row['name'];r=json.loads((out/(name+'.json')).read_text());p=json.loads((out/(name+'.physical.json')).read_text())
  assert r['physical'] and r['HDMI_started'] and r['HDMI_drain_passed'] and r['HDMI_stop_passed']
  assert r['HDMI_backend_failures']==0 and p['backend_failures']==p['proof_failures']==0
  assert p['physical'] and p['acceptance_valid'] and p['capture_status']=='complete' and p['timestamp_order_valid']
  assert not p['failed_closed'] and not p['capture_invalidated']
  assert p['presented_at_hdmi_boundary']==p['new_frames']==r['HDMI_physical_frames']==r['HDMI_queued_frames']>0
  assert p.get('signal_loss_notices',0)==r.get('HDMI_signal_loss_notices',0)
  assert r['valid_decoded_frames']>=r['decoded_frames']>=r['HDMI_physical_frames']
  if name in ['ordered_clean_reordered','high_session_u32','AU_idle_gap_clean']:assert r['HDMI_physical_frames']==200 and not r['display_dropped_frames']
  if name=='idle_gap_continue':assert r['HDMI_new_frame_gaps_over_1s']>=1 and r['HDMI_max_new_frame_gap_ns']>1000000000
  pixels={x['capture_id']:x['rgb565_crc32'] for x in r['pixels']}
  assert len(p['records'])==min(64,p['proof_records_total'])
  assert len({x['token'] for x in p['records']})==len(p['records'])
  for x in p['records']:
   identity=(x['source_session_id']<<32)|x['source_frame_id'];assert pixels[identity]==x['payload_crc32']
  physical.append({'name':name,'new_physical_frames':r['HDMI_physical_frames'],'span_fps':p['derived_new_frame_fps'],
    'repeated_scanouts_not_new_frames':p['scanout_repeats'],'max_between_new_ack_gap_ns':r['HDMI_max_new_frame_gap_ns'],
    'between_new_ack_gaps_over_1s':r['HDMI_new_frame_gaps_over_1s'],'retained_proof_CRC_identity_rows':len(p['records'])})
  physical[-1]['signal_loss_notices']=p.get('signal_loss_notices',0)
  resources=out/(name+'.resources.json')
  if resources.exists():physical[-1]['native_child_peak_RSS_KiB']=json.loads(resources.read_text())['peak_RSS_KiB']
 result.update(HDMI_started=True,native_finite_HDMI_ack_join_passed=True,physical_HDMI_runs=physical,
   RF_video_or_source_clock_age_passed=False)
 result.pop('real_RF_or_DMA_or_HDMI_passed',None)
 result['full_RF_DMA_HDMI_video_passed']=False
 return result
def review_concurrent_padding(out,result):
 name='ordered_clean_reordered';r=json.loads((out/(name+'.concurrent-DMA.json')).read_text())
 resources=json.loads((out/(name+'.resources.json')).read_text())
 assert resources['DMA_alive_at_video_end'] and resources['DMA_returncode']==0
 assert resources['DMA_hw_batches_before_video']>0
 assert resources['DMA_run_before_video']==1
 assert resources['DMA_hw_batches_at_video_end']>resources['DMA_hw_batches_before_video']
 assert r['SOURCE_DMA_batches']==16384 and r['SOURCE_DMA_started'] and not r['reader_error']
 for field in ['SOURCE_DMA_timeout','SOURCE_DMA_lease_error','SOURCE_DMA_bad_metadata','SOURCE_DMA_bad_records',
               'SOURCE_DMA_missing_batches','SOURCE_DMA_duplicate_batches','SOURCE_records','SOURCE_DMA_stopped_running',
               'SOURCE_DMA_stopped_queued','SOURCE_DMA_stopped_user','SOURCE_DMA_stopped_poisoned','SOURCE_DMA_stopped_pinned',
               'owners_after_codec_close','bytes_after_codec_close']:assert r[field]==0,(field,r[field])
 assert not r['HDMI_started'] and r['wall_seconds']<33
 result.update(DMA_started=True,native_padding_DMA_parallel_with_file_HDMI_passed=True,
   concurrent_DMA_stats={k:v for k,v in r.items() if k!='pixels'},SOURCE_video_payload_through_physical_DMA_passed=False)
 return result
def review_dma(out,result):
 rows=[]
 for name in result.get('scheduled_dma_cases',list(DMA_CASES)):
  count=DMA_COUNTS[name]
  r=json.loads((out/(name+'.json')).read_text())
  if result.get('SOURCE_auto_session_requested'):assert r['SOURCE_auto_session'] and r['SOURCE_sessions_adopted']==0
  assert r['machine']=='armv7l' and r['SOURCE_DMA_started']
  if name=='DMA_external_stop':
   assert 0<r['SOURCE_DMA_batches']<count and r['SOURCE_DMA_interrupted'] and 0<r['SOURCE_DMA_tail_batches']<=8
  else:assert r['SOURCE_DMA_batches']==count and not r.get('SOURCE_DMA_interrupted',False)
  assert not r['reader_error'] and not r['SOURCE_DMA_timeout'] and not r['SOURCE_DMA_lease_error']
  assert not r['RF_TX_started'] and r['HDMI_started']==result.get('physical_HDMI_attempt_requested',False) and not r['source_clock_mapping_valid']
  for f in ['SOURCE_DMA_bad_metadata','SOURCE_DMA_bad_records','SOURCE_DMA_missing_batches','SOURCE_DMA_duplicate_batches',
            'SOURCE_DMA_stopped_running','SOURCE_DMA_stopped_queued','SOURCE_DMA_stopped_user','SOURCE_DMA_stopped_poisoned',
            'SOURCE_DMA_stopped_pinned','SOURCE_records','input_aus','decoded_frames','owners_after_codec_close','bytes_after_codec_close']:
   assert r[f]==0,(name,f,r[f])
  assert r.get('SOURCE_DMA_stopped_ready',0)==0
  assert r['pixels']==[] and r['wall_seconds']<8
  rows.append({k:v for k,v in r.items() if k!='pixels'}|{'name':name})
 result.update(completed=True,runs=rows,DMA_started=True,native_physical_DMA_empty_ingress_STOP_reentry_passed=True,
               video_payload_through_physical_DMA_passed=False)
 if 'DMA_external_stop' in result.get('scheduled_dma_cases',[]):result['native_external_stop_READY_tail_and_reentry_passed']=True
 return result
def review_empty_hdmi(out,result):
 for row in result['runs']:
  name=row['name'];r=json.loads((out/(name+'.json')).read_text());p=json.loads((out/(name+'.physical.json')).read_text())
  assert r['physical'] and r['HDMI_started'] and r['HDMI_drain_passed'] and r['HDMI_stop_passed']
  assert r['HDMI_backend_failures']==p['backend_failures']==p['proof_failures']==0
  assert p['physical'] and p['acceptance_valid'] and p['capture_status']=='complete'
  assert not p['failed_closed'] and not p['capture_invalidated'] and not p['physical_ack_window_valid']
  assert p['records']==[] and p['new_frames']==r['HDMI_physical_frames']==r['HDMI_queued_frames']==0
 result.update(HDMI_started=True,native_empty_DMA_HDMI_black_start_STOP_reentry_passed=True,RF_video_or_source_clock_age_passed=False)
 return result
def review_sources(out,baseline,result):
 clean=json.loads((baseline/(SELECTED[0]+'.json')).read_text())['pixels'];rows=[]
 selected=result.get('scheduled_source_cases') or [name for name in SOURCE_CASES if (out/(name+'.json')).exists()]
 for name in selected:
  r=json.loads((out/(name+'.json')).read_text());assert not r['reader_error'] and r['machine']=='armv7l'
  for field in ['unknown_identity','format_errors','owners_after_codec_close','bytes_after_codec_close','source_fragment_current_bytes']:assert r[field]==0,(name,field)
  assert r['coded_pixel_limit']==622592 and r['pool_peak_bytes']<=2097280 and r['pool_peak_owners']<=10 and r['peak_ready']<=8
  assert not r['source_clock_mapping_valid'] and r['source_events']>2900
  expected=clean
  if name=='ordered_wrap':expected=[dict(p,capture_id=(p['capture_id']&0xffffffff00000000)|((0xfffffff0+(p['capture_id']&0xffffffff))&0xffffffff)) for p in clean]
  if name in ('ordered_session_reentry','bounded_pressure_session_reentry'):expected=[dict(p,capture_id=p['capture_id']+0x100000000) for p in clean]
  if name in ('ordered_loss_continue','ordered_output_busy_continue','idle_gap_continue'):
   actual={p['capture_id']:p['rgb565_crc32'] for p in r['pixels']}
   recovery=120 if name=='idle_gap_continue' else 60
   assert all(actual.get(p['capture_id'])==p['rgb565_crc32'] for p in clean if p['capture_id']&0xffffffff>=recovery),name
  else:assert r['pixels']==expected,(name,r['decoded_frames'])
  if name=='invalid_fragments_continue':assert r['source_fragment_invalid']==4
  if name=='ordered_loss_continue':assert r['source_fragment_duplicates']>0 and r['source_fragment_expired_units']>0
  if name=='idle_gap_continue':assert r['source_fragment_expired_units']>0 and r['source_AUs_damaged']>0
  if name=='bounded_pressure_session_reentry':assert r['source_fragment_evicted_units']>0 and r['source_fragment_peak_units']==32 and r['source_fragment_cache_peak_bytes']<=2097152 and r['source_fragment_stale']>0
  if name=='ordered_clean_reordered':assert r['dropped']==0 and r['last_output_seconds']<10.5
  rows.append({k:v for k,v in r.items() if k!='pixels'}|{'name':name,'required_pixel_identity_comparisons_passed':True})
 result.update(completed=True,runs=rows,validated_source_cases=selected,native_SOURCE_NAL_AU_queue_decoder_callback_chain_passed=True,real_RF_or_DMA_or_HDMI_passed=False)
 return result

def review(out,au_native,result):
 native=json.loads((au_native/'receipt.json').read_text());assert native['native_CortexA9_component_passed']
 indices={r['name']:i for i,r in enumerate(native['runs'])};rows=[]
 for name in SELECTED:
  report=json.loads((out/(name+'.json')).read_text());source=au_native/('u'+str(indices[name])+'.units');input_rows=aus(source.read_bytes())
  assert report['machine']=='armv7l' and not report['physical'] and not report['paced']
  assert report['input_aus']==len(input_rows)
  known=sum(r[3] for r in input_rows)
  assert report['known_damaged_input_aus']==report['packet_corrupt_marked_aus']==known
  accepted_ids={r[0] for r in input_rows}
  assert all(p['capture_id'] in accepted_ids for p in report['pixels']),'decoder lost full session/frame identity'
  frames={p['capture_id']&0xffffffff:p['rgb565_crc32'] for p in report['pixels']}
  assert len(frames)==len(report['pixels'])==report['decoded_frames'] and 199 in frames
  rows.append(dict(name=name,input_AUs=len(input_rows),known_damaged_AUs=sum(r[3] for r in input_rows),input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                   decoded_frames=report['decoded_frames'],decoder_flagged_frames=report['damaged_frames'],decode_errors=report['decode_errors'],
                   packet_corrupt_marked_AUs=report['packet_corrupt_marked_aus'],frame_corrupt_flagged=report['frame_corrupt_flagged'],ARM_input_struct_bytes=report['input_struct_bytes'],
                   CPU_seconds=report['cpu_seconds'],wall_seconds=report['wall_seconds'],pixel_frames=frames))
 clean=rows[0];assert clean['decoded_frames']==200 and clean['decoder_flagged_frames']==clean['decode_errors']==0
 idrs=[identity&0xffffffff for identity,_,payload,_ in aus((au_native/('u'+str(indices[SELECTED[0]])+'.units')).read_bytes()) if any(kind==5 for kind,_ in annexb_units(payload))]
 recovery=next(f for f in idrs if f>40)
 for row in rows:
  row['missing_output_source_frames']=sorted(set(clean['pixel_frames'])-set(row['pixel_frames']))
  row['RGB565_CRC32_differs_from_clean_frames']=[f for f in row['pixel_frames'] if clean['pixel_frames'].get(f)!=row['pixel_frames'][f]]
  required=[f for f in clean['pixel_frames'] if f>=recovery]
  assert all(row['pixel_frames'].get(f)==clean['pixel_frames'][f] for f in required),(row['name'],'periodic recovery CRC')
  row['RGB565_CRC32_equal_from_periodic_IDR']=recovery;row['matching_frames_after_recovery']=len(required)
 assert rows[1]['input_AUs']==199 and rows[1]['known_damaged_AUs']>0 and rows[1]['missing_output_source_frames'] and rows[1]['RGB565_CRC32_differs_from_clean_frames']
 result['runs']=rows;result['completed']=True;result['periodic_IDR_recovery_pixel_CRC_comparison_passed']=True
 result['decoder_flags_alone_insufficient_for_artifact_statistics']=any(not r['decoder_flagged_frames'] and r['RGB565_CRC32_differs_from_clean_frames'] for r in rows)
 result['physical_freeze_duration_not_measured']=True
 return result

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve()
 if a.stream_extra and not a.stream:raise ValueError('--stream-extra needs --stream')
 if a.source_events and (not a.stream or not a.fragments):raise ValueError('--source-events needs --stream and --fragments')
 if a.stream_bounds and not a.stream:raise ValueError('--stream-bounds needs --stream')
 if a.source_dma and (not a.stream or a.source_events or a.stream_extra or a.stream_bounds or a.source_case):raise ValueError('--source-dma needs only --stream')
 if a.source_dma_stop and not a.source_dma:raise ValueError('--source-dma-stop needs --source-dma')
 if a.source_dma_auto and not a.source_dma:raise ValueError('--source-dma-auto needs --source-dma')
 if a.hdmi and (not a.stream or (a.source_dma and not a.source_dma_stop)):raise ValueError('--hdmi needs stream file input or explicit --source-dma-stop; empty DMA never counts as video')
 if a.concurrent_padding and (not a.hdmi or a.source_case!='ordered_clean_reordered'):raise ValueError('--concurrent-padding needs one clean SOURCE/HDMI case')
 review_selected=lambda result:review_dma(out,result) if a.source_dma else (review_sources(out,a.baseline.resolve(),result) if a.source_events else (review_stream(out,a.au_native.resolve(),a.baseline.resolve(),result) if a.stream else review(out,a.au_native.resolve(),result)))
 if a.review_only:
  p=out/'receipt.json';result=json.loads(p.read_text());saved=out/'initial-review-failed.json'
  if not result['completed'] and not saved.exists():saved.write_bytes(p.read_bytes())
  result=review_selected(result)
  if a.hdmi:result=(review_empty_hdmi if a.source_dma else review_hdmi)(out,result)
  if a.concurrent_padding:result=review_concurrent_padding(out,result)
  p.write_text(json.dumps(result,indent=2)+'\n');print('SAVED_NATIVE_DECODE_REVIEW_PASS');return
 out.mkdir(exist_ok=False)
 assert re.fullmatch(r'[A-Za-z0-9_-]{1,64}',out.name)
 native=json.loads((a.au_native/'receipt.json').read_text());assert native['native_CortexA9_component_passed']
 selected=list(DMA_CASES) if a.source_dma else (SOURCE_CASES if a.source_events else (['format_bound_recovery'] if a.stream_bounds else (['high_session_u32','overload_fast'] if a.stream_extra else SELECTED)))
 if a.source_dma_stop:selected=['DMA_external_stop','DMA_reentry_5']
 if a.source_case:
  if not a.source_events:raise ValueError('--source-case needs --source-events')
  selected=[a.source_case]
 case_index={r['name']:i for i,r in enumerate(native['runs'])};files={'probe':a.decoder/('g0-stream-decode' if a.stream else 'g0-decode-display')}
 runtime=['ld-linux-armhf.so.3','libavcodec.so.60','libavutil.so.58','libstdc++.so.6','libm.so.6','libgcc_s.so.1','libc.so.6']
 for name in runtime:files[name]=a.decoder/'runtime'/name
 for j,name in enumerate(selected):
  if a.source_dma:continue
  if a.source_events:
   base='clean' if name=='AU_idle_gap_clean' else (name if name not in ['missing_AU_end_continue','invalid_fragments_continue','idle_gap_continue'] else SELECTED[0])
   source=a.fragments/base/'events.bin'
   if name in ['missing_AU_end_continue','invalid_fragments_continue','idle_gap_continue','AU_idle_gap_clean']:
    data=bytearray(source.read_bytes())
    if name=='missing_AU_end_continue':
     for pos in range(0,len(data),544):
      if struct.unpack_from('<I',data,pos+20)[0]==0 and struct.unpack_from('<I',data,pos+32)[0]==12:data[pos+63]&=~2
    elif name=='invalid_fragments_continue':
     pos=next(p for p in range(0,len(data),544) if struct.unpack_from('<I',data,p+20)[0]==0)
     bad=[]
     for offset,fmt,value in [(16,'<I',0xffffffff),(24,'<H',473),(28,'<H',65535),(36,'<I',1)]:
      row=bytearray(data[pos:pos+544]);struct.pack_into(fmt,row,32+offset,value);bad.append(row)
     data=data[:pos]+b''.join(bad)+data[pos:]
    elif name=='AU_idle_gap_clean':
     # Controlled 350ms no-input interval after the first complete VCL NAL
     # in AU12; all original bytes remain. This is not an RF timing trace.
     pos=next(p+544 for p in range(0,len(data),544)
      if struct.unpack_from('<I',data,p+20)[0]==0
      and struct.unpack_from('<I',data,p+32)[0]==12
      and data[p+62] in (1,5)
      and struct.unpack_from('<H',data,p+58)[0]+1==struct.unpack_from('<H',data,p+60)[0])
     for p in range(pos,len(data),544):struct.pack_into('<Q',data,p,struct.unpack_from('<Q',data,p)[0]+350000)
    else:
     pos=next(p for p in range(0,len(data),544) if struct.unpack_from('<I',data,p+20)[0]==0 and struct.unpack_from('<I',data,p+32)[0]==100)
     # Real no-input interval while earlier reordered NALs remain incomplete.
     # Their TTL expiry is intended; compare recovery after the next IDR120.
     assert all(struct.unpack_from('<I',data,p+20)[0]!=0 or struct.unpack_from('<I',data,p+32)[0]<100 for p in range(0,pos,544))
     for p in range(pos,len(data),544):struct.pack_into('<Q',data,p,struct.unpack_from('<Q',data,p)[0]+1300000)
    source=out/(name+'.events');source.write_bytes(data)
   files['in'+str(j)]=source;continue
  source=a.au_native/('u'+str(case_index[name if name in SELECTED else SELECTED[0]])+'.units')
  if name=='format_bound_recovery':
   data=bytearray();sid=0x56334601
   for index,size in enumerate(['1920x1080','320x240']):
    path=out/(size+'.h264')
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:size='+size+':rate=20','-frames:v','1','-c:v','libx264','-profile:v','baseline','-pix_fmt','yuv420p','-x264-params','bframes=0:threads=1','-f','h264',str(path)],check=True)
    payload=path.read_bytes();data.extend(struct.pack('<4sHHQQII',b'V3AU',1,32,(sid<<32)|(0xfffffff0+index),index*50000,len(payload),1)+payload)
   for identity,capture,payload,damaged in aus(source.read_bytes()):data.extend(struct.pack('<4sHHQQII',b'V3AU',1,32,identity,capture+100000,len(payload),damaged)+payload)
   source=out/'bounded-format-recovery.units';source.write_bytes(data)
  if name=='high_session_u32':
   data=bytearray(source.read_bytes());pos=0
   while pos<len(data):
    ident=struct.unpack_from('<Q',data,pos+8)[0];struct.pack_into('<Q',data,pos+8,ident|0x8000000000000000)
    pos+=32+struct.unpack_from('<I',data,pos+24)[0]
   source=out/'high-session.units';source.write_bytes(data)
  files['in'+str(j)]=source
 assert sum(p.stat().st_size for p in files.values())<24*1024*1024
 crc={k:f'{zlib.crc32(p.read_bytes()):08x}' for k,p in files.items()}
 runtime_remote='/tmp/v3-g0-rt-'+f"{zlib.crc32(json.dumps({k:crc[k] for k in runtime},sort_keys=True).encode()):08x}"
 copy_names=[name for name in files if name not in runtime]
 storage_script="python3 - <<'V3FREE'\nimport os\n"
 storage_script+='needed='+str(sum(files[n].stat().st_size for n in copy_names)+8*1024*1024)+'\n'
 storage_script+='runtime='+repr(runtime_remote)+'\n'
 storage_script+='expected='+repr([(name,files[name].stat().st_size) for name in runtime])+'\n'
 storage_script+="for name,size in expected:\n if not os.path.isfile(runtime+'/'+name):needed+=size\ns=os.statvfs('/tmp')\nassert s.f_bavail*s.f_frsize>=needed\nV3FREE\n"
 remote='/tmp/'+out.name;result=dict(scope=__doc__,RF_started=False,DMA_started=False,HDMI_started=False,SD_written=False,PL_changed=False,
     completed=False,full_RF_or_paced_video_or_G0_passed=False,finite_fixture_file_not_continuous_runtime=True,
     decoder_binary_sha256=hashlib.sha256(files['probe'].read_bytes()).hexdigest(),shared_runtime_remote=runtime_remote,runs=[])
 if a.source_events:result['scheduled_source_cases']=selected
 if a.source_dma:result['physical_DMA_attempt_requested']=True
 if a.source_dma:result['scheduled_dma_cases']=selected
 if a.source_dma_auto:result['SOURCE_auto_session_requested']=True
 if a.hdmi:result['physical_HDMI_attempt_requested']=True
 (out/'disk-before.json').write_text(json.dumps(check('before-native-partial-decode'),indent=2)+'\n');server=None
 try:
  with HardwareLease() as lease:
   def run(name,text,timeout=30):
    subprocess.run(['sh','-n'],input=text,text=True,check=True);(out/(name+'.sh')).write_text(text+'\n')
    return command(SERIAL,text,out/(name+'.log'),timeout,lease=lease).decode(errors='replace')
   identity=run('identity','''test "$(uname -m)" = armv7l
test "$(uname -r)" = 6.12.0-xilinx
cat /proc/sys/kernel/random/boot_id
grep ' / ext4 ro,' /proc/mounts
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(devmem 0x7c430008)" = 0x00000000
'''+storage_script)
   result['boot_id']=require_boot_id(identity,getattr(a,'expected_boot_id',None))
   class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
     p=files.get(self.path.lstrip('/'))
     if self.client_address[0]!=BOARD or p is None:self.send_error(403);return
     data=p.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def log_message(self,*args):pass
   server=http.server.ThreadingHTTPServer((HOST,0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
   setup=f'test ! -e {remote}\nmkdir {remote}\nmkdir -p {runtime_remote}\n'
   for name in files:
    target=(runtime_remote if name in runtime else remote)+'/'+name
    setup+=f'if test ! -f {target}; then wget -q -O {target} http://{HOST}:{server.server_address[1]}/{name}; fi\nbusybox crc32 {target}\n'
   setup+=f'chmod 700 {remote}/probe {runtime_remote}/ld-linux-armhf.so.3\n'
   log=run('transfer',setup,60)
   for name in files:assert re.search(r'(?m)^'+crc[name]+' '+re.escape((runtime_remote if name in runtime else remote)+'/'+name)+r'\r?$',log),name
   for j,name in enumerate(selected):
    arguments=('paced source-dma '+('auto' if a.source_dma_auto else '0x56334601')+' '+str(DMA_COUNTS[name])+' 5000') if a.source_dma else (('paced source-events' if a.source_events else ('fast' if name=='overload_fast' else 'paced')) if a.stream else '2 slice null pixels fast')
    if a.hdmi:arguments+=' hdmi'
    input_path='/dev/v3-g0-source-ring' if a.source_dma else remote+'/in'+str(j)
    invocation=f'{runtime_remote}/ld-linux-armhf.so.3 --library-path {runtime_remote} {remote}/probe {input_path} {remote}/r{j} {arguments} > {remote}/stdout{j} 2> {remote}/stderr{j}'
    if name=='DMA_external_stop':
     invocation+=' &\nv3_rx_pid=$!\n'
     invocation+='trap \'kill -CONT "$v3_rx_pid" 2>/dev/null || true; kill -TERM "$v3_rx_pid" 2>/dev/null || true\' EXIT\n'
     invocation+='for i in $(seq 1 150); do\n kill -0 "$v3_rx_pid" || break\n'
     invocation+=' if test "$(devmem 0x7c430008)" = 0x00000001 && test "$(( $(busybox devmem 0x7c430010) ))" -ge 10; then break; fi\n sleep 0.02\ndone\n'
     invocation+='test "$(devmem 0x7c430008)" = 0x00000001\nkill -STOP "$v3_rx_pid"\nsleep 0.05\nkill -TERM "$v3_rx_pid"\nkill -CONT "$v3_rx_pid"\nwait "$v3_rx_pid"'
    if a.hdmi:
     invocation="python3 - <<'V3DISPLAY'\nimport subprocess,resource,json,pathlib,sys,time\n"
     invocation+='runtime='+repr(runtime_remote)+'\np=pathlib.Path('+repr(remote)+')\n'
     if a.concurrent_padding:
      invocation+=f"dma_o=(p/'dma-stdout{j}').open('wb');dma_e=(p/'dma-stderr{j}').open('wb')\n"
      invocation+=f"dma=subprocess.Popen([runtime+'/ld-linux-armhf.so.3','--library-path',runtime,str(p/'probe'),'/dev/v3-g0-source-ring',str(p/'r{j}.DMA.json'),'paced','source-dma','0x56334601','16384','30000'],stdout=dma_o,stderr=dma_e)\n"
     invocation+="a=[runtime+'/ld-linux-armhf.so.3','--library-path',runtime,str(p/'probe'),"+repr(input_path)+",str(p/'r"+str(j)+"')]+"+repr(arguments.split())+'\n'
     workload=f"with (p/'stdout{j}').open('wb') as o,(p/'stderr{j}').open('wb') as e:\n r=subprocess.run(a,stdout=o,stderr=e)\n"
     if name=='DMA_external_stop':
      workload="import os,signal\nwith (p/'stdout0').open('wb') as o,(p/'stderr0').open('wb') as e:\n"
      workload+=" child=subprocess.Popen(a,stdout=o,stderr=e)\n try:\n  deadline=time.monotonic()+3\n  ready=False\n"
      workload+="  while time.monotonic()<deadline and child.poll() is None:\n   run=int(subprocess.check_output(['busybox','devmem','0x7c430008']),16)\n   batches=int(subprocess.check_output(['busybox','devmem','0x7c430010']),16)\n   if run==1 and batches>=10:ready=True;break\n   time.sleep(.01)\n"
      workload+="  assert ready,'DMA not ready for external stop'\n  child.send_signal(signal.SIGSTOP)\n  time.sleep(.05)\n  child.send_signal(signal.SIGTERM)\n  child.send_signal(signal.SIGCONT)\n  r=child.wait(timeout=10)\n"
      workload+=" finally:\n  if child.poll() is None:\n   child.send_signal(signal.SIGCONT)\n   child.terminate()\n   child.wait(timeout=10)\n r=subprocess.CompletedProcess(a,r)\n"
     if a.concurrent_padding:
      invocation+='try:\n deadline=time.monotonic()+3\n before=0;run=0\n while time.monotonic()<deadline and dma.poll() is None:\n  run=int(subprocess.check_output(["busybox","devmem","0x7c430008"]),16)\n  before=int(subprocess.check_output(["busybox","devmem","0x7c430010"]),16)\n  if run==1 and before>0:break\n  time.sleep(.01)\n assert run==1 and before>0 and dma.poll() is None,"DMA not ready before video"\n'
      invocation+=''.join(' '+line+'\n' for line in workload.splitlines())
      invocation+=' alive=dma.poll() is None\n after=int(subprocess.check_output(["busybox","devmem","0x7c430010"]),16)\n dr=dma.wait(timeout=35)\nfinally:\n if dma.poll() is None:\n  dma.terminate()\n  try:dma.wait(timeout=5)\n  except subprocess.TimeoutExpired:dma.kill();dma.wait(timeout=5)\n dma_o.close();dma_e.close()\n'
     else:invocation+=workload
     extra=',DMA_alive_at_video_end=alive,DMA_returncode=dr,DMA_run_before_video=run,DMA_hw_batches_before_video=before,DMA_hw_batches_at_video_end=after,RSS_scope="maximum_child_not_sum"' if a.concurrent_padding else ',RSS_scope="single_native_decoder_child"'
     invocation+=f"(p/'r{j}.resources.json').write_text(json.dumps(dict(peak_RSS_KiB=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,returncode=r.returncode{extra})))\nsys.exit(r.returncode)\nV3DISPLAY"
     ast.parse(invocation.split('\n',1)[1].rsplit('\nV3DISPLAY',1)[0])
    script=f'''set +e
{invocation}
v3_decode_rc=$?
echo "$v3_decode_rc" > {remote}/rc{j}
test "$v3_decode_rc" = 0 -o "$v3_decode_rc" = 2
test -s {remote}/r{j}'''
    if a.source_dma:result['DMA_last_attempt_state']='unknown_until_native_report'
    run('decode-'+str(j),script,90)
    reply={}
    with socket.socket() as listener:
     listener.bind((HOST,0));listener.listen(1);listener.settimeout(15);port=listener.getsockname()[1]
     def receive():
      try:
       conn,peer=listener.accept();assert peer[0]==BOARD
       with conn:
        conn.settimeout(15);parts=[];count=0
        while True:
         data=conn.recv(min(65536,1048577-count))
         if not data:break
         parts.append(data);count+=len(data);assert count<=1048576
       reply['data']=b''.join(parts)
      except BaseException as e:reply['error']=repr(e)
     t=threading.Thread(target=receive,daemon=True);t.start()
     read=f"b=(p/'r{j}').read_bytes()"
     if a.hdmi:read=f"b=json.dumps(dict(report=(p/'r{j}').read_text(),proof=(p/'r{j}.physical.json').read_text(),resources=(p/'r{j}.resources.json').read_text())).encode()"
     if a.concurrent_padding:read=read.replace(')).encode()',f",DMA=(p/'r{j}.DMA.json').read_text())).encode()")
     text="python3 - <<'V3RETURN'\nimport socket,pathlib,json\np=pathlib.Path("+repr(remote)+")\n"+read+f"\ns=socket.create_connection(('{HOST}',{port}),5)\ns.sendall(b)\ns.shutdown(socket.SHUT_WR)\ns.close()\nV3RETURN"
     run('return-'+str(j),text);t.join(timeout=20);assert not t.is_alive() and 'error' not in reply,reply
    data=reply['data']
    if a.hdmi:
     envelope=json.loads(data);data=envelope['report'].encode();(out/(name+'.physical.json')).write_text(envelope['proof']);result['HDMI_started']=True
     (out/(name+'.resources.json')).write_text(envelope['resources'])
     if a.concurrent_padding:(out/(name+'.concurrent-DMA.json')).write_text(envelope['DMA'])
    report=json.loads(data);(out/(name+'.json')).write_bytes(data)
    if a.source_dma:
     result['DMA_started']=result['DMA_started'] or report.get('SOURCE_DMA_started',False)
     result['DMA_last_attempt_state']='native_report_returned'
    assert report['machine']=='armv7l' and report['physical']==a.hdmi and report['paced']==(a.stream and name!='overload_fast')
    input_rows=[] if a.source_events or a.source_dma else aus(files['in'+str(j)].read_bytes())
    if not a.source_events and not a.source_dma:assert report['input_aus']==len(input_rows)
    frames={r['capture_id']&0xffffffff:r['rgb565_crc32'] for r in report['pixels']}
    assert len(frames)==report['decoded_frames']
    if not a.source_dma and name not in ('overload_fast','ordered_wrap'):assert 199 in frames
    result['runs'].append(dict(name=name,input_AUs=report['input_aus'],decoded_frames=report['decoded_frames'],damaged_frames=report.get('damaged_frames',report.get('decoder_flagged_frames')),
                              decode_errors=report['decode_errors'],CPU_seconds=report['cpu_seconds'],wall_seconds=report['wall_seconds'],pixel_frames=frames))
    print(json.dumps({k:value for k,value in result['runs'][-1].items() if k!='pixel_frames'}),flush=True)
   result=review_selected(result)
   if a.hdmi:result=(review_empty_hdmi if a.source_dma else review_hdmi)(out,result)
   if a.concurrent_padding:result=review_concurrent_padding(out,result)
   if a.stream_bounds:run('coded-pixel-limit-log',f"grep 'exceeds specified max pixel count' {remote}/stderr0")
   # Only successful, fully returned results allow removal of exact generated
   # input/program copies. Reports/stdout/stderr and shared runtime stay.
   release=''
   for name in copy_names:release+=f'test "$(busybox crc32 {remote}/{name} | cut -d " " -f1)" = {crc[name]}\n'
   release+="python3 - <<'V3RELEASE'\nimport os\n"
   release+='directory='+repr(remote)+'\nnames='+repr(copy_names)+'\n'
   release+="paths={directory+'/'+n for n in names}\nfor pid in os.listdir('/proc'):\n if not pid.isdigit():continue\n try:\n  text=open('/proc/'+pid+'/maps').read()\n  assert not any(p in text for p in paths),'mapped generated input'\n  for fd in os.listdir('/proc/'+pid+'/fd'):\n   try:target=os.readlink('/proc/'+pid+'/fd/'+fd)\n   except FileNotFoundError:continue\n   assert target not in paths,'open generated input'\n except (FileNotFoundError,ProcessLookupError):pass\nfor p in paths:os.unlink(p)\nprint('EXACT_GENERATED_COPIES_RELEASED')\nV3RELEASE\n"
   try:
    run('release-generated-copies',release);result['generated_board_copies_released']=copy_names
   except (RuntimeError,TimeoutError) as error:result['generated_copy_release_error']=repr(error)
 finally:
  if server:server.shutdown();server.server_close()
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');(out/'disk-after.json').write_text(json.dumps(check('after-native-partial-decode'),indent=2)+'\n')
 print('NATIVE_PARTIAL_DECODE_FINITE_TEST_COMPLETED',flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('au-native','decoder','output'):p.add_argument('--'+name,type=Path,required=True)
 p.add_argument('--review-only',action='store_true')
 p.add_argument('--stream',action='store_true',help='paced bounded queue prototype; needs --baseline for exact CRC/identity comparisons')
 p.add_argument('--baseline',type=Path)
 p.add_argument('--stream-extra',action='store_true',help='high u32 session and finite unpaced overload controls')
 p.add_argument('--source-events',action='store_true',help='direct S512->NAL/AU callbacks, finite event fixture only')
 p.add_argument('--fragments',type=Path)
 p.add_argument('--stream-bounds',action='store_true',help='finite oversized/small-format rejection then normal recovery')
 p.add_argument('--source-case',choices=SOURCE_CASES)
 p.add_argument('--source-dma',action='store_true',help='actual finite32/5 empty SOURCE DMA with LOs off; no video/RF acceptance')
 p.add_argument('--source-dma-stop',action='store_true',help='owned-process STOP/TERM/CONT fills READY slots, drains bounded tail and tests reentry')
 p.add_argument('--source-dma-auto',action='store_true',help='learn no session until a PL-validated SOURCE arrives; empty-DMA control only')
 p.add_argument('--hdmi',action='store_true',help='join stream file to actual retained physical HDMI; requires matched modules ready')
 p.add_argument('--concurrent-padding',action='store_true',help='finite16384 physical empty DMA batches overlap clean file/HDMI; not RF video')
 p.add_argument('--expected-boot-id',help='require the qualified RAM boot before starting the native probe')
 main(p.parse_args())
