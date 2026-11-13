#!/usr/bin/env python3
"""Stage an offline receiver-only initramfs using the qualified Buildroot userspace."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

from stage_g0_receive_bundle import EXECUTABLES, digest
from workspace_storage_guard import GIB, check


BASE_FILES = (
    'bin/busybox', 'lib/ld-linux-armhf.so.3', 'lib/libc.so.6', 'lib/libresolv.so.2',
)
APPLETS = ('cat', 'dirname', 'flock', 'grep', 'insmod', 'mkdir', 'mount', 'sh', 'sleep', 'tr', 'uname')
INITTAB = (
    '::sysinit:/etc/v3-receive-init mounts\n'
    '::once:/etc/v3-receive-init receiver\n'
)


def stage(args):
    bundle = args.bundle.resolve()
    image = args.buildroot_image.resolve(strict=True)
    if not image.is_file():
        raise ValueError('Expected a local Buildroot filesystem image file')
    manifest = json.loads((bundle / 'RECEIVE_BUNDLE.json').read_text())
    inventory = manifest['files']
    if manifest['kernel_release'] != '6.12.0-xilinx' or manifest['kernel_crc32'] != '13f60426':
        raise ValueError('Expected the qualified receiver kernel')
    if not EXECUTABLES <= inventory.keys():
        raise ValueError('Receive bundle is missing an executable')
    for name, info in inventory.items():
        path = bundle / name
        mode = 0o755 if name in EXECUTABLES else 0o644
        if path.resolve() != path or bundle not in path.parents or not path.is_file():
            raise ValueError('Invalid receive bundle path: ' + name)
        if info['bytes'] != path.stat().st_size or info['sha256'] != digest(path):
            raise ValueError('Receive bundle content differs: ' + name)
        if int(info['mode'], 8) != mode or stat.S_IMODE(path.stat().st_mode) != mode:
            raise ValueError('Receive bundle mode differs: ' + name)

    before = check('before-stage-receive-initramfs')
    if before['disk']['available_bytes'] < 5 * GIB:
        raise RuntimeError('Disk space below initramfs staging threshold')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    root = out / 'root'
    for directory in ('bin', 'dev', 'etc', 'lib', 'proc', 'run', 'sys/kernel/debug'):
        (root / directory).mkdir(parents=True)
    extracted = {}
    for name in BASE_FILES:
        target = root / name
        # debugfs reads the source filesystem image without mounting it.
        subprocess.run(['debugfs', '-R', f'dump "/{name}" "{target}"', image], check=True)
        header = target.read_bytes()[:52]
        # ELF32 little-endian, EM_ARM and EABI5 with the hard-float flag.
        if (header[:7] != b'\x7fELF\x01\x01\x01' or header[18:20] != b'\x28\x00'
                or (int.from_bytes(header[36:40], 'little') & 0xff000600) != 0x05000400):
            raise ValueError('Buildroot file is not ARM hard-float ELF: ' + name)
        target.chmod(0o755)
        extracted[name] = digest(target)
    for applet in APPLETS:
        (root / 'bin' / applet).symlink_to('busybox')
    (root / 'init').symlink_to('bin/busybox')
    (root / 'etc/inittab').write_text(INITTAB)
    init = root / 'etc/v3-receive-init'
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'board/g0_receive_init.sh', init)
    init.chmod(0o755)
    for name in inventory:
        target = root / 'opt/video-radio-v3' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundle / name, target)

    archive = out / 'v3-receive-initramfs.cpio.gz'
    with archive.open('xb') as stream:
        subprocess.run(
            ['fakeroot', 'bash', '-e', '-o', 'pipefail', '-c',
             'mknod -m 600 dev/console c 5 1\n'
             'mknod -m 666 dev/null c 1 3\n'
             'find . -print0 | LC_ALL=C sort -z | '
             'cpio --null --create --format=newc --owner=0:0 --quiet | gzip -n -1'],
            cwd=root, stdout=stream, check=True,
        )
    receipt = {
        'scope': __doc__, 'archive_sha256': digest(archive), 'archive_bytes': archive.stat().st_size,
        'source_image': str(image), 'buildroot_file_sha256': extracted,
        'receive_bundle_manifest_sha256': digest(bundle / 'RECEIVE_BUNDLE.json'),
        'kernel_crc32': manifest['kernel_crc32'], 'booted': False, 'SD_written': False,
        'disk_before': before, 'disk_after': check('after-stage-receive-initramfs'),
    }
    (out / 'INITRAMFS.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    os.umask(0o022)
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('bundle', 'buildroot-image', 'output'):
        parser.add_argument('--' + argument, required=True, type=Path)
    stage(parser.parse_args())
