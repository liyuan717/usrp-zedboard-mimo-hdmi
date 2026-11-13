#!/usr/bin/env python3
"""Verify a reference ARM32 export ABI against the actual G0 zImage bytes.

This does not change module vermagic, CRCs, or the running kernel. It requires
the same complete export name/namespace sequence and every CRC to match the
reference ELF + Module.symvers, including module_layout. Only absolute ARM32
ksymtab records and contiguous Linux 6.12 kcrctab layout are supported.
"""
import argparse
import json
from pathlib import Path
import struct
import zlib


def sections(data):
    if data[:7]!=b'\x7fELF\x01\x01\x01':raise ValueError('ARM32 little-endian ELF required')
    h=struct.unpack_from('<16sHHIIIIIHHHHHH',data)
    if h[2]!=40:raise ValueError('reference is not ARM')
    sh=[struct.unpack_from('<10I',data,h[6]+i*h[11]) for i in range(h[12])]
    names=sh[h[13]];strings=data[names[4]:names[4]+names[5]]
    return {strings[s[0]:].split(b'\0',1)[0].decode():s for s in sh}


def cstring(data,offset):
    if not 0<=offset<len(data):raise ValueError('string address out of bounds')
    text=data[offset:offset+256].split(b'\0',1)
    if len(text)!=2:raise ValueError('unterminated export string')
    return text[0].decode('ascii')


def audit(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    elf=a.reference_vmlinux.read_bytes();sec=sections(elf)
    def estring(addr):
        for s in sec.values():
            if s[3] and s[3]<=addr<s[3]+s[5]:return cstring(elf,s[4]+addr-s[3])
        raise ValueError('ELF string address unmapped')
    expected=[];counts=[]
    for name,license in [('__ksymtab','EXPORT_SYMBOL'),('__ksymtab_gpl','EXPORT_SYMBOL_GPL')]:
        s=sec[name]
        if s[5]%12:raise ValueError('unsupported symbol-record width')
        counts.append(s[5]//12)
        for pos in range(s[4],s[4]+s[5],12):
            _,n,ns=struct.unpack_from('<III',elf,pos)
            expected.append((estring(n),estring(ns),license))
    reference={}
    for line in a.reference_symvers.read_text().splitlines():
        f=line.split('\t')
        if len(f)>=4 and f[2]=='vmlinux':reference[f[1]]=int(f[0],16)
    if set(reference)!={row[0] for row in expected}:raise ValueError('reference ELF/symvers export sets differ')
    compressed=a.zimage.read_bytes();images=[]
    for pos in range(len(compressed)-3):
        if compressed[pos:pos+3]!=b'\x1f\x8b\x08':continue
        try:raw=zlib.decompress(compressed[pos:],31)
        except zlib.error:continue
        if b'Linux version 6.12.0-xilinx ' in raw:images.append((pos,raw))
    if len(images)!=1:raise ValueError('one exact G0 kernel payload required')
    gzip_offset,image=images[0];base=sec['.head.text'][3]
    anchor=expected[0][0].encode()+b'\0';loc=image.find(anchor)
    if loc<0 or image.find(anchor,loc+1)>=0:raise ValueError('export anchor is not unique')
    pointer=struct.pack('<I',loc+base)
    candidates=[i-4 for i in range(4,len(image)-4,4) if image[i:i+4]==pointer]
    matches=[]
    for start in candidates:
        try:
            for i,(name,namespace,_) in enumerate(expected):
                _,n,ns=struct.unpack_from('<III',image,start+12*i)
                if cstring(image,n-base)!=name or cstring(image,ns-base)!=namespace:break
            else:matches.append(start)
        except (ValueError,UnicodeDecodeError,struct.error):pass
    if len(matches)!=1:raise ValueError('complete export table sequence does not match uniquely')
    start=matches[0];crc_start=start+12*len(expected);crc_end=crc_start+4*len(expected)
    rows=[];mismatch={}
    for i,(name,namespace,license) in enumerate(expected):
        _,n,ns=struct.unpack_from('<III',image,start+12*i)
        if min(n-base,ns-base)<crc_end:raise ValueError('strings overlap CRC table')
        crc=struct.unpack_from('<I',image,crc_start+4*i)[0]
        if crc!=reference[name]:mismatch[name]={'reference':reference[name],'actual':crc}
        rows.append(f'0x{crc:08x}\t{name}\tvmlinux\t{license}\t{namespace}\n')
    receipt={'scope':'G0 exact compressed-kernel export ABI comparison, not a hardware or G0 pass',
        'kernel':str(a.zimage.resolve()),'kernel_payload_bytes':len(image),'gzip_offset':gzip_offset,
        'export_counts':counts,'export_table_offset':start,'crc_table_offset':crc_start,
        'symbols_compared':len(expected),'mismatches':mismatch,'passed':not mismatch,
        'module_layout_checked':'module_layout' in reference,
        'source':'https://www.kernel.org/doc/html/latest/kbuild/modules.html'}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    if mismatch:raise ValueError('actual kernel CRC mismatch; refuse reference ABI')
    (out/'Module.symvers').write_text(''.join(rows))
    print(f'G0 kernel export ABI: {len(expected)} exact matches, no mismatches')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--zimage',type=Path,required=True)
    p.add_argument('--reference-vmlinux',type=Path,required=True)
    p.add_argument('--reference-symvers',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    audit(p.parse_args())
