#!/usr/bin/env python3
"""Restore the frozen R22 image to a confirmed removable spare and verify its readback."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'docs/evidence/g0-R22-offline-SD-virtual-boot-20261106.json'
# Linux fs.h ioctls. BLKGETSIZE64 uses the host's size_t in the request.
BLKFLSBUF = 0x1261
BLKGETSIZE64 = 0x80001272 | (struct.calcsize('P') << 16)
BLKGETDISKSEQ = 0x80081280
CHUNK_BYTES = 1024 * 1024
R9_LABELS = {'RFV2BOOT', 'rfv2-final-r9'}
R9_UUIDS = {'54C5-832D', '225f39d3-51ce-49ac-885e-1df0082046d7'}


def u64_ioctl(fd, request):
    value = bytearray(8)
    fcntl.ioctl(fd, request, value)
    return struct.unpack('=Q', value)[0]


def read_image(path):
    expected = json.loads(EVIDENCE.read_text())['SD_image']
    with path.open('rb') as image:
        if not stat.S_ISREG(os.fstat(image.fileno()).st_mode):
            raise ValueError('Expected the frozen R22 image as a regular file')
        data = image.read(expected['bytes'] + 1)
    if len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
        raise ValueError('Image differs from the frozen R22 image')
    return data, expected


def card_inventory(device, reuse_r9=False):
    raw = subprocess.check_output(
        ['lsblk', '--json', '--bytes', '--paths',
         '--output', 'NAME,TYPE,SIZE,RM,RO,TRAN,PTTYPE,LABEL,PARTLABEL,UUID,MOUNTPOINTS', str(device)],
        text=True,
    )
    card, = json.loads(raw)['blockdevices']
    if (card['name'] != str(device) or card['type'] != 'disk' or not card['rm'] or card['ro']
            or card['tran'] not in ('usb', 'mmc')):
        raise ValueError('Expected a writable whole removable USB or MMC device')
    children = card.get('children', [])
    if reuse_r9 and (len(children) != 2 or {row['label'] for row in children} != R9_LABELS
                     or {row['uuid'] for row in children} != R9_UUIDS):
        raise ValueError('R9 reuse applies only to the explicitly identified original card')
    for row in [card, *children]:
        for field in ('label', 'partlabel'):
            if not reuse_r9 and (row[field] or '').casefold().startswith('rfv2'):
                raise ValueError('Protected R9/RFV2 media')
        if any(row['mountpoints']) or row.get('children') and row is not card:
            raise ValueError('The whole card and its partitions must be unmounted and unused')
    if card['pttype'] not in (None, 'dos'):
        raise ValueError('Use a blank or DOS-partitioned spare card')
    return card


def write_and_verify(fd, image):
    remaining = memoryview(image)
    while remaining:
        written = os.write(fd, remaining[:CHUNK_BYTES])
        if written <= 0:
            raise OSError('No progress writing SD')
        remaining = remaining[written:]
    os.fsync(fd)
    fcntl.ioctl(fd, BLKFLSBUF)
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    count = 0
    while count < len(image):
        data = os.read(fd, min(CHUNK_BYTES, len(image) - count))
        if not data:
            raise OSError('Short physical SD readback')
        digest.update(data)
        count += len(data)
    return {'bytes': count, 'sha256': digest.hexdigest()}


def main(args):
    device = args.device.resolve(strict=True)
    before = device.stat()
    if device.parent != Path('/dev') or not stat.S_ISBLK(before.st_mode):
        raise ValueError('Expected a block device under /dev')
    card = card_inventory(device, reuse_r9=args.reuse_r9)
    if card['size'] != args.capacity_bytes:
        raise ValueError('Device size differs from the identified spare')
    image, expected = read_image(args.image)
    if len(image) > card['size']:
        raise ValueError('Spare is smaller than the image')
    flags = os.O_RDWR | os.O_EXCL if args.write else os.O_RDONLY
    fd = os.open(device, flags | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if (opened.st_rdev != before.st_rdev or u64_ioctl(fd, BLKGETDISKSEQ) != args.diskseq
                or u64_ioctl(fd, BLKGETSIZE64) != args.capacity_bytes):
            raise ValueError('Open device differs from the identified spare')
        receipt = {'device': str(device), 'diskseq': args.diskseq, 'card_bytes': args.capacity_bytes,
                   'image': expected, 'physical_SD_written': args.write,
                   'original_R9_card_reused': args.reuse_r9 and args.write}
        if args.write:
            verified = write_and_verify(fd, image)
            if verified != expected or u64_ioctl(fd, BLKGETDISKSEQ) != args.diskseq:
                raise OSError('Physical SD readback does not match the frozen R22 image')
            receipt['physical_SD_readback'] = verified
        print(json.dumps(receipt, indent=2))
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--device', type=Path, required=True)
    parser.add_argument('--capacity-bytes', type=int, required=True)
    parser.add_argument('--diskseq', type=int, required=True)
    parser.add_argument('--reuse-r9', action='store_true', help='target the specifically identified R9 card')
    parser.add_argument('--write', action='store_true', help='restore only to the explicitly confirmed card')
    main(parser.parse_args())
