#!/usr/bin/env python3
"""Build and read back a receiver-only FAT32 SD candidate as a regular file."""
import argparse
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

from workspace_storage_guard import GIB, check

FILES = ('BOOT.BIN', 'boot.scr', 'zImage', 'hdmi.dtb', 'v3-receive-initramfs.cpio.gz')
SECTOR = 512
BOOT_LBA = 2048
BOOT_KIB = 64*1024
BOOT_SECTORS = BOOT_KIB*1024//SECTOR


def identity(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a regular file: ' + str(path))
    with path.open('rb') as stream:
        return {'bytes': path.stat().st_size, 'sha256': file_digest(stream, 'sha256').hexdigest()}


def mbr():
    sector = bytearray(SECTOR)
    # Active FAT32 LBA partition. Linux runs directly from the initramfs.
    struct.pack_into('<B3sB3sII', sector, 446, 0x80, b'\xfe\xff\xff', 0x0C,
                     b'\xfe\xff\xff', BOOT_LBA, BOOT_SECTORS)
    sector[510:512] = b'\x55\xaa'
    return sector


def stage(args):
    bundle, out = args.bundle, args.output
    manifest_path = bundle/'RECEIVER_BOOT.json'
    manifest = json.loads(manifest_path.read_text())
    inventory = {name: identity(bundle/name) for name in FILES}
    if manifest['files'] != inventory or manifest['SD_written']:
        raise ValueError('Boot bundle differs from its offline staging receipt')

    before = check('before-receiver-sd-file')
    if before['disk']['available_bytes'] < 5*GIB:
        raise RuntimeError('Disk space below receiver image staging threshold')
    out.mkdir(parents=True, exist_ok=False)
    image = (out/'v3-receiver-candidate.img').absolute()
    mcopy = str(args.mcopy.absolute())  # preserve the mtools applet's executable name
    env = {**os.environ, 'MTOOLSRC': '/dev/null'}
    with tempfile.TemporaryDirectory(dir=out) as scratch:
        fat = Path(scratch)/'boot.fat'
        with (out/'fat-build.log').open('x') as log:
            subprocess.run(['mkfs.vfat', '-F', '32', '-h', str(BOOT_LBA), '-n', 'V3RECEIVE',
                            '-C', fat, str(BOOT_KIB)], check=True, stdout=log, stderr=subprocess.STDOUT)
            for name in FILES:
                subprocess.run([mcopy, '-i', fat, bundle/name, '::/'+name],
                               check=True, env=env, stdout=log, stderr=subprocess.STDOUT)
        with (out/'fat-fsck.log').open('x') as log:
            subprocess.run(['fsck.vfat', '-n', '-v', fat], check=True, stdout=log, stderr=subprocess.STDOUT)
        with image.open('xb') as disk, fat.open('rb') as partition:
            disk.write(mbr())
            disk.seek(BOOT_LBA*SECTOR)
            shutil.copyfileobj(partition, disk)
        partition_table = subprocess.check_output(['sfdisk', '--json', image], text=True)
        (out/'partition-table.json').write_text(partition_table)
        table = json.loads(partition_table)['partitiontable']
        [boot] = table['partitions']
        if (table['label'] != 'dos' or table['sectorsize'] != SECTOR
                or boot['start'] != BOOT_LBA or boot['size'] != BOOT_SECTORS
                or boot['type'].lower() != 'c' or not boot['bootable']
                or image.stat().st_size != (BOOT_LBA+BOOT_SECTORS)*SECTOR):
            raise ValueError('Unexpected receiver image partition table')
        for name, expected in inventory.items():
            readback = Path(scratch)/name
            subprocess.run([mcopy, '-i', f'{image}@@{BOOT_LBA*SECTOR}', '::/'+name, readback],
                           check=True, env=env)
            if identity(readback) != expected:
                raise ValueError('File differs after readback from SD image: ' + name)

    receipt = {
        'scope': __doc__, 'boot_bundle_receipt': identity(manifest_path), 'image': identity(image),
        'PL_bit_crc32': manifest['PL_bit_crc32'], 'boot_partition_lba': BOOT_LBA,
        'files_read_back': inventory, 'FAT_fsck_passed': True,
        'disk_before': before, 'disk_after': check('after-receiver-sd-file'),
        'physical_SD_written': False, 'hardware_cold_boot_passed': False,
    }
    (out/'SD_IMAGE.json').write_text(json.dumps(receipt, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'mcopy', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    stage(parser.parse_args())
