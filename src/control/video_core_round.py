"""Fixed finite RF131 video backend using unchanged console control methods.

No web source, SD or boot asset changes. Only this fresh boot's ADC-derived RFD
can reach its display stage. Local DISPLAY_START is an observation trigger.
"""
from pathlib import Path
import fcntl,hashlib,inspect,io,json,os,secrets,sys,tarfile,textwrap,time,uuid
W=Path(__file__).resolve().parents[3];R=W/'reception-display-optimization/20261030';D=W/'deliverables/usrp-zedboard-integration-20261026/realtime-hdmi';BASE=D/'short-video-staged-r2-20261030';C=R/'candidates/repetition-bound-r2'
sys.path.insert(0,str(W/'photo-console'))
import workflow as wf
from config import APP,STATE,sha,verify_assets
MODE=sys.argv[1];assert MODE in ('--prepare-only','--run');O=Path(sys.argv[2]).resolve();O.mkdir(exist_ok=False)
variant=sys.argv[3] if len(sys.argv)>3 else 'candidate';assert variant in ('baseline','candidate')
N=(C/'arm-compatibility-r3/live-rx-arm') if variant=='candidate' else BASE/'live-rx-arm'
assert sha(N)==('01afeb4925d2568603a3c5e23d7899242c3110344c907db3f80d0efa7a30bce1' if variant=='candidate' else 'e585e1eca65c759c39b1282cea0c8ff73148090802bc2fe4f033712f62dae699')
verify_assets()
for n,h in json.loads((BASE/'CANDIDATE-MANIFEST.json').read_text())['critical_sha256'].items():assert sha(n)==h,n
wf.EVIDENCE_NAMES=wf.EVIDENCE_NAMES|{'RF_READY.json'}


def build(path,run_id,boot_id,secret):
 source=(BASE/'board_supervisor.py').read_text()
 source=source.replace("'/tmp/link-share/short-video-staged-r2-au-hdmi'","'/tmp/link-share/photo-console'").replace('short-video-staged-r2','photo-console')
 needle="result = dict(scope=auth['scope'], valid=False, new_RF=True, new_ADC=True,"
 assert source.count(needle)==1;source=source.replace(needle,needle+"\n                  run_id=auth['run_id'],boot_id=auth['expected_boot_id'],")
 needle="result['video_returncode'] = child.wait(timeout=105)"
 assert source.count(needle)==1
 source=source.replace(needle,"""from quiet_hold import wait_for_quiet_hold
            def quiet_snapshot():
                return dict(counters(),taint=int(Path('/proc/sys/kernel/tainted').read_text()),
                            LO=[p.read_text().strip() for p in LO],ADC=ADC.read_text().strip())
            result['quiet_display'] = wait_for_quiet_hold(child,deadline=time.monotonic()+105,
                stop_requested=STOP.exists,snapshot=quiet_snapshot,baseline_halts=initial['ring_halts'])
            result['video_returncode'] = child.poll()""")
 needle="subprocess.run([python, str(B / 'make_runtime_closure.py')], check=True, timeout=15)"
 assert source.count(needle)==1;source=source.replace(needle,"time.sleep(10)  # Observer preparation, after this-run RF gate.\n        "+needle)
 compile(source,'video-board-supervisor.py','exec')
 agent=(APP/'board_agent.py').read_text();needle="'interrupts-after.log', 'supervisor.log'";assert agent.count(needle)==1;agent=agent.replace(needle,"'interrupts-after.log', 'RF_READY.json', 'supervisor.log'")
 needle="elif 'LIVE_FINITE_ADC_CLOSED' in native:";assert agent.count(needle)==1;agent=agent.replace(needle,"elif (O / 'RF_READY.json').is_file():\n            value['state'] = 'rf_ready'\n            value['rf_ready'] = json.loads((O / 'RF_READY.json').read_text())\n        "+needle)
 compile(agent,'video-board-agent.py','exec')
 files={'board_supervisor.py':source.encode(),'board_agent.py':agent.encode(),'live-rx-arm':N.read_bytes(),'quiet_hold.py':(R/'candidates/quiet-photo-hold-r1/quiet_hold.py').read_bytes()}
 for n in ('hdmi-presenter','make_runtime_closure.py','receive_gate.py','acceptance_gate.py'):files[n]=(BASE/n).read_bytes()
 for n in ('receiver_offline_diag.py','diagnostic_pacer.py'):files[n]=(R/'candidates/packet-paced-display-r1'/n).read_bytes()
 auth=dict(authorized=True,scope='finite RF131 ten AUs; fresh ADC and real PL, then paced HDMI from this run only',run_id=run_id,expected_boot_id=boot_id,api_key=secret,maximum_frames=131,unique_frames=128,ADC_blocks=2048,display_time_scale=5,HDMI_hold_ms=60000,SD_write=False,tx_gain_db=18.0,native_sha256=sha(N),presenter_sha256=sha(BASE/'hdmi-presenter'))
 files['AUTHORIZATION.json']=json.dumps(auth).encode();rows=[dict(name=n,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()) for n,raw in files.items()];files['FILES.json']=json.dumps(rows).encode()
 assert not any(n.endswith(('.png','.mp4','.rfd','.rxth','.h264','.iqb')) for n in files)
 with tarfile.open(path,'x:gz') as t:
  for n,raw in files.items():
   info=tarfile.TarInfo(n);info.size=len(raw);info.mode=0o755 if n in ('hdmi-presenter','live-rx-arm') else 0o600;t.addfile(info,io.BytesIO(raw))
 review=dict(valid=True,variant=variant,source='unchanged frozen finite RF131/10AU staged controller; fresh IDs/paths + quiet stop monitor + packet pacer',native_sha256=sha(N),runtime_boot_drivers_presenter_PL_CRC_unchanged=True,source_media='1.mp4 first2seconds, original5fps10AUs',maximum_RF_bursts=131,new_ADC_blocks=2048,display_nominal_fps=1,hold_ms=60000,no_media_or_IQ_deployed=True,SD_written=False,web_source_changed=False,files=rows)
 (O/'BUNDLE-REVIEW.json').write_text(json.dumps(review,indent=2)+'\n');return rows


