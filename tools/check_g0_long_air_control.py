#!/usr/bin/env python3
"""Offline coordinator flow tests; all UART/RF/network surfaces are replaced."""
import argparse,contextlib,json,re,threading,subprocess,os,zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import run_g0_source_long_air as m

def main(a):
    out=a.output.resolve();out.mkdir(exist_ok=False)
    data=a.padding.resolve().read_bytes();crc=f'{zlib.crc32(data):08x}'
    real_popen=subprocess.Popen;results=[]
    tx_fixture=out/'mock-TX-with-swap-option'
    tx_fixture.write_bytes(b'--swap-iq-channels\0')
    for mode in ['clean','clean-swapped','wrong-IQ-mapping','RX-setup-failure','RX-arm-failure','UART-timeout']:
        swap=mode in ('clean-swapped','wrong-IQ-mapping')
        expected_ok=mode in ('clean','clean-swapped')
        directory=out/mode;calls=[];go=threading.Event();done=threading.Event()
        state={'GO_sent':False,'lease_held':False}
        class Input:
            def write(self,text):
                assert calls[-1]=='RX-arm' and state['lease_held'];assert text=='GO\n'
                state['GO_sent']=True;go.set()
            def flush(self):pass
            def close(self):done.set();go.set()
        class Process:
            def __init__(self,producer=False):
                self.producer=producer;self.stdin=Input()
                if producer:
                    wave=directory/'wave';wave.mkdir();os.mkfifo(wave/'iq.fifo')
                    (wave/'receipt.json').write_text(json.dumps(dict(completed=True,
                        bytes_written=240000000*16,intentional_SOURCE_CRC_erasures_per_block=0)))
                self.stdout=self.lines()
            def lines(self):
                if swap:
                    yield ('G0_TX_IQ_MAPPING rf0=0 rf1=1\n' if mode=='wrong-IQ-mapping' else 'G0_TX_IQ_MAPPING rf0=1 rf1=0\n')
                yield 'G0_READY WAIT_GO\n'
                go.wait(2)
                if state['GO_sent']:
                    yield 'TX_BURST_ACK channel_mask=3\n'
                    yield 'TX completed: 240000000 dual-stream samples at 4 MSPS.\n'
                done.set()
            def poll(self):return 0 if done.is_set() else None
            def wait(self,timeout=None):
                assert done.wait(3);return 0
            def terminate(self):done.set();go.set()
        def popen(args,**kwargs):
            if args[0]=='sh':return real_popen(args,**kwargs)
            if args[0]!='/usr/bin/python3':assert ('--swap-iq-channels' in args)==swap
            return Process(producer=args[0]=='/usr/bin/python3')
        @contextlib.contextmanager
        def lease():
            state['lease_held']=True
            try:yield object()
            finally:state['lease_held']=False
        def board(serial,text,path,timeout,lease=None):
            assert state['lease_held'];name=path.stem;calls.append(name);path.write_text('SIMULATED UART\n')
            if mode=='RX-setup-failure' and name=='RX-setup':raise RuntimeError('simulated incompatible ENSM')
            if mode=='RX-arm-failure' and name=='RX-arm':raise RuntimeError('simulated RX not running')
            if mode=='UART-timeout' and name=='preflight':raise TimeoutError('simulated UART timeout')
            if name=='RX-result':
                remote='/tmp/v3-source/'+directory.name
                return (f'SOURCE_DMA batches=65536 hw_batches=65536 records=0 crc=00000000 exit=0 running=0 queued=0 user=0 poisoned=0 pinned=0\n'
                        f'SOURCE_COMPACT dump_bytes={len(data)} omitted_batches=0\n{crc} {remote}.v3bc\n').encode()
            return b''
        class Connection:
            def __init__(self):self.pos=0
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,*args):pass
            def recv(self,n):
                part=data[self.pos:self.pos+n];self.pos+=len(part);return part
        class Server:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def bind(self,*args):pass
            def listen(self,*args):pass
            def settimeout(self,*args):pass
            def getsockname(self):return (m.HOST,12345)
            def accept(self):return Connection(),(m.BOARD,1)
        args=SimpleNamespace(output=directory,gain_db=12,tx_binary=tx_fixture,session=0x56334601,swap_iq_channels=swap)
        with patch.object(m,'qualification',return_value=('mock-boot','00000000')),\
             patch.object(m,'HardwareLease',lease),patch.object(m,'command',board),\
             patch.object(m.subprocess,'Popen',popen),patch.object(m.socket,'socket',Server):
            try:m.main(args)
            except (RuntimeError,TimeoutError,ValueError):
                assert not expected_ok
            else:assert expected_ok
        receipt=json.loads((directory/'receipt.json').read_text())
        assert not state['lease_held']
        if expected_ok:
            assert state['GO_sent'] and receipt['completed'] and receipt['cleanup_LOs_off']
            assert receipt['logical_iq_streams_by_X310_RF_port']==([1,0] if swap else [0,1])
            assert receipt['remote_capture_copy_released']
            assert receipt['capture_review']['SOURCE_unique']==0
            assert not receipt['actual_SOURCE_byte_join_passed']
            assert not receipt['measured_steady_SOURCE_rate_meets_current_video_margin']
            assert not receipt['full_G0_or_video_passed']
        else:
            assert not state['GO_sent'] and not receipt['completed']
            if mode=='wrong-IQ-mapping':assert 'RX-arm' not in calls
            if mode=='UART-timeout':assert calls==['preflight'] # no blind UART cleanup/retry
            else:assert calls[-1]=='RX-idle'
        results.append({'mode':mode,'calls':calls,'GO_sent':state['GO_sent'],'passed':True})
    proof={'scope':__doc__,'cases':results,'RF_started_in_reality':False,'hardware_touched':False,
           'actual_RF_coordinator_integration_passed':False}
    (out/'receipt.json').write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--padding',type=Path,required=True);main(p.parse_args())
