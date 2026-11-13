#!/usr/bin/env python3
"""Finite unique dual-stream G0 IQ producer. No radio is opened by this tool."""
import argparse,errno,hashlib,json,os,select,time,zlib
from pathlib import Path
from g0_fec_session_waveform import frame_iq,public_prefix,rrc,PERIOD,SPS,RATE
from workspace_storage_guard import check,GIB

def main(a):
    if not 8<=a.frames<=3000 or not 0<=a.erase_sources<=12:
        raise ValueError('finite8..3000 periods/0..12 erasures required')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    before=check('before-stream-IQ');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
    assert before['disk']['available_bytes']>=5*GIB
    fifo=out/'iq.fifo';os.mkfifo(fifo,0o600)
    result={'completed':False,'RF_started_by_producer':False,'periods':a.frames,
            'session':a.session,'banks':8,'k':20,'n':32,'symbol_bytes':512,
            'descriptors_per_period':2,'data_packets_per_period':14,
            'intentional_SOURCE_CRC_erasures_per_block':a.erase_sources,
            'sample_rate':RATE,'duration_s':a.frames*PERIOD*SPS/RATE,
            'total_samples':a.frames*PERIOD*SPS,'bytes_written':0,'FEC_cache_peak_blocks':0,
            'full_packet_trace_enabled':False,'g0_or_RF_capacity_passed':False}
    fd=None;start=time.monotonic();digest=hashlib.sha256();crc=0
    try:
        print('G0_WAVE_FIFO_READY '+str(fifo),flush=True)
        deadline=time.monotonic()+60
        while fd is None:
            try:fd=os.open(fifo,os.O_WRONLY|os.O_NONBLOCK)
            except OSError as error:
                if error.errno!=errno.ENXIO:raise
                if time.monotonic()>=deadline:raise TimeoutError('No IQ reader in60seconds')
                time.sleep(.02)
        prefix=public_prefix();h=rrc();cache={};deadline=time.monotonic()+result['duration_s']+60
        for sf in range(a.frames):
            # Only the blocks touched by this period survive, independent of duration.
            first=sf*14//32
            for block in list(cache):
                if block<first:del cache[block]
            packed,_=frame_iq(a.session,sf,cache,a.erase_sources,prefix,h)
            result['FEC_cache_peak_blocks']=max(result['FEC_cache_peak_blocks'],len(cache))
            assert len(cache)<=2
            assert abs(packed).max()<=.125
            data=packed.tobytes();digest.update(data);crc=zlib.crc32(data,crc)
            view=memoryview(data)
            while view:
                if time.monotonic()>deadline:raise TimeoutError('Finite IQ producer wall deadline')
                if not select.select([],[fd],[],.2)[1]:continue
                try:n=os.write(fd,view)
                except BlockingIOError:continue
                view=view[n:];result['bytes_written']+=n
        result['completed']=True
    except BaseException as error:
        result['error']=repr(error);raise
    finally:
        if fd is not None:os.close(fd)
        result.update(elapsed_s=time.monotonic()-start,IQ_sha256=digest.hexdigest(),IQ_crc32=f'{crc:08x}')
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
        (out/'disk-after.json').write_text(json.dumps(check('after-stream-IQ'),indent=2)+'\n')
    print(json.dumps(result),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--frames',type=int,default=3000);p.add_argument('--session',type=lambda s:int(s,0),default=0x56334601)
    p.add_argument('--erase-sources',type=int,default=0);main(p.parse_args())
