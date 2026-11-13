#!/usr/bin/env python3
"""Bound PC/PS monotonic time over the dedicated Ethernet link for G0 calibration."""
import argparse
from fractions import Fraction
import json
from math import ceil, floor
from pathlib import Path
import re
import secrets
import socket
import struct
import time

from board_command import command
from g0_forward_timebase import (TxClockBracket, TX_PERIOD_SAMPLES, TX_TRAINING_SAMPLE,
                                 training_host_interval)
from hardware_lease import HardwareLease
from run_g0_source_air import BOARD, HOST, SERIAL

PACKET = struct.Struct('!QQQ')
SCALE = 1_000_000
RATE_PPM = 400
PATH_CALIBRATION_MODEL = 'bounded-rate-TX-endpoint-intersection-v1'

# Exactly one reply for each accepted request. This finite helper is written
# only to board tmpfs. It neither opens radio devices nor shares the UART.
SERVER = r"""
from pathlib import Path
import socket,struct,sys,time
host,board,count,token,seconds=sys.argv[1:]
count=int(count)
token=bytes.fromhex(token)
stop=Path(__file__).with_name('stop')
request=struct.Struct('!16sQ')
reply=struct.Struct('!QQQ')
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
 sock.bind((board,0))
 print('CLOCK_PORT='+str(sock.getsockname()[1]),flush=True)
 end=time.monotonic()+int(seconds)
 remaining=count
 while remaining and time.monotonic()<end and not stop.exists():
  sock.settimeout(min(.25,max(.001,end-time.monotonic())))
  try:data,peer=sock.recvfrom(128)
  except socket.timeout:continue
  before=time.monotonic_ns()
  if peer[0]!=host or len(data)!=request.size:continue
  key,serial=request.unpack(data)
  if key!=token or serial>=count:continue
  after=time.monotonic_ns()
  sock.sendto(reply.pack(serial,before,after),peer)
  remaining-=1
print('CLOCK_DONE',flush=True)
"""


def host_interval(board_ns, rows, ppm=RATE_PPM):
    """Intersect causal inequalities using a finite relative rate bound."""
    if not rows or not 0 <= ppm < SCALE or board_ns <= 0:
        raise ValueError('clock observations and relative rate bound required')
    lower = upper = None
    for sent, received_board, sent_board, received in rows:
        if not (0 < sent <= received and 0 < received_board <= sent_board):
            raise ValueError('invalid timestamp ordering')
        before = board_ns - received_board
        after = board_ns - sent_board
        candidate_low = sent + before - (abs(before)*ppm + SCALE-1)//SCALE
        candidate_high = received + after + (abs(after)*ppm + SCALE-1)//SCALE
        lower = candidate_low if lower is None else max(lower, candidate_low)
        upper = candidate_high if upper is None else min(upper, candidate_high)
    if lower > upper:
        raise ValueError('observations contradict the relative rate bound')
    return lower, upper


def affine_rate_bounds(rows):
    """Conditional host ns per board ns under a positive affine clock."""
    lower, upper = Fraction(0), None
    for i, (send_i, rx_i, tx_i, recv_i) in enumerate(rows):
        if not (0 < send_i <= recv_i and 0 < rx_i <= tx_i):
            raise ValueError('invalid timestamp ordering')
        for send_j, rx_j, tx_j, recv_j in rows[i+1:]:
            if rx_j <= tx_i or send_j <= recv_i:
                continue
            lower = max(lower,Fraction(send_j-recv_i,rx_j-tx_i))
            bound = Fraction(recv_j-send_i,tx_j-rx_i)
            upper = bound if upper is None else min(upper,bound)
    if upper is None or lower <= 0 or lower > upper:
        raise ValueError('no ordered observations support an affine rate interval')
    return lower,upper


