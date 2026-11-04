#!/usr/bin/env python3
"""Verify visible source and, when downloaded, the exact runtime package."""
from pathlib import Path
import argparse,hashlib,json,re
ROOT=Path(__file__).resolve().parents[2]
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  while b:=f.read(4<<20):h.update(b)
 return h.hexdigest()
def verify(root=ROOT,assets=False):
 root=Path(root).resolve();m=json.loads((root/'build/source_manifest.json').read_text())
 seen=set()
 for r in m['files']:
  p=root/r['path'];rel=Path(r['workspace_path'])
  if rel.is_absolute() or '..' in rel.parts or str(rel) in seen:raise ValueError('unsafe/duplicate workspace path')
  seen.add(str(rel))
  if not p.resolve().is_relative_to(root) or p.is_symlink() or p.stat().st_size!=r['bytes'] or sha(p)!=r['sha256']:raise ValueError('source mismatch: '+r['path'])
 for p in root.rglob('*'):
  if '.git' in p.relative_to(root).parts or '__pycache__' in p.parts:continue
  if not re.fullmatch('[a-z0-9_.-]+',p.name):raise ValueError('noncompliant filename: '+str(p))
 if assets:
  item=json.loads((root/'data/manifest.json').read_text())['runtime_package'];p=root/item['path']
  if p.stat().st_size!=item['bytes'] or sha(p)!=item['sha256']:raise ValueError('Runtime missing or still an LFS pointer; run build/scripts/download_assets.py or git lfs pull')
 return dict(valid=True,source_files=len(seen),runtime_package_verified=assets,hardware_test=False)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--assets',action='store_true');a=p.parse_args();print(json.dumps(verify(assets=a.assets),indent=2))
