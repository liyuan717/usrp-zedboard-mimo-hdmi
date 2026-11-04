from pathlib import Path
import subprocess,os,json,hashlib,time
R=Path('/tmp/link-live');B=R/'photo-seu-reobserve-r1';O=Path('/tmp/link-share/photo-seu-reobserve-r1')
A=Path('/sys/bus/iio/devices/iio:device0');LO=[A/'out_altvoltage0_RX_LO_powerdown',A/'out_altvoltage1_TX_LO_powerdown'];ADC=Path('/sys/bus/iio/devices/iio:device3/buffer/enable')
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==(R/'EXPECTED_BOOT_ID').read_text().strip()
assert Path('/proc/sys/kernel/tainted').read_text().strip()=='4096'
assert all(p.read_text().strip()=='1' for p in LO) and ADC.read_text().strip()=='0'
source=B/'received-live.rfd'
assert hashlib.sha256(source.read_bytes()).hexdigest()=='bd3fcd4ba939207f07d239ced500f29e019884dcddc530988bc17fbf64d831c5'
assert hashlib.sha256((B/'hdmi-presenter').read_bytes()).hexdigest()=='33414cfa2ba8ae6e48f946ce098f8d90e0b65465333069aa41acb0930b72f295'
O.mkdir(exist_ok=False)
with (R/'HDMI_SINGLE_ACTIVATION_CLAIM').open('x') as claim: claim.write('photo-seu-reobserve-r1\n')
before=subprocess.check_output(['/bin/busybox','dmesg']);(O/'kernel-before.log').write_bytes(before)
argv=[os.readlink('/proc/self/exe'),str(B/'receiver_offline_diag.py'),'--input',str(source),'--offline','--runtime-closure',str(B/'runtime-closure.json'),'--proof-output',str(O/'hdmi-proof.json'),'--report',str(O/'video-report.json'),'--session-id','714','--imbc-build-id','1','--max-records','42','--expected-access-units','1','--timeout-s','30','--offline-deadline-s','8','--max-pending','2','--diagnostic-offline-hold-ms','60000']
env=dict(os.environ,PYTHONPATH=str(R/'python')+':'+str(R/'python/tools'))
result={'scope':'re-display actual 20261030 r2 RF-received seu photo RFD; offline display-only, no new RF/ADC or PL result','new_RF':False,'new_ADC':False,'scanout_sampler':False,'host_UART_polling_during_hold':False,'visual_confirmation':None,'visual_pass':False,'valid':False}
try:
 with (O/'video.log').open('xb') as log:
  p=subprocess.Popen(argv,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  try:result['returncode']=p.wait(timeout=95)
  except subprocess.TimeoutExpired:
   import signal
   os.killpg(p.pid,signal.SIGTERM)
   try:p.wait(timeout=5)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=3)
   raise
 proof=json.loads((O/'hdmi-proof.json').read_text());video=json.loads((O/'video-report.json').read_text());text=(O/'video.log').read_text()
 result['proof']=proof;result['video']=video;result['hold_completed']='FINITE_VIDEO_HOLD_END' in text
 result['valid']=bool(result['returncode']==0 and video['valid'] and result['hold_completed'] and proof['presented_at_hdmi_boundary']==1 and proof['scanout_repeats']>=1000 and proof['backend_failures']==0 and proof['proof_failures']==0)
except BaseException as e:result['failure']=repr(e)
finally:
 time.sleep(15)  # Catch delayed post-teardown DRM timeouts before declaring clean.
 after=subprocess.check_output(['/bin/busybox','dmesg']);(O/'kernel-after.log').write_bytes(after)
 result['taint']=Path('/proc/sys/kernel/tainted').read_text().strip();result['LO']=[p.read_text().strip() for p in LO];result['ADC']=ADC.read_text().strip()
 result['new_kernel_errors']=[l for l in after[len(before):].decode(errors='replace').splitlines() if any(x in l for x in ['Oops','panic','BUG:','WARNING:','timed out'])]
 if result['taint']!='4096' or result['LO']!=['1','1'] or result['ADC']!='0' or result['new_kernel_errors']:result['valid']=False
 (O/'result.json').write_text(json.dumps(result,indent=2)+'\n');print('DISPLAY_REOBSERVE_DONE',result['valid'],flush=True)
