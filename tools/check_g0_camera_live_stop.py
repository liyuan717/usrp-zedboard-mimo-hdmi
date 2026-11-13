#!/usr/bin/env python3
"""Actual live camera cancellation and FIFO disconnect cleanup; no TX/board.
Finite expected failures must restore camera control and release all owners.
"""
import argparse,json,os,select,subprocess,time
from pathlib import Path

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False);v=Path(__file__).resolve().parents[1];runs=[]
 for mode in ['SIGTERM','reader_disconnect']:
  wave=out/mode;fd=None;process=None
  with (out/(mode+'.log')).open('w') as log:
   try:
    process=subprocess.Popen(['/usr/bin/python3',str(v/'tools/g0_camera_live_stream.py'),'--output',str(wave)],stdout=log,stderr=subprocess.STDOUT)
    deadline=time.monotonic()+15
    while not (wave/'iq.fifo').exists():
     if process.poll() is not None:raise RuntimeError('live producer exited before stop test')
     if time.monotonic()>deadline:raise TimeoutError('stop FIFO ready')
     time.sleep(.02)
    fd=os.open(wave/'iq.fifo',os.O_RDONLY|os.O_NONBLOCK);start=time.monotonic();count=0
    while time.monotonic()-start<2:
     if not select.select([fd],[],[],.1)[0]:continue
     data=os.read(fd,65536)
     if not data:
      if count or process.poll() is not None:raise RuntimeError('early EOF in stop setup')
      time.sleep(.01);continue
     count+=len(data)
    if count<64000000:raise ValueError('stop must occur during active live IQ')
    stopped=time.monotonic()
    if mode=='SIGTERM':
     process.terminate()
     while process.poll() is None:
      if time.monotonic()-stopped>8:raise TimeoutError('live SIGTERM cleanup')
      if select.select([fd],[],[],.05)[0]:os.read(fd,65536)
    else:os.close(fd);fd=None
    rc=process.wait(timeout=8);elapsed=time.monotonic()-stopped
    p=json.loads((wave/'receipt.json').read_text())
    if rc==0 or p['completed'] or p['camera_worker_alive_after_cleanup']:raise ValueError('stop result incorrectly passed finite run')
    if p['original_dynamic_framerate']!=p['restored_dynamic_framerate']:raise ValueError('camera control not restored')
    for field in ['queued_SOURCE_owners_after_cleanup','scheduler_SOURCE_owners_after_cleanup','coded_owners_after_cleanup','audit_equation_owners_after_cleanup']:
     if p[field]:raise ValueError('unreleased owner '+field)
    expected='InterruptedError' if mode=='SIGTERM' else 'BrokenPipeError'
    if expected not in p.get('error',''):raise ValueError('unexpected stop failure reason')
    runs.append(dict(mode=mode,expected_nonzero_returncode=rc,active_IQ_bytes_before_stop=count,cleanup_seconds=elapsed,
     error=p['error'],original_restored_dynamic_framerate=[p['original_dynamic_framerate'],p['restored_dynamic_framerate']],
     all_SOURCE_coded_audit_owners_zero=True,camera_thread_exited=True))
   finally:
    if fd is not None:os.close(fd)
    if process is not None and process.poll() is None:
     process.terminate()
     try:process.wait(timeout=8)
     except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
 result=dict(completed=True,RF_started=False,USRP_opened=False,runs=runs)
 (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
