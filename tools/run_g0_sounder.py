#!/usr/bin/env python3
"""One leased, finite dual-TX sounder and tagged raw capture. G0 only.

Linux timing here only places a diagnostic capture inside a finite burst.
It is NOT the production TDD slot scheduler and supplies no TDD timing pass.
"""
import argparse
import json
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time
import zlib
from board_command import command,require_boot_id
from hardware_lease import HardwareLease

SERIAL='/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00'
CLEANUP='''phy=/sys/bus/iio/devices/iio:device0
echo 1 > "$phy/out_altvoltage0_RX_LO_powerdown"
echo 1 > "$phy/out_altvoltage1_TX_LO_powerdown"
cat "$phy/out_altvoltage0_RX_LO_powerdown"
cat "$phy/out_altvoltage1_TX_LO_powerdown"
cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state
'''


def run(a):
    if not 0<=a.gain_db<=18 or not 0<=a.rx_gain_db<=70:raise ValueError('gain outside development bounds')
    if not 200000<=a.rx_bandwidth_hz<=18000000:raise ValueError('RX bandwidth outside measured front-end configuration range')
    if abs(a.tx_lo_offset_hz)>1000000:raise ValueError('G0 LO-offset probe limited to +/-1MHz; signal target stays5.5GHz')
    project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    waveform=json.loads((a.waveform/'waveform.json').read_text())
    if waveform['sample_rate_hz']!=4_000_000 or waveform['duration_seconds']>.99:
        raise ValueError('finite 4MSPS probe required')
    records=a.records
    if not 1<=records<=2097152:raise ValueError('finite capture limited to 32MiB')
    if waveform['duration_seconds'] < records/4000000+.12:
        raise ValueError('finite waveform too short for requested capture and placement margin')
    expected_bytes=records*16;result={'scope':'G0 finite forward channel measurement, not TDD/production PHY',
        'center_frequency_hz':5500000000,'TX_gain_db':a.gain_db,'RX_gain_db':a.rx_gain_db,'RX_bandwidth_hz':a.rx_bandwidth_hz,
        'TX_LO_offset_hz':a.tx_lo_offset_hz,'desired_RF_frequency_hz':5500000000,
        'TX_antenna':'TX/RX channels0,1; RF0 vertical/RF1 horizontal','RX_antenna':'RX1A vertical/RX2A horizontal',
        'FMCOMMS3_TX_enabled':False,'feedback_measurement':False,'records_requested':records,
        'TX_samples_requested':waveform['total_samples'],'TX_duration_seconds':waveform['duration_seconds'],
        'g0_passed':False,'completed':False}
    tx=None;receiver=None;receiver_error=[];capture={};rx_setup=False;uart_timeout=False
    with HardwareLease() as lease:
        try:
            if a.expected_boot_id:
                identity=command(SERIAL,'cat /proc/sys/kernel/random/boot_id\n'
                    'grep " / ext4 ro," /proc/mounts\n'
                    'test -r /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state',
                    out/'boot-identity.log',12,lease=lease).decode(errors='replace')
                result['boot_id']=require_boot_id(identity,a.expected_boot_id)
            setup=(project/'board/g0_forward_rx_setup.sh').read_text().replace('echo 40 >',f'echo {a.rx_gain_db:g} >')
            setup=setup.replace('echo 2000000 >',f'echo {a.rx_bandwidth_hz} >')
            (out/'rx-setup.sh').write_text(setup)
            rx_setup=True
            try:command(SERIAL,setup,out/'rx-setup.log',20,lease=lease)
            except TimeoutError:uart_timeout=True;raise
            # Validate the live allocation before arming a burst. The legacy
            # 8MiB module remains supported for default-size probes.
            check='cat /sys/bus/platform/devices/7c430000.rf-receiver-v2/capture_state\n'
            state=command(SERIAL,check,out/'preflight.log',10,lease=lease)
            import re
            sizes=re.findall(rb'buffer_bytes=(\d+)',state)
            available=int(sizes[-1]) if sizes else 8*1024*1024
            if expected_bytes>available:raise ValueError('requested capture exceeds live driver allocation')
            with socket.socket() as server:
                server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                server.bind(('169.254.209.83',8014));server.listen(1);server.settimeout(20)
                def receive():
                    try:
                        connection,peer=server.accept()
                        with connection:
                            if peer[0]!='169.254.209.84':raise ValueError('unexpected sender')
                            connection.settimeout(10);data=bytearray()
                            while len(data)<=expected_bytes:
                                chunk=connection.recv(min(65536,expected_bytes+1-len(data)))
                                if not chunk:break
                                data.extend(chunk)
                            if len(data)!=expected_bytes:raise ValueError('capture byte count differs')
                        (out/'adc.raw').write_bytes(data)
                        capture.update({'bytes':len(data),'crc32':f'{zlib.crc32(data):08x}'})
                    except BaseException as e:receiver_error.append(str(e))
                receiver=threading.Thread(target=receive);receiver.start()
                args=[str(a.endpoint.resolve()),'--iq',str((a.waveform/'iq.fc32').resolve()),'--freq-hz','5500000000',
                    '--gain-db',str(a.gain_db),'--tx-lo-offset-hz',str(a.tx_lo_offset_hz),
                    '--max-samples',str(waveform['total_samples']),'--enable-tx']
                tx=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
                lines=queue.Queue()
                def read_tx():
                    with (out/'tx.log').open('w') as log:
                        for line in tx.stdout:log.write(line);log.flush();lines.put(line)
                monitor=threading.Thread(target=read_tx);monitor.start()
                def wait_marker(marker,timeout):
                    end=time.monotonic()+timeout
                    while time.monotonic()<end:
                        line=lines.get(timeout=max(.01,end-time.monotonic()))
                        if marker in line:return line.strip()
                    raise TimeoutError('TX marker absent '+marker)
                wait_marker('G0_READY WAIT_GO',25)
                tx.stdin.write('GO\n');tx.stdin.flush()
                result['TX_arm_marker']=wait_marker('G0_ARMED',3)
                time.sleep(.27)
                script=f'''test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
owner=/sys/bus/platform/devices/7c430000.rf-receiver-v2
echo {records} > "$owner/capture_records"
cat "$owner/capture_state"
cat "$owner/capture_data" > /tmp/g0-forward.raw
busybox crc32 /tmp/g0-forward.raw
python3 -c "import socket,pathlib; s=socket.create_connection(('169.254.209.83',8014),5); s.sendall(pathlib.Path('/tmp/g0-forward.raw').read_bytes()); s.shutdown(socket.SHUT_WR); s.close()"
cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown
'''
                (out/'capture.sh').write_text(script)
                try:raw=command(SERIAL,script,out/'capture.log',20,lease=lease)
                except TimeoutError:uart_timeout=True;raise
                receiver.join(15)
                if receiver.is_alive() or receiver_error:raise RuntimeError('capture receiver failed '+str(receiver_error))
                if capture['crc32'].encode()+b' /tmp/g0-forward.raw' not in raw:raise ValueError('board/host capture CRC differs')
                if b'overflow=0 bad_keep=0 fences=0 status=0x1' not in raw:raise ValueError('capture status requires review')
                ret=tx.wait(timeout=5);monitor.join(2)
                txlog=(out/'tx.log').read_text()
                if ret or 'TX_BURST_ACK channel_mask=3' not in txlog:raise RuntimeError('TX burst failed or missing EOB acknowledgement')
                result.update({'completed':True,'capture':capture,'EOB_acknowledged_channels':[0,1]})
        finally:
            if tx is not None and tx.poll() is None:
                # A finite stream owns at most the declared sample count.
                # Give the existing process its completion deadline before
                # stopping an unresponsive diagnostic endpoint.
                try:tx.wait(timeout=5)
                except subprocess.TimeoutExpired:tx.terminate();tx.wait(timeout=3)
            if rx_setup and not uart_timeout:
                command(SERIAL,CLEANUP,out/'rx-stop.log',15,lease=lease)
            result['UART_timeout_requires_inspection']=uart_timeout
            (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--endpoint',type=Path,required=True)
    p.add_argument('--waveform',type=Path,required=True)
    p.add_argument('--gain-db',type=float,default=0)
    p.add_argument('--rx-gain-db',type=float,default=40)
    p.add_argument('--rx-bandwidth-hz',type=int,default=2000000)
    p.add_argument('--tx-lo-offset-hz',type=int,default=0)
    p.add_argument('--records',type=int,default=524288)
    p.add_argument('--expected-boot-id',help='reject a different diagnostic RAM boot before RF')
    p.add_argument('--output',type=Path,required=True)
    run(p.parse_args())