def path_delay_bounds(rows, anchors, tx_begin, tx_end, expected_sid, path_budget_ns):
    """Compare bounded RF/CPACK observations with independent wired timing."""
    if not isinstance(tx_begin,TxClockBracket) or not 0 < path_budget_ns <= 10_000_000:
        raise ValueError('valid timed TX bracket and explicit path budget required')
    if not 2 <= len(anchors) <= 9:
        raise ValueError('need at least two separated physical anchor observations')
    rate=tx_begin.rate_after_burst(*tx_end)
    if not rate['observed_rate_within_projection']:
        raise ValueError('device endpoint observations do not verify TX rate allowance')
    low_rate,high_rate=affine_rate_bounds(rows)
    if not 1-Fraction(RATE_PPM,SCALE)<=low_rate<=high_rate<=1+Fraction(RATE_PPM,SCALE):
        raise ValueError('wired endpoint observations do not verify PC/PS rate allowance')
    before,last_after,last_ticks=tx_end
    tx_observations=((tx_begin.before_ns,tx_begin.after_ns,tx_begin.observed_ticks),tx_end)
    first=min(r[1] for r in rows);last=max(r[2] for r in rows)
    samples=[]
    for anchor in anchors:
        frame=anchor['superframe']
        lo,hi=anchor['board_rx_lower_ns'],anchor['board_rx_upper_ns']
        if (anchor['sid']!=expected_sid or not 0<=frame<3200 or
                not first<=lo<=hi<=last or (samples and frame<=samples[-1]['superframe'])):
            raise ValueError('anchor identity or independent timing coverage')
        tx_ticks=tx_begin.start_ticks+frame*TX_PERIOD_SAMPLES+TX_TRAINING_SAMPLE
        part=Fraction(tx_ticks-tx_begin.observed_ticks,last_ticks-tx_begin.observed_ticks)
        if not 0<=part<=1:
            raise ValueError('TX anchor outside device time brackets')
        affine_tx_low=floor((1-part)*tx_begin.before_ns+part*before)-251
        affine_tx_high=ceil((1-part)*tx_begin.after_ns+part*last_after)+251
        tx_low,tx_high=training_host_interval(tx_ticks,tx_observations)
        receive_low,_=host_interval(lo,rows)
        _,receive_high=host_interval(hi,rows)
        samples.append(dict(superframe=frame,lower_ns=receive_low-tx_high,upper_ns=receive_high-tx_low,
                            affine_lower_ns=receive_low-affine_tx_high,affine_upper_ns=receive_high-affine_tx_low))
    return dict(model=PATH_CALIBRATION_MODEL,anchors=samples,maximum_observed_upper_ns=max(r['upper_ns'] for r in samples),
                all_sampled_paths_consistent_with_budget=all(0<=r['upper_ns']<=path_budget_ns for r in samples),
                candidate_path_budget_ns=path_budget_ns,continuous_or_standalone_path_bound_passed=False)


def read_samples(port, token, count, interval_ms, stopping=None,raw=False):
    rows = []
    clock=(lambda:time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)) if raw else time.monotonic_ns
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((HOST, 0))
        sock.connect((BOARD, port))
        for serial in range(count):
            if stopping is not None and stopping.is_set():
                break
            packet = struct.pack('!16sQ', token, serial)
            before = clock()
            sock.send(packet)
            until = time.monotonic() + .15
            while (left := until-time.monotonic()) > 0:
                sock.settimeout(left)
                try:
                    data = sock.recv(128)
                except socket.timeout:
                    break
                after = clock()
                if len(data) == PACKET.size:
                    tag, board_before, board_after = PACKET.unpack(data)
                    if tag == serial:
                        rows.append((before, board_before, board_after, after))
                        break
            if stopping is None:
                time.sleep(interval_ms/1000)
            elif stopping.wait(interval_ms/1000):
                break
    return rows


def server_script(remote, token, count, seconds):
    if not re.fullmatch(r'/tmp/[A-Za-z0-9_-]{1,64}(?:/wired-clock)?',remote):
        raise ValueError('invalid RAM clock helper path')
    if not 8<=count<=440 or not 1<=seconds<=110:
        raise ValueError('finite clock helper limits')
    return f'''mkdir {remote}
cat > {remote}/probe.py <<'V3CLOCKPY'
{SERVER}
V3CLOCKPY
python3 -u {remote}/probe.py {HOST} {BOARD} {count} {token.hex()} {seconds} > {remote}/log 2>&1 &
for i in $(seq 1 30); do grep -q '^CLOCK_PORT=' {remote}/log && break; sleep 0.05; done
grep '^CLOCK_PORT=' {remote}/log'''


