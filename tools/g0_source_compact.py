#!/usr/bin/env python3
"""Bounded reader of the local V3BC evidence container, not an RF/DMA ABI.

Each entry retains the DMA header, valid record prefix and trailer, plus the
host-consumer CLOCK_MONOTONIC timestamp. Unused padding is reconstructed as zero.
Malformed DMA metadata is retained as a complete batch for ordinary validation.
"""
import struct
from pathlib import Path

HEADER=struct.pack('<4sHHII',b'V3BC',1,16,32768,0)
MAX_BYTES=64*1024*1024
MAX_BATCHES=65536

def batches(path):
    path=Path(path)
    if path.stat().st_size>MAX_BYTES:
        raise ValueError('evidence exceeds64MiB cap')
    with path.open('rb') as f:
        if f.read(16)!=HEADER:
            raise ValueError('invalid V3BC header')
        count=0;last=0;consumed=16
        while entry:=f.read(16):
            if len(entry)!=16:
                raise ValueError('truncated entry header')
            size,reserved,arrival=struct.unpack('<IIQ',entry)
            if reserved or not 64<=size<=32768 or size%8 or not arrival or arrival<last:
                raise ValueError('invalid entry size, flags or timestamp')
            count+=1
            if count>MAX_BATCHES:
                raise ValueError('batch count exceeds finite limit')
            consumed+=16+size
            if consumed>MAX_BYTES:
                raise ValueError('stream exceeds64MiB cap')
            data=f.read(size)
            if len(data)!=size:
                raise ValueError('truncated batch body')
            if size<32768:
                valid=size-64
                if data[:4]!=b'V3DB' or data[-32:-28]!=b'V3DE' or struct.unpack_from('<I',data,size-28)[0]!=valid:
                    raise ValueError('compact prefix disagrees with DMA trailer')
                data=data[:-32]+bytes(32768-size)+data[-32:]
            last=arrival
            yield arrival,data
