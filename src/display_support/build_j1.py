from pathlib import Path
import hashlib,json,os,resource,signal,subprocess,sys,time
C=Path(__file__).resolve().parent;W=C.parents[3];S=C/'source';mode=sys.argv[1];assert mode in ('host','arm','sanitizer')
B=C/('build-'+mode+'-r2');B.mkdir(exist_ok=False)
dep=W/'deliverables/usrp-zedboard-integration-20261026/release/verified-assets/build/hdmi_presenter_pipeline_20261026/r4_4/final/deps/video_codec_helper/diagnostics'
assert (dep/'crc32_slice8_candidate.h').exists()
compiler=str(W/'hardware-review/boot-fix-build/toolchain/bin/arm-buildroot-linux-gnueabihf-g++') if mode=='arm' else 'g++'
flags=['-std=c++17','-O2','-g','-Wall','-Wextra','-Wpedantic','-Werror','-pthread','-I'+str(S/'include'),'-I'+str(dep)]
if mode=='arm':flags+=['-mcpu=cortex-a9','-mfpu=neon','-mfloat-abi=hard']
if mode=='sanitizer':flags+=['-O1','-fsanitize=address,undefined','-fno-omit-frame-pointer']
rows=[]
def mem():return int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))
def cap():
 if mode!='sanitizer':resource.setrlimit(resource.RLIMIT_AS,(768<<20,768<<20))
 resource.setrlimit(resource.RLIMIT_CORE,(0,0));os.nice(10)
def run(name,argv,expected=0):
 assert mem()>1536*1024
 with (B/(name+'.stdout')).open('xb') as out,(B/(name+'.stderr')).open('xb') as err:
  p=subprocess.Popen(list(map(str,argv)),cwd=B,stdout=out,stderr=err,preexec_fn=cap,start_new_session=True);start=time.monotonic();low=mem()
  while p.poll() is None:
   low=min(low,mem())
   if low<768*1024 or time.monotonic()-start>120:os.killpg(p.pid,signal.SIGKILL);p.wait();raise RuntimeError('owned build/test guard '+name)
   time.sleep(.1)
 row=dict(name=name,argv=list(map(str,argv)),returncode=p.returncode,expected=expected,host_available_min_kib=low,elapsed_s=time.monotonic()-start);rows.append(row);(B/'STEPS.json').write_text(json.dumps(rows,indent=2)+'\n');print(name,p.returncode,flush=True)
 if p.returncode!=expected:raise RuntimeError(name+' failed; log preserved')
core=['protocol','rgb_expand','presenter','null_backend','drm_backend'];stems=core+['receiver','app_runner','main']
for stem in stems:run(stem,[compiler,*flags,'-c',S/'src'/(stem+'.cpp'),'-o',B/(stem+'.o')])
run('link-presenter',[compiler,*flags,*[B/(n+'.o') for n in stems],'-o',B/'hdmi-presenter'])
if mode!='arm':
 tests=['protocol_test','rgb_expand_test','presenter_test','tcp_receiver_test','codec_receiver_test','codec_session_runner_test','drm_session_guard_test','drm_discovery_test','source_pts_pacing_test']
 if mode=='sanitizer':tests=['source_pts_pacing_test','presenter_test','codec_session_runner_test']
 for n in tests:
  extra=['receiver'] if n in ('tcp_receiver_test','codec_receiver_test') else ['receiver','app_runner'] if n=='codec_session_runner_test' else []
  wraps=['-Wl,--wrap=open,--wrap=close,--wrap=mmap,--wrap=munmap,--wrap=ioctl'] if n=='drm_discovery_test' else []
  run(n+'-build',[compiler,*flags,'-UNDEBUG',S/'tests'/(n+'.cpp'),*[B/(s+'.o') for s in core+extra],*wraps,'-o',B/n])
  run(n+'-run',[B/n])
 if mode=='host':
  for name in ('cli_codec_completion_test.sh','cli_proof_existing_path_test.sh'):run(name,['sh',S/'tests'/name,B/'hdmi-presenter'])
  for i,bad in enumerate(('-1','garbage','18446744073709551616')):run('reject-max-'+str(i),[B/'hdmi-presenter','--backend','null','--codec-output-fd','0','--max-records',bad],2)
  run('pacing-requires-idle-refresh',[B/'hdmi-presenter','--backend','null','--codec-output-fd','0','--source-pts-pacing'],2)
(B/'RESULT.json').write_text(json.dumps(dict(valid=True,mode=mode,hardware_access=False,all_passed=True,commands=len(rows),presenter_sha256=hashlib.sha256((B/'hdmi-presenter').read_bytes()).hexdigest()),indent=2)+'\n')
