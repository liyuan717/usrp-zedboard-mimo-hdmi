#!/usr/bin/env python3
"""Shared session/NAL/AU/DMA ownership controls and actual ARM probe build.
Host timings are not native throughput. Synthetic payloads are not RF video.
"""
import argparse,hashlib,json,os,resource,shutil,subprocess
from pathlib import Path
from workspace_storage_guard import check,GIB

SOURCES=['ps/g0_source_session_probe.cpp','ps/g0_video_fragment.c','ps/g0_video_au.c','ps/g0_dma_batch.c',
 'ps/include/g0_source_session.hpp','ps/include/g0_source_video_ingress.hpp','ps/include/g0_au_decode_queue.hpp',
 'ps/include/g0_video_fragment.h','ps/include/g0_video_au.h','ps/include/g0_dma_batch.h',
 'ps/include/g0_source_age.hpp','ps/include/g0_source_ring.h']

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 before=check('before-SOURCE-session-controls');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise RuntimeError('storage threshold')
 root=out/'sources'
 for name in SOURCES:
  target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(v/name,target)
 commands=[];result=dict(completed=False,RF_started=False,native_passed=False,full_G0_or_video_passed=False)
 def run(name,args,**kwargs):
  commands.append(args)
  p=subprocess.run(args,capture_output=True,text=True,timeout=60,**kwargs)
  (out/(name+'.stdout')).write_text(p.stdout);(out/(name+'.stderr')).write_text(p.stderr)
  if p.returncode:raise RuntimeError(name+' failed; inspect log')
  return p.stdout
 def no_core():resource.setrlimit(resource.RLIMIT_CORE,(0,0))
 try:
  host=[]
  for mode,flags in [('normal',['-O2']),('sanitized',['-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer'])]:
   objects=[]
   for stem in ['g0_video_fragment','g0_video_au','g0_dma_batch']:
    obj=out/(mode+'-'+stem+'.o');run(mode+'-'+stem,['gcc','-std=c11','-Wall','-Wextra','-Werror',*flags,'-c',str(root/'ps'/(stem+'.c')),'-o',str(obj)]);objects.append(str(obj))
   binary=out/('probe-'+mode)
   run('compile-'+mode,['g++','-std=c++17','-Wall','-Wextra','-Werror','-pthread',*flags,str(root/'ps/g0_source_session_probe.cpp'),*objects,'-o',str(binary)])
   env={**os.environ,'ASAN_OPTIONS':'detect_leaks=1:abort_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'}
   r=json.loads(run('run-'+mode,[str(binary)],env=env,preexec_fn=no_core));assert r['completed'] and r['byteexact_AUs']==5
   host.append(r)
  assert host[0]==host[1]
  faults=[]
  session='ps/include/g0_source_session.hpp';au='ps/g0_video_au.c'
  mutations={
   'remove_session_lifetime_epoch_guard':(session,'if(older(epoch,first_epoch_))return kReject;','/* intentionally wrong: old A lifetime accepted */'),
   'reject_useful_same_session_old_epoch':(session,'if(sid==sid_){','if(older(epoch,epoch_))return kReject;\n  if(sid==sid_){'),
   'allow_foreign_session_same_epoch':(session,'if(epoch==epoch_||older(epoch,epoch_))return kReject;','if(older(epoch,epoch_))return kReject;'),
   'AU_idle_from_first_NAL':(au,'local-c->last_local>=V3AU_IDLE_US','local-c->first_local>=V3AU_IDLE_US'),
   'AU_no_absolute_hold_bound':(au,'local-c->first_local>=V3AU_MAX_HOLD_US','0'),
   'AU_invalid_input_refreshes_idle':(au,'if(!n||!data','c->last_local=local;\n if(!n||!data')}
  for name,(path,old,new) in mutations.items():
   case=out/name;shutil.copytree(root/'ps',case/'ps');original=(root/path).read_text();assert original.count(old)==1
   (case/path).write_text(original.replace(old,new,1))
   binary=case/'probe';objects=[str(out/('normal-'+s+'.o')) for s in ['g0_video_fragment','g0_video_au','g0_dma_batch']]
   if path==au:
    obj=case/'au.o';run('compile-C-'+name,['gcc','-std=c11','-O2','-Wall','-Wextra','-Werror','-c',str(case/path),'-o',str(obj)])
    objects[1]=str(obj)
   run('compile-'+name,['g++','-std=c++17','-O2','-Wall','-Wextra','-Werror','-pthread',str(case/'ps/g0_source_session_probe.cpp'),*objects,'-o',str(binary)])
   p=subprocess.run([str(binary)],capture_output=True,text=True,timeout=10,preexec_fn=no_core)
   (case/'stdout').write_text(p.stdout);(case/'stderr').write_text(p.stderr)
   assert p.returncode and 'Assertion' in p.stderr and 'failed' in p.stderr
   faults.append(dict(name=name,compiled=True,functional_failure_returncode=p.returncode,witness=p.stderr.strip()))
  compile=json.loads((a.decoder_build/'commands.json').read_text())[0];cxx=compile[0];cc=cxx[:-3]+'gcc'
  flags=['-O2','-mcpu=cortex-a9','-mfpu=neon','-mfloat-abi=hard','-Wall','-Wextra','-Werror']
  objects=[]
  for stem in ['g0_video_fragment','g0_video_au','g0_dma_batch']:
   obj=out/('arm-'+stem+'.o');run('arm-'+stem,[cc,'-std=c11',*flags,'-c',str(root/'ps'/(stem+'.c')),'-o',str(obj)]);objects.append(str(obj))
  binary=out/'g0-source-session-probe'
  run('arm-link',[cxx,'-std=c++17','-static','-pthread',*flags,str(root/'ps/g0_source_session_probe.cpp'),*objects,'-o',str(binary)])
  result.update(completed=True,host_normal_ASan_UBSan_byteexact=True,host_result=host[0],
   compiled_functional_faults=faults,ARM_compiled=True,ARM_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
   source_sha256={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in SOURCES})
 finally:
  (out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n');(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-SOURCE-session-controls'),indent=2)+'\n')
 print(json.dumps(result),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--decoder-build',type=Path,required=True)
 p.add_argument('--output',type=Path,required=True);main(p.parse_args())
