"""Bind finite RF evidence to the recorded antenna setup and link configuration."""
import argparse
import hashlib
import json
import math
from pathlib import Path


def read_rf_setup(path, tx_binary, rx_commands, iq_mapping):
    if path is None:
        return None, None
    with path.open('rb') as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError('RF setup record exceeds 4096 bytes')
    setup = json.loads(raw)
    if not isinstance(setup, dict) or setup.get('schema') != 'G0-rf-setup-v1':
        raise ValueError('RF setup record requires G0-rf-setup-v1')
    for field in ('setup_id', 'distance_measurement', 'geometry'):
        if not isinstance(setup.get(field), str) or not setup[field].strip():
            raise ValueError('RF setup record requires '+field)
    distance = setup.get('antenna_separation_m')
    if type(distance) not in (int, float) or not 0 < distance < math.inf:
        raise ValueError('RF setup requires a positive measured antenna separation in metres')
    return raw, {
        'description': setup,
        'description_sha256': hashlib.sha256(raw).hexdigest(),
        'TX_binary_sha256': hashlib.sha256(tx_binary.read_bytes()).hexdigest(),
        'RX_commands_sha256': hashlib.sha256(rx_commands.encode()).hexdigest(),
        'logical_IQ_by_RF_port': list(iq_mapping),
    }


def match_rf_evidence(video, capacity):
    """Check that both receipts describe the same boot and forward RF setup."""
    fields = ('RF_setup', 'boot_id', 'bit_crc32', 'fixed_FEC_banks', 'frequency_hz',
              'sample_rate_hz', 'TX_gain_db', 'TX_LO_offset_hz', 'RX_gain_db', 'RX_bandwidth_hz')
    for field in fields:
        if video.get(field) is None or video[field] != capacity.get(field):
            raise ValueError('video and capacity RF receipts differ or lack '+field)
    if not video['RF_setup']:
        raise ValueError('video and capacity receipts require a recorded antenna setup')
    return {field: video[field] for field in fields}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Compare the RF identity of two existing receipts offline.')
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--capacity', type=Path, required=True)
    args = parser.parse_args()
    pair = match_rf_evidence(json.loads(args.video.read_text()), json.loads(args.capacity.read_text()))
    print(json.dumps(pair, indent=2))