def stop_script(remote):
    return f'''touch {remote}/stop
for i in $(seq 1 30); do grep -q '^CLOCK_DONE$' {remote}/log && break; sleep 0.05; done
grep '^CLOCK_DONE$' {remote}/log
rm {remote}/probe.py {remote}/log {remote}/stop
rmdir {remote}'''


def port_from_log(ready):
    port,=re.findall(r'(?m)^CLOCK_PORT=(\d+)\r?$',ready)
    if not 1<=int(port)<=65535:
        raise ValueError('invalid clock server port')
    return int(port)


def main(a):
    out = a.output.resolve()
    out.mkdir(exist_ok=False)
    if not 8 <= a.samples <= 440 or not 5 <= a.interval_ms <= 250:
        raise ValueError('bounded 8..440 requests at 5..250ms intervals required')
    window = max(12, ceil(a.samples*a.interval_ms/1000)+4)
    if window > 110:
        raise ValueError('standalone sample plan exceeds the 110-second RAM helper window')
    token = secrets.token_bytes(16)
    remote = '/tmp/g0-wired-clock-' + secrets.token_hex(6)
    boot_id = json.loads(a.boot_receipt.read_text())['boot_id']
    if not re.fullmatch(r'[0-9a-f-]{36}', boot_id):
        raise ValueError('invalid verified RAM boot identity')
    start = f'''test "$(cat /proc/sys/kernel/random/boot_id)" = {boot_id}
grep ' / ext4 ro,' /proc/mounts
test "$(devmem 0x7c430008)" = 0x00000000
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage0_RX_LO_powerdown)" = 1
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
'''+server_script(remote,token,a.samples,window)
    result = dict(completed=False,boot_id=boot_id,RF_started=False,SD_written=False,
                  relative_rate_allowance_ppm=RATE_PPM,physical_RF_path_calibrated=False)
    try:
        with HardwareLease() as lease:
            ready = command(SERIAL,start,out/'start.log',7,lease=lease)
            try:
                rows = read_samples(port_from_log(ready.decode()),token,a.samples,a.interval_ms)
            finally:
                stop = stop_script(remote)+'''
test "$(devmem 0x7c430008)" = 0x00000000
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1'''
                command(SERIAL,stop,out/'stop.log',16,lease=lease)
        if len(rows) < a.samples*9//10:
            raise RuntimeError('insufficient independent wired clock replies')
        widths = []
        for _,board_rx,board_tx,_ in rows:
            lo,hi = host_interval((board_rx+board_tx)//2,rows)
            widths.append(hi-lo)
        lower,upper = affine_rate_bounds(rows)
        result.update(completed=True,requests=a.samples,replies=len(rows),rows=rows,
                      sample_interval_ms=a.interval_ms,server_window_seconds=window,
                      maximum_board_sample_gap_ns=max(following[1]-previous[2]
                                                      for previous,following in zip(rows,rows[1:])),
                      board_observation_span_ns=rows[-1][2]-rows[0][1],
                      observed_affine_rate_ppm=[float((x-1)*SCALE) for x in (lower,upper)],
                      observed_affine_rate_within_allowance=1-Fraction(RATE_PPM,SCALE)<=lower<=upper<=1+Fraction(RATE_PPM,SCALE),
                      max_intersected_width_ns=max(widths),min_intersected_width_ns=min(widths),
                      worst_round_trip_ns=max(d-a for a,_,_,d in rows))
    finally:
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--boot-receipt',type=Path,required=True)
    parser.add_argument('--samples',type=int,default=48)
    parser.add_argument('--interval-ms',type=int,default=10)
    main(parser.parse_args())
