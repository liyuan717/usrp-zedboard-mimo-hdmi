#!/usr/bin/env python3
"""Dual-TX synthetic ADC stimulus through the complete SOURCE receiver RTL."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import zlib

import numpy as np

from dma_batch_stream_probe import decode_batch
from fec_deadline_proof import NAME as FEC_STREAM_SOURCE, validate_deadline_replacement
from g0_fec_session_waveform import descriptor, modulate_packets, packet, public_prefix, rrc
from g0_fixed_differential_model import quantize_adc
from g0_forward_timebase import ForwardTimebase
from g0_packet_fec import source_pattern
from g0_source_raw_diagnostic import MONITOR
from g0_source_receiver_probe import TB
from workspace_storage_guard import GIB, check

SID = 0x34AC01DF
EPOCH = 0x8011
BASE = 0x12345000
# The receiver estimates DC over 80000 samples, then discards 2048.
# Supply an idle interval before the intentionally padded warmup waveform.
LEAD = 84000
TAIL = 4096
PERIOD_SAMPLES = 80000
FRAMES = 8
WARMUP = 3
BAD_VRT_FRAME = 4
# Ask the shared DMA slot to publish VRS3 periodically. Each frame still
# carries its range declaration on air, and reserves fourteen data slots.
PROFILE_FRAMES = (2, 5)
VRT_SLOT = 1
# Public normal-training symbol 416 and TX RRC group delay 20.
TRAINING_SAMPLE = 416 * 4 + 20
MAX_COORDINATE_ERROR = 2
TRACKING_MONITOR = r"""
`define T `F.receiver.timing
always @(posedge clk) if(resetn)begin
 if(`T.arm_fire)$display("D_TIMING_ARM first=%h",`T.arm_first_tag);
 if(`T.peak_valid && `T.peak_ready)
  $display("D_TIMING_PEAK error=%0d point=%0d tag=%h",`T.peak_error,`T.peak_point,`T.peak_tag);
 if(`T.sfo_valid)
  $display("D_TIMING_SFO error=%0d locked=%0d step=%h",`T.sfo_error,`T.sfo_locked,`T.symbol_step);
end
"""


def generate(path, swap_iq_channels=False):
    cache = {}
    controls = {}
    waveform = [np.zeros((LEAD, 2), dtype=np.complex128)]
    prefix, taps = public_prefix(), rrc()
    for frame in range(FRAMES):
        def packet_at(slot):
            if frame >= WARMUP and slot > VRT_SLOT:
                wire, meta = packet(SID, frame-WARMUP, slot, cache, 2)
                struct.pack_into('<II', wire, 12, frame, frame*16+slot)
                crc = zlib.crc32(wire[:-4])
                struct.pack_into('<I', wire, 544, crc ^ int(meta['intentional_CRC_error']))
                return wire, meta
            if slot == 0:
                kind = 3
                first = max(0, frame-WARMUP)*14//32
                body = bytearray(descriptor(first))
                struct.pack_into('<H', body, 6, 2 if frame in PROFILE_FRAMES else 0)
            elif slot == VRT_SLOT:
                kind = 5
                body = ForwardTimebase(frame*PERIOD_SAMPLES, frame*20000-7, frame*20000+11).pack()
            else:
                kind, body = 5, bytes(32)
            if slot in (0, VRT_SLOT):
                controls[frame, bytes(body[:4])] = bytes(body)
            header = struct.pack('<4sBBHIIIIIHH', b'VRL3', 1, kind, 0, SID,
                                 frame, frame*16+slot, 0xffffffff, 0xffffffff, 0, 512)
            raw = header + body + bytes(480)
            damaged = frame == BAD_VRT_FRAME and slot == VRT_SLOT
            return raw + struct.pack('<I', zlib.crc32(raw) ^ int(damaged)), {'kind': kind}

        iq, _ = modulate_packets(SID, frame, packet_at, prefix, taps)
        waveform.append(iq[:, ::2] + 1j*iq[:, 1::2])
    waveform.append(np.zeros((TAIL, 2), dtype=np.complex128))
    tx = np.concatenate(waveform)
    if swap_iq_channels:
        tx = tx[:, ::-1]
    matrix = np.array([[1, .18+.12j], [.14-.10j, .84+.24j]])
    adc, _ = quantize_adc(12000 * (tx @ matrix.T) + [320-240j, -160+100j])
    packed = adc.astype('<i2').copy().view('<u8').ravel()
    np.savetxt(path, packed, fmt='%016x')
    return len(adc), controls


def verify(path, expected):
    batch = bytearray()
    batches, controls, sources = 0, {}, {}
    seq = 0
    for line in path.read_text().splitlines():
        phase, word, last = line.split()
        assert phase == '1'
        batch.extend(int(word, 16).to_bytes(8, 'little'))
        if last == '0':
            continue
        records, _ = decode_batch(bytes(batch), batches)
        batches += 1
        batch.clear()
        for record in records:
            kind, flags = struct.unpack_from('<HH', record, 12)
            assert struct.unpack_from('<Q', record, 24)[0] == seq
            seq += 1
            if kind not in (1, 3):
                assert kind == 2, ('unexpected record', kind)
                continue
            sid, delivery_epoch = struct.unpack_from('<II', record, 16)
            assert sid == SID
            block, row, length, status = struct.unpack_from('<IHHI', record, 48)
            if kind == 1:
                assert len(record) == 576 and length == 512 and block < 3 and row < 20
                assert record[64:] == source_pattern(block)[row].tobytes()
                assert (block, row) not in sources
                sources[block, row] = bool(flags & 1)
                continue
            payload = record[64:]
            magic = payload[:4]
            assert (block, magic) in expected and payload == expected[block, magic]
            assert magic != b'VRS3' or block in PROFILE_FRAMES
            assert not (block == BAD_VRT_FRAME and magic == b'VRT3')
            assert len(record) == 96 and flags == 4 and row == 65535 and length == 32
            tag, capture_time = struct.unpack_from('<QQ', record, 32)
            assert tag >> 48 == EPOCH and capture_time == 0
            assert (block, magic) not in controls
            actual_q32 = ((tag & ((1 << 48)-1)) << 32) | status
            expected_sample = BASE + LEAD + block*PERIOD_SAMPLES + TRAINING_SAMPLE
            error = (actual_q32 - (expected_sample << 32)) / (1 << 32)
            assert abs(error) <= MAX_COORDINATE_ERROR, (block, magic, error)
            controls[block, magic] = {'frame': block, 'kind': magic.decode(),
                'adc_tag': f'{tag:016x}', 'fraction_q32': status, 'delivery_epoch': delivery_epoch,
                'reference_error_samples': error}
    assert not batch
    for frame in PROFILE_FRAMES:
        assert (frame, b'VRS3') in controls, ('missing VRS3', frame)
    for frame in range(WARMUP, FRAMES):
        if frame not in (*PROFILE_FRAMES, BAD_VRT_FRAME):
            assert (frame, b'VRT3') in controls, ('missing VRT3', frame)
    for frame in PROFILE_FRAMES:
        if (frame, b'VRT3') in controls:
            profile, anchor = controls[frame, b'VRS3'], controls[frame, b'VRT3']
            for field in ('adc_tag', 'fraction_q32', 'delivery_epoch'):
                assert profile[field] == anchor[field], (frame, field)
    missing = {(block, row) for block in (0, 1) for row in range(20)} - sources.keys()
    assert not missing, ('missing SOURCE rows', sorted(missing))
    assert sum(sources.values()) >= 4
    return dict(batches=batches, records=seq, controls=list(controls.values()),
                sources=len(sources), source_bytes=512*len(sources), restored=sum(sources.values()))


def main(a):
    v3 = Path(__file__).resolve().parents[1]
    out, ref = a.output.resolve(), a.reference.resolve()
    before = check('before-forward-PHY')
    assert before['disk']['available_bytes'] >= 5*GIB
    out.mkdir(exist_ok=False)
    (out/'disk-before.json').write_text(json.dumps(before, indent=2)+'\n')
    try:
        proof = json.loads((ref/'receipt.json').read_text())
        assert proof['atomic_CPACK_tag_CSR_freshness_fence_STOP_test_passed']
        hashes = proof['source_sha256'].copy()
        assert len(hashes) == 55
        for name, digest in hashes.items():
            path = v3/('pl/g0' if name == 'g0_source_receiver.v' else 'sim/g0')/name
            if name == FEC_STREAM_SOURCE and a.fec_deadline_proof:
                replacement = validate_deadline_replacement(digest, path, a.fec_deadline_proof)
                hashes[name] = digest = replacement['compiled_sha256']
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
            shutil.copyfile(path, out/name)
        rom_hashes = {}
        for rom in ref.glob('*.hex'):
            shutil.copyfile(rom, out/rom.name)
            rom_hashes[rom.name] = hashlib.sha256(rom.read_bytes()).hexdigest()
        n, expected = generate(out/'adc.hex', a.swap_iq_channels)
        declaration = f'reg [63:0] adc[0:{n-1}]; integer k;\n' + MONITOR + TRACKING_MONITOR
        body = f'''
$readmemh("adc.hex",adc);
phase=1;csr_write(44,0,0,15);csr_write(8,1,1,15);
for(k=0;k<{n};k=k+1)begin
 pd={{16'h{EPOCH:04x},48'({BASE}+k),adc[k]}};
 pv=1;@(negedge clk);pv=0;repeat(k%2?12:11)@(negedge clk);
end
if(!dut.search_armed || dut.cal_faults || dut.cal_range)$fatal(1,"ADC frontend fault");
$display("FORWARD_PHY_ACQUISITION coarse=%0d startup=%0d/%0d headers=%0d/%0d frames=%0d link_crc=%0d/%0d",coarse_events,start_good,start_bad,header_good,header_bad,link_frames,link_good_total,link_bad_total);
repeat(100000)@(negedge clk);
csr_write(8,0,0,15);idle;
$display("FORWARD_PHY_SOURCE c=%0d dropped=%0d bad=%0d aborted=%0d crc_good=%0d crc_bad=%0d",dut.counters[0],dut.counters[1],dut.counters[2],dut.counters[3],dut.counters[4],dut.counters[5]);
if(dut.counters[1] || dut.counters[2] || dut.counters[3])$fatal(1,"SOURCE bridge loss");
'''
        (out/'tb.sv').write_text(TB.replace('@RAW_DECL@', declaration).replace('@BODY@', body))
        bp = a.vivado_bin.resolve()
        xpm = bp.parent/'data/ip/xpm'
        for label, args in (
            ('compile', [bp/'xvlog', '--sv', xpm/'xpm_cdc/hdl/xpm_cdc.sv', xpm/'xpm_memory/hdl/xpm_memory.sv', *hashes, 'tb.sv']),
            ('elaborate', [bp/'xelab', 'tb', '-s', 'forward_phy_tb', '-mt', 'off']),
            ('simulate', [bp/'xsim', 'forward_phy_tb', '-runall']),
        ):
            with (out/(label+'.log')).open('w') as log:
                subprocess.run(args, cwd=out, stdout=log, stderr=subprocess.STDOUT, check=True)
        log = (out/'simulate.log').read_text()
        assert 'G0_SOURCE_BOARD_BRIDGE_PASS' in log and 'Fatal:' not in log, log[-3000:]
        result = verify(out/'batches.txt', expected)
        result.update(completed=True, adc_samples=n, source_sha256=hashes,
                      public_ROM_sha256=rom_hashes,
                      simulation=[s for s in log.splitlines() if s.startswith(('FORWARD_PHY_', 'G0_SOURCE_'))],
                      synthetic_channel=True, logical_iq_streams_by_synthetic_RF_port=[1,0] if a.swap_iq_channels else [0,1],
                      physical_clock_or_RF_or_full_G0_passed=False)
        (out/'receipt.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result, indent=2))
    finally:
        (out/'disk-after.json').write_text(json.dumps(check('after-forward-PHY'), indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('output', 'reference', 'vivado-bin'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--fec-deadline-proof', type=Path)
    parser.add_argument('--swap-iq-channels', action='store_true', help='permute logical TX streams before synthetic MIMO mixing')
    main(parser.parse_args())
