"""Fixed original-file OTA backend; invoked only by explicit web start."""
from pathlib import Path
import math,signal,copy,fcntl,hashlib,inspect,io,json,os,re,secrets,struct,subprocess,sys,tarfile,time,types
APP=Path(__file__).resolve().parent;W=APP.parent;R=W/'reception-display-optimization/20261030';D=W/'deliverables/usrp-zedboard-integration-20261026/realtime-hdmi'
sys.path.append(str(R/'tools'))
from sintel250_fetchfixed import make_video25,wf
from finite_tx_trigger import feed_and_trigger,validate_actual
from config import STATE,sha,verify_assets
from resource_watch import ResourceWatch
from original_file_protocol import manifest,chunks
from board_mp4 import access_units
PROTO=W/'deliverables/usrp-zedboard-integration-20261026/candidates/usrp-preroll-gain12/usrp/mrf1-v1'
EXTRA={'received-original.mp4','FILE_READY.json'}

def prepare(source,out):
 import sys
 sys.path.insert(0,str(PROTO));import air_phy as phy
 phy.verify_contract();units=access_units(source)
 session=secrets.token_bytes(16);payloads=list(chunks(source,session));head=manifest(source,session)
 frames=[(1,(head,head))]+[(i//2+2,(payloads[i],payloads[min(i+1,len(payloads)-1)])) for i in range(0,len(payloads),2)]
 if not 2<=len(frames)<=1000:raise ValueError('原文件超出首版单窗口容量；上限180000字节')
 wave=out/'source.iqb';seq=0;carry=b''
 with wave.open('xb') as target:
  def emit(raw):
   nonlocal carry,seq
   carry+=raw
   while len(carry)>=16384*16:
    target.write(struct.pack('<4sQI',b'IQB1',seq,16384)+carry[:16384*16]);carry=carry[16384*16:];seq+=1
  first=phy.build_frames(frames[:1])[0].tobytes()
  emit(first*3)
  for at in range(0,len(frames),16):emit(phy.build_frames(frames[at:at+16])[0].tobytes())
  if carry:target.write(struct.pack('<4sQI',b'IQB1',seq,len(carry)//16)+carry);seq+=1
  target.write(struct.pack('<4sQI',b'IQB1',seq,0))
 spec=dict(unique_frames=len(frames),RF_bursts=len(frames)+3,samples=(len(frames)+3)*3840,file_bytes=source.stat().st_size,file_sha256=sha(source),AUs=len(units),no_transcoding=True)
 wf.write_json(out/'original-file-profile.json',spec);return spec

def fixed_feeder(gain):
 def validate(value):
  value=copy.deepcopy(value)
  for channel in value.get('channels',[]):
   actual=channel.get('gain_db')
   if type(actual) not in (int,float) or not math.isfinite(actual) or abs(actual-gain)>.01:raise ValueError('USRP actual gain differs from requested')
   channel['gain_db']=18
  validate_actual(value)
 context=dict(feed_and_trigger.__globals__,validate_actual=validate)
 function=types.FunctionType(feed_and_trigger.__code__,context,closure=feed_and_trigger.__closure__)
 function.__kwdefaults__=dict(feed_and_trigger.__kwdefaults__)
 return function

def make_manager(source,out,settings,stop,notify):
 if settings['frequency_hz']!=5500000000 or settings['bandwidth_hz']!=40000000 or settings['sample_rate_hz']!=4000000:
  raise ValueError('当前实收配置固定5500 MHz、40 MHz带宽、4 MS/s；发射增益可在0–18 dB调节')
 verify_assets()
 # No hardware is opened before real ARM NULL decode validates the selected file.
 validation=out/'offline-codec-validation'
 with (out/'offline-codec.log').open('xb') as log:
  done=subprocess.Popen(['/usr/bin/python3',str(APP/'tests/validate_original_integration.py'),str(validation),str(source)],stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1'))
  end=time.monotonic()+90
  while done.poll() is None:
   if stop.is_set() or time.monotonic()>end:
    os.killpg(done.pid,signal.SIGTERM)
    try:done.wait(3)
    except subprocess.TimeoutExpired:os.killpg(done.pid,signal.SIGKILL);done.wait(3)
    raise RuntimeError('离线解码校验已停止或超时，未发射')
   time.sleep(.1)
 if done.returncode:raise ValueError('视频未通过板端 ARM 解码离线校验，未发射；查看本轮 offline-codec.log')
 reference=json.loads((validation/source.stem/'report.json').read_text())
 spec=prepare(source,out);cls,old_build=make_video25(out)
 names=wf.EVIDENCE_NAMES|EXTRA|{'RF_READY.json'}
 class OriginalManager(cls):
  def update(self,stage=None,detail=None,**fields):
   if detail:
    detail=detail.replace('照片','原文件视频').replace('二十五个 AU','原文件视频').replace('347 个',str(spec['RF_bursts'])+' 个')
   result=wf.Manager.update(self,stage,detail,**fields)
   notify(self.snapshot());return result
  def preflight(self):
   wf.Manager.preflight(self)
   profile=dict(name='priority',sha256='66ac62616234da99209aed6ebf557b92751636a01b964c61768d78c785edd4d7',receiver_sha256='09fd7222ec31434d5643c9235171add705c747e5e9b853da2182f99228d83eec',SD_written=False)
   wf.write_json(out/'runtime-profile.json',profile)
  def boot(self):
   boot=wf.Manager.boot(self)
   pins=json.loads((W/'reception-display-optimization/20261101/sintel250-wired-reproduce-r1-preflight/expected-root-runtime.json').read_text());actual={}
   keys=list(pins)
   for i in range(0,len(keys),3):
    raw=self.command('sha256sum '+' '.join(keys[i:i+3]),'root-runtime-'+str(i//3),20)
    for digest,name in re.findall(rb'(?:^|\n)([0-9a-f]{64})\s+(/[^\r\n ]+)',raw):actual[name.decode()]=digest.decode()
   mismatch=[k for k in pins if actual.get(k)!=pins[k]]
   wf.write_json(out/'root-runtime-check.json',dict(valid=not mismatch,mismatches=mismatch))
   if mismatch:raise RuntimeError('SD关键运行库已改变；未发射：'+','.join(mismatch))
   return boot
  def status(self):
   value=wf.Manager.status(self)
   if value.get('state')=='file_ready':
    ready=value['file_ready'];self.update('processing','原文件空口接收完成，准备 HDMI 播放（约10秒后开始）',file_ready=ready)
   return value
  def transmit(self):
   outtx=out/'tx';outtx.mkdir()
   argv=[str(D/'short-video-staged-r2-20261030/tx-gain18/continuous_tx'),'--stats',str(outtx/'transport.json'),'--actual',str(outtx/'actual-readback.json'),'--freq','5500000000','--bandwidth','40000000','--gain',str(settings['gain_db']),'--scale','1','--queue','8','--prefill','4','--max-send-samples','65536','--clock','internal','--time','internal','--addr','192.168.10.2','--serial','3095BA5','--enable-tx','YES','--tx-zero-preroll-samples','400000']
   wf.write_json(outtx/'command.json',argv)
   with (outtx/'stdout.log').open('xb') as stdout,(outtx/'stderr.log').open('xb') as stderr:
    self.tx=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr,start_new_session=True);os.set_blocking(self.tx.stdin.fileno(),False)
    timing=fixed_feeder(settings['gain_db'])(self.tx,(out/'source.iqb').read_bytes(),lambda:json.loads((outtx/'actual-readback.json').read_text()),lambda:self.command('touch /tmp/link-live/photo-console.trigger','trigger-new-ADC',10),self.check_stop)
    wf.write_json(outtx/'timing.json',timing)
   stats=json.loads((outtx/'transport.json').read_text())
   if not(self.tx.returncode==0 and stats['transport_pass'] and stats['input_samples_per_channel']==stats['accepted_samples_per_channel']==spec['samples'] and all(stats[k]==0 for k in ['underflows','late_errors','other_async_errors'])):raise RuntimeError('USRP有限发射回执失败')
  def grade(self,evidence):
   board=json.loads((evidence/'result.json').read_text());native=board.get('native_summary',{})
   if not board.get('valid'):raise RuntimeError('板端原文件接收/显示失败：'+board.get('failure','未知错误'))
   assert board['run_id']==self.state['run_id'] and board['boot_id']==self.state['boot_id']
   assert native['captured_blocks']==5120 and native['crc_rejected_frames']==0 and native['delivery']['ipc_dropped_records']==0
   ids=board['received_frame_ids'];assert ids==[1]*(len(ids)-spec['unique_frames']+1)+list(range(2,spec['unique_frames']+1))
   assert spec['unique_frames']<=len(ids)<=spec['RF_bursts']
   from original_audit import audit
   independent=audit(evidence,source)
   wf.write_json(out/'independent-original-audit.json',independent)
   recovered=evidence/'received-original.mp4'
   assert recovered.stat().st_size==spec['file_bytes'] and sha(recovered)==spec['file_sha256']
   assert recovered.read_bytes()==source.read_bytes(),'received original differs byte-for-byte'
   assert board['original_file']['sha256']==spec['file_sha256']
   proof=json.loads((evidence/'hdmi-proof.json').read_text());assert proof['physical'] is True and proof['presented_at_hdmi_boundary']==25
   assert proof['proof_failures']==proof['backend_failures']==0
   report=json.loads((evidence/'video-report.json').read_text());assert report['valid'] and len(report['accepted'])==25
   expected=[(x['sequence'],x['source_pts_ms'],x['accepted_payload_crc32']) for x in reference['accepted']]
   actual=[(x['sequence'],x['source_pts_ms'],x['accepted_payload_crc32']) for x in report['accepted']]
   assert actual==expected,'received-file decoded pixels differ from independent ARM NULL reference'
   observed_pixels=[(x['source_frame_id'],x['source_pts_ns'],x['payload_crc32']) for x in proof['records']]
   expected_pixels=[(x['sequence'],x['source_pts_ms']*1000000,int(x['accepted_payload_crc32'],16)) for x in reference['accepted']]
   assert observed_pixels==expected_pixels,'physical HDMI pixel CRC differs from selected file reference'
   assert board['rx_lo_powerdown']==board['tx_lo_powerdown']=='1' and board['adc_buffer_enable']=='0' and not board['kernel_error_lines']
   from video25_sintel import cadence25
   return dict(technical_pass=True,new_RF=True,new_ADC=True,historical_redisplay=False,run_id=self.state['run_id'],boot_id=self.state['boot_id'],ADC_blocks=5120,ADC_sha256=sha(evidence/'new-adc-output.rxth'),RF_frames=len(ids),unique_RF_frames=spec['unique_frames'],CRC_correct_packets=len(ids)*2,HDMI_frames=25,original_file_byte_identical=True,original_file_sha256=sha(recovered),original_file_bytes=spec['file_bytes'],LO_closed=True,ADC_closed=True,kernel_errors=[],presentation_cadence=cadence25(proof),continuous_realtime_verified=False,visual_confirmation=None)
 # The existing tested private transfer/fetch methods retain their bounded HTTP caps.
 OriginalManager.fetch.__globals__['EVIDENCE_NAMES']=names
 def build(path,run_id,boot_id,secret):
  old_build(out/'template-app.tar.gz',run_id,boot_id,secret)
  with tarfile.open(out/'template-app.tar.gz') as t:
   files={m.name:t.extractfile(m).read() for m in t.getmembers()};modes={m.name:m.mode for m in t.getmembers()}
  files['board_supervisor.py']=(APP/'original_board.py').read_bytes()
  for name in ['board_mp4.py','original_file_protocol.py']:files[name]=(APP/name).read_bytes();modes[name]=0o600
  for name in ['reference_mrf1.py','reference_rvf2.py']:files[name]=(PROTO/'protocol'/name).read_bytes();modes[name]=0o600
  agent=files['board_agent.py'].decode().replace("'interrupts-after.log',", "'received-original.mp4', 'FILE_READY.json', 'interrupts-after.log',")
  needle="elif (O / 'RF_READY.json').is_file():"
  assert needle in agent
  agent=agent.replace(needle,"elif (O / 'FILE_READY.json').is_file():\n            value['state']='file_ready'\n            value['file_ready']=json.loads((O / 'FILE_READY.json').read_text())\n        "+needle)
  files['board_agent.py']=agent.encode()
  auth=json.loads(files['AUTHORIZATION.json']);auth.update(original_file_mode=True,scope='bounded original MP4 OTA then board decode/HDMI',unique_frames=spec['unique_frames'],maximum_frames=spec['RF_bursts'],tx_gain_db=settings['gain_db'])
  for name in ['source_video_sha256','media_version','video_target_bitrate_bps']:auth.pop(name,None)
  # Never include source bytes, expected decode output or original-file hash in board bundle.
  files['AUTHORIZATION.json']=json.dumps(auth).encode()
  files['FILES.json']=json.dumps([dict(name=n,bytes=len(b),sha256=hashlib.sha256(b).hexdigest()) for n,b in files.items() if n!='FILES.json']).encode()
  with tarfile.open(path,'x:gz') as t:
   for name,raw in files.items():
    info=tarfile.TarInfo(name);info.size=len(raw);info.mode=modes[name];t.addfile(info,io.BytesIO(raw))
  return json.loads(files['FILES.json'])
 return OriginalManager,build,spec

def run(source,out,settings,stop,notify,lock,run_id):
 cls,build,spec=make_manager(source,out,settings,stop,notify)
 m=cls();m.run=out;m.secret=secrets.token_hex(32);m.stop_event=stop;m.lock_fd=os.dup(lock.fileno())
 m.state=dict(run_id=run_id,active=True,stage='preparing',detail='准备本轮原文件空口传输',mode='new_OTA_original_MP4',started_unix_ns=time.time_ns(),directory=str(out))
 previous_build=wf.build_bundle
 try:
  wf.build_bundle=build
  with ResourceWatch(m,out):m._work()
 finally:
  wf.build_bundle=previous_build
 return m.snapshot()
