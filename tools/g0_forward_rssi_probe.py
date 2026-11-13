#!/usr/bin/env python3
"""Finite analog-path RSSI observation for zero-SOURCE diagnosis; no PL changes or video signoff."""
import argparse,json,queue,re,subprocess,threading,time
from pathlib import Path
from board_command import command
from hardware_lease import HardwareLease
from workspace_storage_guard import check,GIB
SERIAL='/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00'

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
 before=check('before-finite-RSSI-forward');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 wave=v/'build/g0-fec-session-clean-wave-r1/iq.fc32';assert wave.stat().st_size>=2000000*16
 result={'scope':__doc__,'frequency_hz':5500000000,'TX_gain_db':12,'RX_gain_db':60,'sample_rate_hz':4000000,'RX_bandwidth_hz':4000000,'TX_LO_offset_hz':1000000,'TX_duration_s':.5,'TX_max_samples':2000000,'ports':'X3100/RX1A vertical; X3101/RX2A horizontal','board_TX_off':True,'SD_written':False,'PL_changed':False,'DMA_started':False,'RF_started':False,'cleanup_LOs_off':False,'g0_or_video_or_phase_measurement_passed':False,'host_intervals':{}}
 tx=None;uart_ready=True
 try:
  with HardwareLease() as lease:
   def run(name,text,timeout=20):
    nonlocal uart_ready
    subprocess.run(['sh','-n'],input=text,text=True,check=True);(out/(name+'.sh')).write_text(text+'\n');start=time.monotonic()
    try:return command(SERIAL,text,out/(name+'.log'),timeout,lease=lease).decode(errors='replace')
    except TimeoutError:uart_ready=False;raise
    finally:result['host_intervals'][name]=[start,time.monotonic()]
   run('preflight','''test "$(uname -r)" = 6.12.0-xilinx
grep ' / ext4 ro,' /proc/mounts
cat /proc/sys/kernel/random/boot_id
test "$(devmem 0x7c430008)" = 0x00000000
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
test -r /sys/bus/iio/devices/iio:device0/in_voltage0_rssi
test -r /sys/bus/iio/devices/iio:device0/in_voltage1_rssi''')
   try:
    setup=(v/'build/g0-air-R7-clean-r2/RX-setup.sh').read_text();run('RX-setup',setup)
    args=[str(v/'build/g0-forward/g0_uhd_probe'),'--iq',str(wave),'--freq-hz','5500000000','--gain-db','12','--tx-lo-offset-hz','1000000','--max-samples','2000000','--enable-tx'];(out/'TX-command.json').write_text(json.dumps(args,indent=2)+'\n')
    lines=queue.Queue();events=[]
    tx=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
    def read_tx():
     with (out/'TX.log').open('w') as f:
      for line in tx.stdout:
       f.write(line);f.flush();events.append({'time':time.monotonic(),'line':line.strip()});lines.put(line)
     lines.put(None)
    thread=threading.Thread(target=read_tx,daemon=True);thread.start()
    for _ in range(100):
     line=lines.get(timeout=30)
     if line is None:raise RuntimeError('TX ended before READY')
     if line.strip()=='G0_READY WAIT_GO':break
    else:raise RuntimeError('No READY')
    def sample_body():return '''echo RSSI_SAMPLE
cat /proc/uptime
cat /sys/bus/iio/devices/iio:device0/in_voltage0_rssi
cat /sys/bus/iio/devices/iio:device0/in_voltage1_rssi'''
    run('baseline',sample_body())
    result['GO_host_monotonic']=time.monotonic();tx.stdin.write('GO\n');tx.stdin.flush();result['RF_started']=True
    run('during','for i in $(seq 1 12); do\n'+sample_body()+'\nsleep 0.1\ndone')
    rc=tx.wait(timeout=20);thread.join(timeout=2);result['TX_exit']=rc;result['TX_host_events']=events
    assert rc==0 and 'TX_BURST_ACK channel_mask=3' in (out/'TX.log').read_text()
    assert 'TX completed: 2000000 dual-stream samples' in (out/'TX.log').read_text()
    result['both_TX_EOB']=True;result['observation_completed']=True
   finally:
    if tx is not None and tx.poll() is None:
     tx.stdin.close()
     try:tx.wait(timeout=5)
     except subprocess.TimeoutExpired:tx.terminate();tx.wait(timeout=5)
    if uart_ready:
     run('RX-stop','''echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown
echo 1 > /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
test "$(devmem 0x7c430008)" = 0x00000000''');result['cleanup_LOs_off']=True
 finally:
  (out/'disk-after.json').write_text(json.dumps(check('after-finite-RSSI-forward'),indent=2)+'\n');(out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps({k:val for k,val in result.items() if k!='TX_host_events'}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
