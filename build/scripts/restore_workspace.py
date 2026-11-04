#!/usr/bin/env python3
from pathlib import Path
import argparse,hashlib,json,os,shutil,subprocess,sys,tarfile
sys.dont_write_bytecode=True
from verify import ROOT,verify
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args();verify(assets=True);dest=a.output.absolute()
if dest.exists():raise SystemExit('Output must not exist; never overwrite the current project')
# Extract a verified, local package into an owned staging directory. No SD/system write.
item=json.loads((ROOT/'data/manifest.json').read_text())['runtime_package'];dest.mkdir(parents=True);stage=dest/'distribution_runtime';stage.mkdir()
proc=subprocess.Popen(['zstd','-q','-d','-c',str(ROOT/item['path'])],stdout=subprocess.PIPE)
with tarfile.open(fileobj=proc.stdout,mode='r|') as tar:
 for m in tar:
  name=Path(m.name)
  if name.is_absolute() or '..' in name.parts or not (m.isfile() or m.isdir()):raise ValueError('unsafe runtime package member')
  q=stage/name
  if m.isdir():q.mkdir(parents=True,exist_ok=True)
  else:
   q.parent.mkdir(parents=True,exist_ok=True)
   with tar.extractfile(m) as src,q.open('xb') as out:shutil.copyfileobj(src,out,4<<20)
 while proc.stdout.read(4<<20):pass
if proc.wait()!=0:raise ValueError('zstd failed')
rows=json.loads((ROOT/'build/source_manifest.json').read_text())['files']
for r in rows:
 q=dest/r['workspace_path'];q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/r['path'],q);q.chmod(r['mode'])
notes=[];assets=json.loads((ROOT/'board/asset_manifest.json').read_text())['files']
for r in assets:
 rel=Path(r['workspace_path'])
 if rel.is_absolute() or '..' in rel.parts:raise ValueError('unsafe asset path')
 if r['kind']!='file':continue
 q=dest/rel;src=stage/r['path']
 if q.exists():raise ValueError('duplicate restored source/asset')
 h=hashlib.sha256()
 with src.open('rb') as f:
  while b:=f.read(4<<20):h.update(b)
 if src.stat().st_size!=r['bytes'] or h.hexdigest()!=r['sha256']:raise ValueError('runtime file mismatch')
 q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,q);q.chmod(r['mode'])
for r in assets:
 if r['kind']!='symlink':continue
 q=dest/r['workspace_path'];q.parent.mkdir(parents=True,exist_ok=True)
 if Path(r['target']).is_absolute() or not (q.parent/r['target']).resolve().is_relative_to(dest):notes.append(r);continue
 if q.exists() or q.is_symlink():raise ValueError('link path conflict')
 q.symlink_to(r['target'])
# The reference filesystem remains an archive; it is not installed to any disk.
shutil.copyfile(stage/'base_rootfs.cpio.gz',dest/'reference_base_rootfs.cpio.gz')
shutil.rmtree(stage)
report=dict(restored=True,source_files=len(rows),runtime_entries=len(assets),deferred_external_links=notes,SD_written=False,hardware_access=False,legacy_recorded_workspace='/home/leeeo/Documents/ChatGPT/zedboard+fmcomms3')
(dest/'restore_report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
