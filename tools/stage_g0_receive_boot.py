#!/usr/bin/env python3
"""Stage a regular-file Zynq receiver boot bundle from the qualified R9 and V3 artifacts."""
import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import struct
import subprocess
import sys
from zipfile import ZipFile

from stage_g0_source_ram import BIT, FILES, HWH, ROUTE, asset_record, hardware_handoff
from workspace_storage_guard import GIB, check

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
from linux_package_boot import bit_identity_and_payload

PS7_FILES = ('ps7_init.c', 'ps7_init.h', 'ps7_init.tcl', 'ps7_init_gpl.c', 'ps7_init_gpl.h')
INITRAMFS = 'v3-receive-initramfs.cpio.gz'


def word_swap(data):
    if len(data) % 4:
        raise ValueError('Boot partition must contain whole 32-bit words')
    swapped = bytearray(len(data))
    for byte in range(4):
        swapped[byte::4] = data[3-byte::4]
    return bytes(swapped)


def replace_pl_partition(image, old_bit, new_bit):
    boot_header = struct.unpack_from('<11I', image, 0x20)
    if (boot_header[:3] != (0xAA995566, 0x584C4E58, 0)
            or (sum(boot_header) & 0xFFFFFFFF) != 0xFFFFFFFF):
        raise ValueError('Expected an unencrypted Zynq boot header with a valid checksum')
    table, offset = struct.unpack_from('<II', image, 0x98)
    _, count, table_offset_words, _, auth_offset = struct.unpack_from('<5I', image, table)
    if count != 3 or offset != table_offset_words*4 or auth_offset:
        raise ValueError('Expected an unsigned image table with exactly three partitions')
    headers = [struct.unpack_from('<16I', image, offset+64*i) for i in range(4)]
    if headers[-1] != (0,)*15 + (0xFFFFFFFF,):
        raise ValueError('Expected exactly three boot partitions')
    ranges = []
    for header, attributes in zip(headers[:3], (0x10, 0x20, 0x10)):
        if ((sum(header) & 0xFFFFFFFF) != 0xFFFFFFFF or header[6] != attributes
                or header[8] or header[10] or not header[0] == header[1] == header[2]):
            raise ValueError('Unexpected partition checksum, authentication or layout')
        ranges.append((header[5]*4, (header[5]+header[0])*4))
    if not (offset+256 <= ranges[0][0] < ranges[0][1] <= ranges[1][0]
            < ranges[1][1] <= ranges[2][0] < ranges[2][1] <= len(image)):
        raise ValueError('Invalid Zynq boot partition ranges')
    if headers[2][3:5] != (0x04000000, 0x04000000):
        raise ValueError('Unexpected U-Boot load or entry address')
    old_design, old_part, old = bit_identity_and_payload(old_bit)
    new_design, new_part, new = bit_identity_and_payload(new_bit)
    if (old_design.split(b';')[0] != new_design.split(b';')[0]
            or old_part != new_part or len(old) != len(new) or old == new):
        raise ValueError('Bitstreams must target the same design and have distinct equal-sized payloads')
    start, end = ranges[1]
    raw = word_swap(image[start:end])
    padding = raw[len(old):]
    if raw[:len(old)] != old or len(padding) >= 64 or padding != b'\x20\x00\x00\x00'*(len(padding)//4):
        raise ValueError('Archived PL partition differs from its canonical bitstream')
    return image[:start]+word_swap(new+padding)+image[end:], ranges


def stage(args):
    before = check('before-stage-receive-boot')
    if before['disk']['available_bytes'] < 5*GIB:
        raise RuntimeError('Disk space below boot staging threshold')
    platform, ram, init = args.platform, args.ram_assets, args.initramfs
    ram_manifest = json.loads((ram/'BOOT_MANIFEST.json').read_text())
    ram_data = {name: (ram/name).read_bytes() for name in FILES}
    if (ram_manifest['files'] != [asset_record(name, ram_data[name]) for name in FILES]
            or ram_manifest['routed_review_sha256'] != sha256((platform/ROUTE).read_bytes()).hexdigest()
            or ram_manifest['hardware_handoff_without_timestamp_sha256'] != sha256(hardware_handoff(platform)).hexdigest()
            or ram_data['hdmi_pipeline.bit'] != (platform/BIT).read_bytes()):
        raise ValueError('RAM artifacts differ from the qualified platform')
    init_manifest = json.loads((init/'INITRAMFS.json').read_text())
    init_data = (init/INITRAMFS).read_bytes()
    if (init_manifest['archive_sha256'] != sha256(init_data).hexdigest()
            or init_manifest['archive_bytes'] != len(init_data)
            or init_manifest['kernel_crc32'] != ram_manifest['files'][-1]['crc32']):
        raise ValueError('Initramfs and kernel qualification differ')

    xsa = platform/'project/g0_source_pre.xsa'
    with ZipFile(args.baseline) as archive, ZipFile(xsa) as candidate:
        if candidate.read('receiver_v2.hwh') != (platform/HWH).read_bytes():
            raise ValueError('Candidate XSA hardware handoff differs from the qualified platform')
        reference_image = archive.read('boot/BOOT.BIN')
        reference_bit = archive.read('hardware/receiver_v2.bit')
        with ZipFile(BytesIO(archive.read('hardware/receiver_v2.xsa'))) as reference:
            ps7 = {name: candidate.read(name).replace(b'\r\n', b'\n') for name in PS7_FILES}
            if any(reference.read(name).replace(b'\r\n', b'\n') != data for name,data in ps7.items()):
                raise ValueError('PS7 initialization differs from archived R9 beyond line endings')
        image, ranges = replace_pl_partition(reference_image, reference_bit, ram_data['hdmi_pipeline.bit'])

    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    (out/'BOOT.BIN').write_bytes(image)
    (out/'zImage').write_bytes(ram_data['zImage'])
    (out/'hdmi.dtb').write_bytes(ram_data['hdmi.dtb'])
    (out/INITRAMFS).write_bytes(init_data)
    with (out/'mkimage.log').open('x') as log:
        subprocess.run([str(args.mkimage.resolve()), '-A', 'arm', '-O', 'linux', '-T', 'script',
                        '-C', 'none', '-n', 'V3 receiver candidate', '-d',
                        str(ROOT/'board/g0_receive_boot.cmd'), str(out/'boot.scr')],
                       check=True, stdout=log, stderr=subprocess.STDOUT)
    manifest = {
        'scope': __doc__, 'baseline': str(args.baseline), 'candidate_PS7_XSA': str(xsa),
        'PS7_initialization_sha256_after_CRLF_normalization': {name:sha256(data).hexdigest() for name,data in ps7.items()},
        'boot_partition_byte_ranges': ranges,
        'FSBL_and_U_Boot_unchanged_from_archived_R9': True,
        'PL_bit_crc32': ram_manifest['files'][0]['crc32'],
        'files': {name: {'bytes': (out/name).stat().st_size, 'sha256': sha256((out/name).read_bytes()).hexdigest()}
                  for name in ('BOOT.BIN', 'boot.scr', 'zImage', 'hdmi.dtb', INITRAMFS)},
        'disk_before': before, 'disk_after': check('after-stage-receive-boot'),
        'SD_written': False, 'booted_on_hardware': False,
    }
    (out/'RECEIVER_BOOT.json').write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('platform', 'ram-assets', 'initramfs', 'baseline', 'mkimage', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    stage(parser.parse_args())
