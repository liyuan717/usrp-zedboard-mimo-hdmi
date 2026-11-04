"""Real ARM decoder NULL backend over newly prepared original MP4 fixtures. No RF."""
from pathlib import Path
import os,sys,json,subprocess,re,hashlib,tarfile,time
APP=Path(__file__).resolve().parents[1];sys.path.insert(0,str(APP))
from board_mp4 import playback_rfd,access_units
from original_backend import make_manager,prepare,PROTO,D
from video_control import DEFAULTS
import threading
OUT=APP/'tests/original-integration-20261101'/sys.argv[1];OUT.mkdir()
runtime=D/'resume6-candidate/runtime';q=D/'standalone-video-acceptance-20261030/private/tools/qemu-extracted/usr/bin/qemu-arm';root=D/'standalone-video-acceptance-20261030/private/image-build/root-tree'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
sys.path.insert(0,str(PROTO/'protocol'))
results=[]
sources=[Path(sys.argv[2]).resolve()] if len(sys.argv)>2 else sorted(Path('/home/leeeo/Downloads/videos').glob('*.mp4'))
for source in sources:
 out=OUT/source.stem;out.mkdir();start=time.monotonic()
 if len(sys.argv)>2:
  spec=dict(file_sha256=sha(source))
 else:
  cls,build,spec=make_manager(source,out,DEFAULTS,threading.Event(),lambda v:None)
  build(out/'app.tar.gz','a'*32,'00000000-0000-0000-0000-000000000000','offline-only')
 with tarfile.open(out/'app.tar.gz') if len(sys.argv)==2 else __import__('contextlib').nullcontext(None) as t:
  for member in t.getmembers() if t is not None else []:
   assert member.isfile() and '/' not in member.name
  rows=json.load(t.extractfile('FILES.json')) if t is not None else []
  for row in rows:assert hashlib.sha256(t.extractfile(row['name']).read()).hexdigest()==row['sha256']
  assert t is None or not any(n.name.endswith(('.mp4','.rfd','.iqb','.h264','.png')) for n in t.getmembers())
 closure={'schema':'native-video-runtime-closure-v1'}
 for role,lib,elf in [('helper',runtime/'codec/lib',runtime/'codec/bin/mimo-video-codec-helper'),('presenter',runtime/'presenter/lib',APP.parent/'reception-display-optimization/20261030/candidates/hdmi-source-pts-r1/build-arm-r2/hdmi-presenter')]:
  argv=list(map(str,[q,'-cpu','cortex-a9',lib/'ld-linux-armhf.so.3','--library-path',lib,elf]));listing=argv[:6]+['--list',argv[6]]
  text=subprocess.check_output(listing,text=True,timeout=10)
  paths=set(next(p for p in pair if p) for pair in re.findall(r'=>\s+(/[^\s(]+)|^\s*(/[^\s(]+)\s+\(',text,flags=re.M))
  item=dict(argv=argv,elf_argv_index=6,elf=dict(path=str(elf),sha256=sha(elf)),libraries=[dict(path=p,sha256=sha(p)) for p in sorted(paths)],loader_sha256=sha(q),loader_list_argv=listing)
  if role=='helper':item['environment']={'MIMO_CODEC_CRC':'slice8','MIMO_CODEC_PROFILE':'1'}
  else:item.update(backend='null');item['argv']+=['--rgb-expand','neon','--queue-wait-ms','3000','--read-timeout-ms','30000','--drain-timeout-ms','5000','--repeat-displayed-on-idle','--source-pts-pacing']
  closure[role]=item
 (out/'closure.json').write_text(json.dumps(closure))
 adapter=playback_rfd(source,out/'TX-fixture-playback.rfd')
 script=APP.parent/'reception-display-optimization/20261030/candidates/packet-paced-display-r1/receiver_offline_diag.py'
 argv=list(map(str,[q,'-cpu','cortex-a9','-L',root,root/'usr/bin/python3.12',script,'--input',out/'TX-fixture-playback.rfd','--offline','--runtime-closure',out/'closure.json','--proof-output',out/'proof.json','--report',out/'report.json','--session-id','714','--imbc-build-id','1','--expected-access-units','25','--max-records',adapter['records'],'--max-pending','2','--timeout-s','30','--offline-deadline-s','30','--diagnostic-time-scale','1']))
 env=dict(os.environ,PYTHONHOME=str(root/'usr'),PYTHONPATH=str(runtime/'python')+':'+str(runtime/'python/tools'),PYTHONDONTWRITEBYTECODE='1',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
 for key in ['LD_PRELOAD','LD_AUDIT','LD_LIBRARY_PATH']:env.pop(key,None)
 with (out/'codec.stdout').open('xb') as stdout,(out/'codec.stderr').open('xb') as stderr:
  p=subprocess.run(argv,env=env,stdout=stdout,stderr=stderr,timeout=60)
 report=json.loads((out/'report.json').read_text());proof=json.loads((out/'proof.json').read_text())
 assert p.returncode==0 and report['valid'] and len(report['accepted'])==25,(source,report.get('failure'))
 assert report['terminal']['workers_alive']==[] and proof['physical'] is False
 results.append(dict(file=source.name,file_sha256=sha(source),spec=spec,ARM_decode_AUs=25,physical_RF_verified=False,physical_HDMI_verified=False,elapsed_s=time.monotonic()-start))
 (OUT/'RESULT.json').write_text(json.dumps(dict(valid=len(results)==len(sources),tests=results,scope='Offline original-file framing and real ARM NULL decode only'),indent=2))
 print(source.name,'ARM NULL decode25 PASS',flush=True)