class VideoManager(wf.Manager):
 def preflight(self):
  super().preflight()
  wf.write_json(self.run/'video-profile.json',dict(finite_RF_bursts=131,unique_RF_frames=128,ADC_blocks=2048,session_id=714,video_AUs=10,nominal_source_fps=5,diagnostic_display_fps=1,frozen_IQB_sha256=sha(D/'live-baseline/frozen-video-tx/video-tx.iqb'),native_sha256=sha(N),SD_written=False))
 def update(self,stage=None,detail=None,**fields):
  if detail:detail=detail.replace('照片','短视频').replace('42 个','131 个')
  return super().update(stage,detail,**fields)
 def status(self):
  value=super().status()
  if value['state']=='rf_ready' and not getattr(self,'display_triggered',False):
   ready=value['rf_ready']
   assert len(ready['RFD_sha256'])==64 and all(c in '0123456789abcdef' for c in ready['RFD_sha256'])
   assert ready['RF_records'] in range(128,132) and [a['sequence'] for a in ready['public_AUs']]==list(range(10))
   assert type(ready['native_elapsed_ns']) is int and ready['native_elapsed_ns']>0
   wf.write_json(self.run/'RF_READY.json',ready)
   self.update('ready_to_display','本轮新 ADC 已完整恢复十个 AU，等待观察窗口启动；RF/ADC 已关闭')
   if (self.run/'DISPLAY_START').exists():
    self.command('touch /tmp/link-live/photo-console.display','trigger-this-run-display',5);self.display_triggered=True
  return value
 def grade(self,evidence):
  b=json.loads((evidence/'result.json').read_text());assert b['run_id']==self.state['run_id'] and b['boot_id']==self.state['boot_id']
  code='import sys,json;sys.path.insert(0,sys.argv[1]);from grade_evidence import grade;from pathlib import Path;print(json.dumps(grade(Path(sys.argv[2]))))'
  p=wf.subprocess.run(['/usr/bin/python3','-c',code,str(BASE),str(evidence)],capture_output=True,text=True,timeout=25)
  (self.run/'grade.stdout').write_text(p.stdout);(self.run/'grade.stderr').write_text(p.stderr);assert p.returncode==0,p.stderr
  g=json.loads(p.stdout);wf.write_json(self.run/'independent-grade.json',g)
  assert b['rx_lo_powerdown']==b['tx_lo_powerdown']=='1' and b['adc_buffer_enable']=='0' and not b['kernel_error_lines']
  for other in R.glob('*/result.json'):
   if other.parent!=self.run:assert json.loads(other.read_text()).get('ADC_sha256')!=g['ADC']['sha256'],'ADC matches previous round'
  proof=b['hdmi_proof'];return dict(technical_pass=True,new_RF=True,new_ADC=True,historical_redisplay=False,run_id=self.state['run_id'],boot_id=self.state['boot_id'],ADC_blocks=g['ADC']['blocks'],ADC_sha256=g['ADC']['sha256'],RF_frames=len(b['received_frame_ids']),unique_RF_frames=g['unique_frames'],CRC_correct_packets=g['CRC_correct_byte_identical_packets'],AUs=g['AUs'],HDMI_frames=proof['presented_at_hdmi_boundary'],same_buffer_scanout_repeats=proof['scanout_repeats'],LO_closed=True,ADC_closed=True,kernel_errors=[],visual_confirmation=None,continuous_realtime_verified=False)

# Retain the already tested bounded sender/timing/control method verbatim,
# changing only the fixed media source and exact finite sample count.
sender=textwrap.dedent(inspect.getsource(wf.Manager.transmit));assert sender.count('161280')==1 and sender.count('frames=42')==1
sender=sender.replace('161280','503040').replace('frames=42','frames=131')
namespace=dict(wf.__dict__,BASE=BASE);exec(compile(sender,'fixed-video-transmit.py','exec'),namespace);VideoManager.transmit=namespace['transmit']
if MODE=='--prepare-only':
 build(O/'candidate-app.tar.gz',uuid.uuid4().hex,str(uuid.uuid4()),secrets.token_hex(32));print('FINITE_VIDEO_BUNDLE_PREPARED');raise SystemExit(0)

available=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')));assert available>1536*1024
m=VideoManager();m.run=O;m.secret=secrets.token_hex(32);m.state=dict(run_id=uuid.uuid4().hex,active=True,stage='preparing',mode='new_OTA_finite_video_'+variant,historical_redisplay=False,visual_confirmation=None,started_unix_ns=time.time_ns());m.update();wf.build_bundle=build
fd=os.open(STATE/'operation.lock',os.O_CREAT|os.O_RDWR|os.O_CLOEXEC,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);m.lock_fd=fd
from resource_watch import ResourceWatch
with ResourceWatch(m,O):m._work()
print(json.dumps(m.snapshot(),ensure_ascii=False),flush=True)
