#!/usr/bin/env python3
"""Build native ARM queued DMA ownership probe against the verified actual kernel ABI."""
import argparse,hashlib,json,shutil,struct,subprocess
from pathlib import Path
from audit_g0_kernel_abi import sections,cstring
from workspace_storage_guard import check as disk_check,GIB

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);headers=a.kernel_headers.resolve();abi=a.abi.resolve()
 check=disk_check('before-native-DMA-ring-build');(out/'disk-before.json').write_text(json.dumps(check,indent=2)+'\n');assert check['disk']['available_bytes']>=5*GIB
 try:
  old=json.loads((abi/'receipt.json').read_text());assert old['passed'] and old['module_layout_checked'] and old['symbols_compared']>10000
  reference=(abi/'Module.symvers').read_bytes();assert (headers/'Module.symvers').read_bytes()==reference
  stem='g0_source_ring' if a.source_batches else 'g0_dma_ring'
  for src,name in [('ps/kernel/'+stem+'.c',stem+'.c'),('ps/include/'+stem+'.h',stem+'.h')]:shutil.copyfile(v/src,out/name)
  (out/'Makefile').write_text('obj-m += '+stem+'.o\n')
  with (out/'build.log').open('w') as log:subprocess.run(['make','-C',str(headers),'M='+str(out),'ARCH=arm','CROSS_COMPILE='+a.cross_prefix,'-j2','modules'],stdout=log,stderr=subprocess.STDOUT,check=True)
  data=(out/(stem+'.ko')).read_bytes();sec=sections(data);versions=sec['__versions'];assert versions[5]%64==0
  actual={f[1]:int(f[0],16) for row in reference.decode().splitlines() if len(f:=row.split())>=4};imports={}
  for off in range(versions[4],versions[4]+versions[5],64):
   crc=struct.unpack_from('<I',data,off)[0];name=cstring(data,off+4);assert actual[name]==crc,name;imports[name]=f'{crc:08x}'
  assert 'module_layout' in imports
  s=sec['.modinfo'];magic=[x.decode() for x in data[s[4]:s[4]+s[5]].split(b'\0') if x.startswith(b'vermagic=')]
  assert len(magic)==1 and magic[0].startswith('vermagic=6.12.0-xilinx ')
  sources=['ps/kernel/'+stem+'.c','ps/include/'+stem+'.h']
  if not a.source_batches:
   (out/'include').mkdir();shutil.copyfile(v/'ps/include/g0_dma_ring.h',out/'include/g0_dma_ring.h');shutil.copyfile(v/'ps/g0_dma_ring_probe.c',out/'g0_dma_ring_probe.c')
   with (out/'native-build.log').open('w') as log:subprocess.run([a.cross_prefix+'gcc','-O2','-std=gnu11','-Wall','-Wextra','-Werror','-static',str(out/'g0_dma_ring_probe.c'),'-o',str(out/'g0_dma_ring_probe')],stdout=log,stderr=subprocess.STDOUT,check=True)
   native=sections((out/'g0_dma_ring_probe').read_bytes());assert '.text' in native
   sources.append('ps/g0_dma_ring_probe.c')
  receipt={'scope':__doc__,'source_batch_profile':a.source_batches,'module':stem+'.ko','import_count':len(imports),'actual_kernel_imports':imports,'vermagic':magic[0],'module_layout_checked':True,'module_force_loaded':False,'source_sha256':{p:hashlib.sha256((v/p).read_bytes()).hexdigest() for p in sources},'g0_or_hardware_acceptance_passed':False}
  (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-native-DMA-ring-build'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','kernel-headers','abi']:p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--cross-prefix',required=True);p.add_argument('--source-batches',action='store_true');main(p.parse_args())
