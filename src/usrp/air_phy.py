"""Block-oriented MRF1 transmitter, pinned to the published 8021db6c contract."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'protocol'))
import reference_mrf1 as reference
import reference_rvf2 as rvf2

COMMIT = '8021db6c59a77e65bebbedabca961ac82c90ab70'
PROFILE = 'MRF1-QPSK-2X2-4M-v1'
FS = 4_000_000
FRAME_SAMPLES = 3840
POINTS = np.asarray(reference.POINTS, dtype=np.complex128)
TAPS = np.asarray(reference.rrc_taps(), dtype=np.float64)


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify_contract():
    lock = json.loads((HERE / 'protocol-lock.json').read_text())
    if lock['commit'] != COMMIT:
        raise ValueError('protocol commit lock mismatch')
    for name, expected in lock['sha256'].items():
        if sha_file(HERE / 'protocol' / name) != expected:
            raise ValueError('pinned protocol file changed: ' + name)
    c = json.loads((HERE / 'protocol/air_interface.json').read_text())
    expected = dict(profile_id=PROFILE, sample_rate_hz=FS, symbol_rate_hz=1_000_000,
                    samples_per_symbol=4, frame_symbols=950, frame_scans=3840,
                    iq_peak_component=.125, payload_bytes_per_stream=128,
                    rrc_rolloff=.35, rrc_taps=41, modulation_id=1)
    for k, v in expected.items():
        if c[k] != v:
            raise ValueError('unsupported frozen PHY field: ' + k)
    return c


@lru_cache(maxsize=1)
def template():
    lanes, _, sections = reference.frame_symbols((bytes(128), bytes(128)), 0)
    return np.asarray(lanes, dtype=np.complex128), sections


def build_frames(rows):
    """Return sample-major LE fc32 [samples, 2]; no inter-frame padding.

    Only packet-bearing symbols are replaced. Each frame retains its own full
    RRC convolution, and both lanes share one normalization factor per frame.
    """
    if not 1 <= len(rows) <= 16:
        raise ValueError('waveform batch must contain 1..16 RF frames')
    base, sections = template()
    symbols = np.broadcast_to(base, (len(rows), 2, 950)).copy()
    packets = []
    for frame_id, payloads in rows:
        packets.append([reference.packet(payloads[s], frame_id, s) for s in (0, 1)])
    raw = np.frombuffer(b''.join(p for pair in packets for p in pair), dtype=np.uint8)
    raw = raw.reshape(len(rows), 2, 152)
    ids = ((raw[..., None] >> np.array([6, 4, 2, 0], dtype=np.uint8)) & 3)
    modulated = POINTS[ids.reshape(len(rows), 2, 608)]
    cursor = 80
    for section in sections:
        name, start, n = section['name'], section['start_symbol'], section['symbol_count']
        if name == 'headers':
            symbols[:, :, start:start+n] = modulated[:, :, :80]
        elif name.startswith('payload_'):
            symbols[:, :, start:start+n] = modulated[:, :, cursor:cursor+n]
            cursor += n
    assert cursor == 608
    shaped = np.empty((len(rows), 2, FRAME_SAMPLES), dtype=np.complex128)
    for i in range(len(rows)):
        for s in (0, 1):
            up = np.zeros(3800, dtype=np.complex128)
            up[::4] = symbols[i, s]
            shaped[i, s] = np.convolve(up, TAPS, mode='full')
    peak = np.maximum(abs(shaped.real).max(axis=(1, 2)), abs(shaped.imag).max(axis=(1, 2)))
    shaped *= (.125 / peak)[:, None, None]
    samples = np.ascontiguousarray(shaped.transpose(0, 2, 1).reshape(-1, 2), dtype='<c8')
    return samples, packets


def deterministic_payloads(frame_id, mode='deterministic'):
    if mode == 'ascending':
        return bytes(range(128)), bytes(range(255, 127, -1))
    if mode == 'boundary':
        return bytes([0, 255, 85, 170]) * 32, bytes([128, 127, 1, 254]) * 32
    return tuple(reference.deterministic_bytes(b'MRF1-PAYLOAD', seed, frame_id, s, 128)
                 for s, seed in enumerate((717, 1717)))


def vector_check():
    manifest = json.loads((HERE / 'protocol/vectors-manifest.json').read_text())
    results = []
    for row in manifest['vectors']:
        payloads = tuple(bytes.fromhex(x) for x in row['payload_hex'])
        canonical = reference.frame(payloads, row['frame_id'])
        for item in row['artifacts']:
            name = item['path']
            value = (canonical['packets'][0 if '.stream0.' in name else 1]
                     if name.endswith('.bin') else canonical[name.rsplit('.', 1)[-1]])
            if len(value) != item['bytes'] or hashlib.sha256(value).hexdigest() != item['sha256']:
                raise AssertionError('published vector regeneration failed: ' + name)
        samples, packets = build_frames([(row['frame_id'], payloads)])
        expected = np.frombuffer(canonical['fc32le'], dtype='<c8').reshape(-1, 2)
        error = float(np.maximum(abs((samples-expected).real), abs((samples-expected).imag)).max())
        assert packets[0] == canonical['packets'] and error <= 1e-6
        components = samples.view('<f4').reshape(-1, 4)
        quantized = np.rint(components.astype(np.float64) * 32767).astype('<i2').tobytes()
        # Quantize full-precision canonical values for the exact sc16 recipe;
        # independently generated fc32 can round a tie differently after casting.
        results.append({'name': row['name'], 'packet_bytes_exact': True,
                        'canonical_manifest_hashes_pass': True, 'fc32_max_component_error': error,
                        'fc32_quantized_sc16_matches': quantized == canonical['sc16le']})
    return results
