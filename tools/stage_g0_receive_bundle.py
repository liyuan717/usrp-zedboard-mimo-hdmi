#!/usr/bin/env python3
"""Stage the candidate V3 receive service and matched ARM artifacts locally."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from build_g0_stream_decode import RUNTIME
from prepare_g0_hdmi_native import NAMES as HDMI_MODULES
from workspace_storage_guard import GIB, check

EXECUTABLES = {'g0_receive_service.sh', 'g0-stream-decode', 'runtime/ld-linux-armhf.so.3'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_receipt(directory, project):
    receipt = json.loads((directory / 'receipt.json').read_text())
    for name, expected in receipt['source_sha256'].items():
        if digest(project / name) != expected:
            raise ValueError('Source differs from build receipt: ' + name)
    return receipt


def stage(args):
    project = Path(__file__).resolve().parents[1]
    decoder = checked_receipt(args.decoder, project)
    driver = checked_receipt(args.driver, project)
    hdmi = json.loads(args.hdmi_proof.read_text())
    if not decoder['ARM_compiled'] or not driver['module_layout_checked'] or not driver['source_batch_profile']:
        raise ValueError('Matched ARM decoder and SOURCE driver required')
    if not hdmi['completed'] or hdmi['forced_module_load'] or hdmi['kernel_crc32'] != '13f60426':
        raise ValueError('Matched HDMI module proof required')

    decode = args.decoder / 'g0-stream-decode'
    if b'source-dma-live\0' not in decode.read_bytes():
        raise ValueError('Decoder lacks live SOURCE entry')
    files = {
        name: project / 'board' / name
        for name in ('g0_receive_service.sh', 'g0_forward_video_rx_setup.sh')
    }
    files['g0-stream-decode'] = decode
    files['modules/g0_source_ring.ko'] = args.driver / 'g0_source_ring.ko'
    for name in RUNTIME:
        files['runtime/' + name] = args.decoder / 'runtime' / name
    for name in HDMI_MODULES:
        filename = name + '.ko'
        info = hdmi['modules'][filename]
        source = project / info['source']
        if digest(source) != info['sha256']:
            raise ValueError('HDMI module differs from native proof: ' + filename)
        files['modules/' + filename] = source

    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    before = check('before-stage-receive-bundle')
    (out / 'disk-before.json').write_text(json.dumps(before, indent=2) + '\n')
    try:
        if before['disk']['available_bytes'] < 5 * GIB:
            raise RuntimeError('Disk space below bundle staging threshold')
        inventory = {}
        for name, source in files.items():
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            mode = 0o755 if name in EXECUTABLES else 0o644
            target.chmod(mode)
            inventory[name] = {
                'bytes': target.stat().st_size, 'sha256': digest(target), 'mode': f'{mode:04o}',
            }
        manifest = {
            'scope': __doc__,
            'entry_point': './g0_receive_service.sh STATE_DIRECTORY',
            'kernel_release': '6.12.0-xilinx',
            'kernel_crc32': hdmi['kernel_crc32'],
            'files': inventory,
            'SD_written': False,
            'cold_boot_passed': False,
        }
        (out / 'RECEIVE_BUNDLE.json').write_text(json.dumps(manifest, indent=2) + '\n')
    finally:
        (out / 'disk-after.json').write_text(json.dumps(check('after-stage-receive-bundle'), indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('decoder', 'driver', 'hdmi-proof', 'output'):
        parser.add_argument('--' + argument, required=True, type=Path)
    stage(parser.parse_args())
