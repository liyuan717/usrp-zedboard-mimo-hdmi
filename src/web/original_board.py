"""RAM-only original-file receiver; no expected file/media arrives via Ethernet."""
from pathlib import Path
import os,sys,json,time,hashlib,subprocess,signal,traceback
R=Path('/tmp/link-live');B=R/'photo-console';O=Path('/tmp/link-share/photo-console')
sys.path[:0]=[str(B),str(R/'python'),str(R/'python/tools')]
from original_file_protocol import Assembler
from read_rxonly_delivery import iter_delivery
from reference_mrf1 import parse_packet
from board_mp4 import playback_rfd
P=Path('/sys/bus/iio/devices/iio:device0');LO=[P/'out_altvoltage0_RX_LO_powerdown',P/'out_altvoltage1_TX_LO_powerdown'];ADC=Path('/sys/bus/iio/devices/iio:device3/buffer/enable');RING=Path('/sys/class/misc/zed-rxth/device')
STOP=R/'photo-console.stop';TRIGGER=R/'photo-console.trigger'
def save(name,v):(O/name).write_text(json.dumps(v,indent=2)+'\n')
def counters():return {k:int((RING/k).read_text()) for k in ['ring_halts','ready_blocks','ring_capacity']}
def retire(p):
 if p is not None and p.poll() is None:
  os.killpg(p.pid,signal.SIGINT)
  try:p.wait(10)
  except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait(3)
