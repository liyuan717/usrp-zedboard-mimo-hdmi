#!/usr/bin/env python3
"""Build the finite diagnostic DMA owner and matching raw-capture DT.

Requires separately prepared headers from the actual r9 embedded config and
the complete zImage export audit. Never force-load or patch module vermagic.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
from audit_g0_kernel_abi import sections, cstring


def build(a):
    project=Path(__file__).resolve().parents[1]
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    headers=a.kernel_headers.resolve();abi=a.abi.resolve()
    receipt=json.loads((abi/'receipt.json').read_text())
    if not receipt['passed'] or not receipt['module_layout_checked'] or receipt['symbols_compared']<10000:
        raise ValueError('full actual-kernel export comparison required')
    if (headers/'include/config/kernel.release').read_text().strip()!='6.12.0-xilinx':
        raise ValueError('prepared header release mismatch')
    reference=(abi/'Module.symvers').read_bytes()
    dest=headers/'Module.symvers'
    if dest.exists() and dest.read_bytes()!=reference:raise ValueError('refusing to replace a different ABI')
    dest.write_bytes(reference)
    shutil.copyfile(project/'ps/kernel/g0_adc_capture.c',out/'g0_adc_capture.c')
    (out/'Makefile').write_text('obj-m += g0_adc_capture.o\n')
    call=['make','-C',str(headers),'M='+str(out),'ARCH=arm','CROSS_COMPILE='+str(a.cross_prefix),'-j2','modules']
    with (out/'build.log').open('w') as log:subprocess.run(call,stdout=log,stderr=subprocess.STDOUT,check=True)
    data=(out/'g0_adc_capture.ko').read_bytes();sec=sections(data)
    ver=sec['__versions'];imports={}
    if ver[5]%64:raise ValueError('unsupported ARM module version layout')
    actual={f[1]:int(f[0],16) for line in reference.decode().splitlines() if len(f:=line.split())>=4}
    for pos in range(ver[4],ver[4]+ver[5],64):
        import struct
        name=cstring(data,pos+4);crc=struct.unpack_from('<I',data,pos)[0]
        if actual.get(name)!=crc:raise ValueError('module import ABI differs: '+name)
        imports[name]=f'{crc:08x}'
    if 'module_layout' not in imports:raise ValueError('module layout CRC not checked')
    m=sec['.modinfo'];modinfo=data[m[4]:m[4]+m[5]].split(b'\0')
    magic=[x.decode() for x in modinfo if x.startswith(b'vermagic=')]
    if len(magic)!=1 or not magic[0].startswith('vermagic=6.12.0-xilinx '):raise ValueError('built vermagic mismatch')
    dtc=headers/'scripts/dtc/dtc'
    with (out/'r9-base.dts').open('w') as dst,(out/'dtc-decompile.log').open('w') as log:
        subprocess.run([str(dtc),'-I','dtb','-O','dts',str(a.base_dtb.resolve())],stdout=dst,stderr=log,check=True)
    overlay=(project/'board/g0_adc.dtsi').resolve()
    (out/'g0-adc.dts').write_text('/include/ "r9-base.dts"\n/include/ "'+str(overlay)+'"\n')
    with (out/'dtc-compile.log').open('w') as log:
        subprocess.run([str(dtc),'-I','dts','-O','dtb','-o',str(out/'g0-adc.dtb'),str(out/'g0-adc.dts')],stdout=log,stderr=subprocess.STDOUT,check=True)
    dts=subprocess.check_output([str(dtc),'-I','dtb','-O','dts',str(out/'g0-adc.dtb')],stderr=subprocess.DEVNULL).decode()
    if 'adi,expect-s-axis-tlast;' in dts or 'compatible = "seu,video-radio-v3-g0-adc";' not in dts:
        raise ValueError('derived DT raw byte-count contract failed')
    (out/'abi.json').write_text(json.dumps({'scope':'G0 finite raw ADC driver only; not G0/G3 acceptance',
        'module_imports':imports,'import_count':len(imports),'mismatches':{},'vermagic':magic[0],
        'actual_kernel_export_audit':str(abi/'receipt.json'),'module_force_loaded':False,
        'dt_no_tlast_requirement':True,'g0_passed':False},indent=2)+'\n')
    print('G0 raw DMA owner compiled; '+str(len(imports))+' actual-kernel imports match')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel-headers',type=Path,required=True)
    p.add_argument('--abi',type=Path,required=True)
    p.add_argument('--cross-prefix',required=True)
    p.add_argument('--base-dtb',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    build(p.parse_args())
