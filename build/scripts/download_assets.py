#!/usr/bin/env python3
from pathlib import Path
import json,urllib.request,hashlib,os
ROOT=Path(__file__).resolve().parents[2];item=json.loads((ROOT/'data/manifest.json').read_text())['runtime_package'];p=ROOT/item['path'];p.parent.mkdir(exist_ok=True)
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  while b:=f.read(4<<20):h.update(b)
 return h.hexdigest()
if p.exists() and p.stat().st_size==item['bytes'] and sha(p)==item['sha256']:print('Already verified:',p);raise SystemExit(0)
if p.exists():
 with p.open('rb') as existing:prefix=existing.read(128)
 if not prefix.startswith(b'version https://git-lfs.github.com/spec/v1'):raise SystemExit('Existing file is neither verified data nor an LFS pointer; inspect it before retrying')
tmp=p.with_suffix(p.suffix+'.partial')
if tmp.exists():raise SystemExit('Partial output exists; inspect or move it first')
# Repository-owned, fixed download endpoint. No arbitrary URL/command interface.
url='https://media.githubusercontent.com/media/liyuan717/usrp-zedboard-mimo-hdmi/main/'+item['path']
h=hashlib.sha256();n=0
with urllib.request.urlopen(url,timeout=120) as src,tmp.open('xb') as dst:
 while b:=src.read(4<<20):dst.write(b);h.update(b);n+=len(b)
if n!=item['bytes'] or h.hexdigest()!=item['sha256']:raise SystemExit('Download size/SHA mismatch; partial data retained for diagnosis')
os.replace(tmp,p);print('Downloaded and SHA256 verified:',p)
