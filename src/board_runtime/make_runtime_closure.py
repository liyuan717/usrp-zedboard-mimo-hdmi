#!/usr/bin/env python3
from pathlib import Path
import subprocess,re,json,hashlib,os
R=Path('/tmp/link-live');out={'schema':'native-video-runtime-closure-v1'}
def digest(p):return {'path':str(p.resolve()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
for role,loader,lib,elf in [('helper',R/'codec/lib/ld-linux-armhf.so.3',R/'codec/lib',R/'codec/bin/mimo-video-codec-helper'),('presenter',R/'presenter/lib/ld-linux-armhf.so.3',R/'presenter/lib',R/'presenter/hdmi-presenter')]:
 argv=[str(loader),'--library-path',str(lib),str(elf)];cmd=argv[:3]+['--list',argv[3]]
 text=subprocess.check_output(cmd,text=True,timeout=5)
 paths=set(next(part for part in pair if part) for pair in re.findall(r'=>\s+(/[^\s(]+)|^\s*(/[^\s(]+)\s+\(',text,flags=re.M))
 item={'argv':argv,'elf_argv_index':3,'elf':digest(elf),'libraries':[digest(Path(p)) for p in sorted(paths)],'loader_sha256':digest(loader)['sha256'],'loader_list_argv':cmd}
 if role=='helper':item['environment']={'MIMO_CODEC_CRC':'slice8','MIMO_CODEC_PROFILE':'1'}
 else:item['backend']='kms';item['argv']+=['--rgb-expand','neon','--stage-timing','--queue-wait-ms','3000','--drain-timeout-ms','5000','--read-timeout-ms','60000']
 out[role]=item
(R/'runtime-closure.json').write_text(json.dumps(out,indent=2)+'\n');print('runtime closure written')