def main():
 auth=json.loads((B/'AUTHORIZATION.json').read_text());O.mkdir(exist_ok=False)
 assert auth['original_file_mode'] is True and auth['SD_write'] is False
 assert auth['expected_boot_id']==Path('/proc/sys/kernel/random/boot_id').read_text().strip()==(R/'EXPECTED_BOOT_ID').read_text().strip()
 assert '/dev/root / ext4 ro,' in Path('/proc/mounts').read_text()
 assert 2<=auth['unique_frames']<=1000 and auth['maximum_frames']==auth['unique_frames']+3
 assert all(p.read_text().strip()=='1' for p in LO) and ADC.read_text().strip()=='0'
 assert hashlib.sha256((B/'live-rx-arm').read_bytes()).hexdigest()==auth['native_sha256']
 before=subprocess.check_output(['/bin/busybox','dmesg']);(O/'kernel-before.log').write_bytes(before);initial=counters();save('rxth-before.json',initial)
 result=dict(valid=False,new_RF=True,new_ADC=True,original_file_mode=True,run_id=auth['run_id'],boot_id=auth['expected_boot_id'],visual_pass=False)
 child=None
 try:
  assert int(Path('/proc/sys/kernel/tainted').read_text())==4096
  LO[0].write_text('0\n');save('READY',{'ready':True})
  end=time.monotonic()+45
  while not TRIGGER.exists():
   if STOP.exists() or time.monotonic()>end:raise RuntimeError('trigger wait stopped or expired')
   time.sleep(.02)
  env=dict(os.environ,LINK_FINITE_CAPTURE_CFO='1',LINK_DIAGNOSTIC_COARSE_CFO_HZ='7223.213205776218',LINK_DIAGNOSTIC_SFO_PPM='39.98002049598881',LINK_DIAGNOSTIC_RF_FRAME_BUDGET=str(auth['maximum_frames']),LINK_DIAGNOSTIC_LAST_RF_FRAME_ID=str(auth['unique_frames']),LINK_RXTH_EVIDENCE_OUT=str(O/'new-adc-output.rxth'))
  argv=[str(R/'presenter/lib/ld-linux-armhf.so.3'),'--library-path',str(R/'presenter/lib'),str(B/'live-rx-arm'),'start-rx','--blocks','5120','--imbc-build-id','1','--deadline-ms','120000','--delivery-path',str(O/'received-live.rfd')]
  with (O/'native.log').open('xb') as log:
   child=subprocess.Popen(argv,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True);end=time.monotonic()+135
   while child.poll() is None:
    text=(O/'native.log').read_text(errors='replace')
    if 'LIVE_FINITE_ADC_CLOSED' in text:LO[0].write_text('1\n')
    if STOP.exists() or time.monotonic()>end:raise RuntimeError('native stopped or timed out')
    if counters()['ring_halts']!=initial['ring_halts'] or int(Path('/proc/sys/kernel/tainted').read_text())!=4096:raise RuntimeError('ring halt or new taint')
    time.sleep(.05)
  assert child.returncode==0,'native receiver failed'
  for p in LO:p.write_text('1\n')
  assert ADC.read_text().strip()=='0' and counters()['ready_blocks']==0
  summaries=[json.loads(x) for x in (O/'native.log').read_text().splitlines() if x.startswith('{')]
  native=next(x for x in reversed(summaries) if x.get('schema')=='mrf1-native-rx-backend-v1');result['native_summary']=native
  assert native['captured_blocks']==5120 and native['crc_rejected_frames']==0 and native['delivery']['ipc_dropped_records']==0
  records=list(iter_delivery((O/'received-live.rfd').open('rb')));ids=[x.frame_id for x in records]
  assert auth['unique_frames']<=len(ids)<=auth['maximum_frames']
  assert ids==[1]*(len(ids)-auth['unique_frames']+1)+list(range(2,auth['unique_frames']+1))
  assembler=Assembler(O/'assembly')
  try:
   for record in records:
    assert not record.rejected
    for stream,packet in enumerate(record.packets):
     fid,payload=parse_packet(packet,stream);assert fid==record.frame_id;assembler.accept(payload)
   recovered=assembler.finish()
  finally:assembler.close()
  assert recovered['bytes']<=180000
  Path(recovered['path']).replace(O/'received-original.mp4');recovered['path']='received-original.mp4';result['original_file']=recovered
  result['received_frame_ids']=ids;result['fresh_RF_stage_completed']=True;save('FILE_READY.json',dict(original_file=recovered,RF_records=len(ids),native_elapsed_ns=native['elapsed_ns']))
  derived=playback_rfd(O/'received-original.mp4',O/'playback-derived.rfd');result['playback_adapter']=derived
  # Visible preparation period; no RF or ADC during playback.
  for _ in range(100):
   if STOP.exists():raise RuntimeError('stopped before HDMI')
   time.sleep(.1)
  with (R/'HDMI_SINGLE_ACTIVATION_CLAIM').open('x') as claim:claim.write(auth['run_id'])
  python=os.readlink('/proc/self/exe');subprocess.run([python,str(B/'make_runtime_closure.py')],check=True,timeout=15)
  argv=[python,str(B/'receiver_offline_diag.py'),'--input',str(O/'playback-derived.rfd'),'--offline','--runtime-closure',str(B/'runtime-closure.json'),'--proof-output',str(O/'hdmi-proof.json'),'--report',str(O/'video-report.json'),'--session-id','714','--imbc-build-id','1','--max-records',str(derived['records']),'--expected-access-units','25','--timeout-s','30','--offline-deadline-s','30','--max-pending','2','--diagnostic-offline-hold-ms','60000','--diagnostic-time-scale','1']
  with (O/'video.log').open('xb') as log:
   child=subprocess.Popen(argv,stdout=log,stderr=subprocess.STDOUT,env=dict(os.environ,PYTHONPATH=str(R/'python')+':'+str(R/'python/tools')),start_new_session=True);end=time.monotonic()+105
   while child.poll() is None:
    if STOP.exists() or time.monotonic()>end:raise RuntimeError('display stopped or timed out')
    # Deliberately quiet hold: no serial, sysfs or /dev/mem polling.
    time.sleep(.5)
  assert child.returncode==0,'video decoder/presenter failed'
  report=json.loads((O/'video-report.json').read_text());proof=json.loads((O/'hdmi-proof.json').read_text())
  result['video_report']=report;result['hdmi_proof']=proof
  assert report['valid'] and len(report['accepted'])==25 and proof['physical'] is True
  assert proof['presented_at_hdmi_boundary']==25 and proof['proof_failures']==proof['backend_failures']==0
  assert proof.get('source_pts_pacing_enabled') is True
  assert 'FINITE_VIDEO_HOLD_END' in (O/'video.log').read_text()
  result['valid']=True
 except BaseException as error:
  result['failure']=type(error).__name__+': '+str(error);traceback.print_exc()
 finally:
  retire(child)
  for p in LO:p.write_text('1\n')
  time.sleep(15);after=subprocess.check_output(['/bin/busybox','dmesg']);(O/'kernel-after.log').write_bytes(after)
  errors=[x for x in after[len(before):].decode(errors='replace').splitlines() if any(w in x for w in ['Oops','panic','BUG:','WARNING:','timed out'])]
  result.update(kernel_error_lines=errors,rx_lo_powerdown=LO[0].read_text().strip(),tx_lo_powerdown=LO[1].read_text().strip(),adc_buffer_enable=ADC.read_text().strip(),rxth_after=counters())
  if errors or any(p.read_text().strip()!='1' for p in LO) or ADC.read_text().strip()!='0' or counters()['ring_halts']!=initial['ring_halts'] or int(Path('/proc/sys/kernel/tainted').read_text())!=4096:result['valid']=False
  save('result.json',result)
if __name__=='__main__':main()
