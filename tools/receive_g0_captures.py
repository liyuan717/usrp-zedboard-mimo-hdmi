#!/usr/bin/env python3
"""Receive bounded diagnostic captures from the explicitly addressed board."""
import argparse
import json
from pathlib import Path
import socket
import zlib


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bind',default='169.254.209.83')
    p.add_argument('--board',default='169.254.209.84')
    p.add_argument('--port',type=int,default=8014)
    p.add_argument('--bytes',type=int,nargs='+',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if any(n<=0 or n>32*1024*1024 for n in a.bytes):raise ValueError('capture size outside G0 owner bounds')
    a.output.mkdir(parents=True,exist_ok=False)
    receipts=[]
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        server.bind((a.bind,a.port));server.listen(1);server.settimeout(45)
        print('Listening for '+str(len(a.bytes))+' bounded G0 captures',flush=True)
        for index,size in enumerate(a.bytes):
            connection,peer=server.accept()
            with connection:
                if peer[0]!=a.board:raise ValueError('unexpected capture sender')
                connection.settimeout(15);data=bytearray()
                while True:
                    chunk=connection.recv(min(65536,size+1-len(data)))
                    if not chunk:break
                    data.extend(chunk)
                    if len(data)>size:raise ValueError('capture exceeds declared size')
                if len(data)!=size:raise ValueError('truncated capture')
            path=a.output/f'capture-{index:03d}.raw';path.write_bytes(data)
            row={'file':path.name,'bytes':len(data),'crc32':f'{zlib.crc32(data):08x}','peer':peer[0]}
            receipts.append(row);print(json.dumps(row),flush=True)
    (a.output/'transport.json').write_text(json.dumps(receipts,indent=2)+'\n')
