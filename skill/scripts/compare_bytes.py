#!/usr/bin/env python3
import argparse,hashlib,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('reference',type=Path);p.add_argument('actual',type=Path);a=p.parse_args();h=hashlib.sha256();offset=0
with a.reference.open('rb') as x,a.actual.open('rb') as y:
 while True:
  b=x.read(1<<20);c=y.read(1<<20)
  if b!=c:raise SystemExit('byte mismatch in chunk beginning at '+str(offset))
  if not b:break
  h.update(b);offset+=len(b)
print(json.dumps(dict(byte_identical=True,bytes=offset,sha256=h.hexdigest())))
