#!/usr/bin/env python3
"""Review the packaged U-Boot SD startup path and exercise its loader branches on the host."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
from zlib import crc32

from stage_g0_receive_sd_image import FILES, SECTOR, identity

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
from uboot_contract import fdt_properties, verify_control_dtb

MOCK = r"""
setenv() { printf -v "$1" '%s' "$2"; }
fatload() {
    test "$#" = 4 && test "$1" = mmc && test "$2" = 0:1 || exit 3
    test "$4" != "$OMIT" && test -f "$INPUTS/$4" || return 1
    printf -v filesize '%x' "$(wc -c < "$INPUTS/$4")"
    printf 'FATLOAD %s %s\n' "$3" "$4"
}
bootz() {
    test "$#" = 3 || exit 3
    printf 'BOOTARGS %s\nBOOTZ %s %s %s\n' "$bootargs" "$1" "$2" "$3"
    exit 0
}
source "$SCRIPT"
"""


def embedded_control(payload):
    found = []
    for match in re.finditer(b'\xd0\x0d\xfe\xed', payload):
        start = match.start()
        if start+40 > len(payload):
            continue
        size = struct.unpack_from('>I', payload, start+4)[0]
        if 40 <= size <= len(payload)-start:
            data = payload[start:start+size]
            try:
                fdt_properties(data)
            except (ValueError, struct.error):
                continue
            found.append(data)
    if len(found) != 1:
        raise ValueError('Expected one U-Boot control DT in the boot partition')
    return found[0]


def boot_environment(payload):
    marker = b'bootcmd=run distro_bootcmd\0'
    if payload.count(marker) != 1:
        raise ValueError('Expected one built-in distro boot environment')
    start = payload.index(marker)
    end = payload.index(b'\0\0', start, start+65536)
    variables = {}
    for entry in payload[start:end].split(b'\0'):
        name, separator, value = entry.decode('ascii').partition('=')
        if not separator or name in variables:
            raise ValueError('Malformed built-in U-Boot environment')
        variables[name] = value
    keys = {
        'bootcmd': 'run distro_bootcmd',
        'distro_bootcmd': 'run bootcmd_${target}',
        'boot_targets': 'mmc0',
        'boot_prefixes': '/',
        'boot_scripts': 'boot.scr',
        'bootcmd_mmc0': 'devnum=0; run mmc_boot',
        'mmc_boot': 'run scan_dev_for_boot_part',
        'scan_dev_for_boot_part': '-bootable',
        'scan_dev_for_boot': 'run scan_dev_for_scripts',
        'scan_dev_for_scripts': 'run boot_a_script',
        'boot_a_script': 'source ${scriptaddr}',
    }
    result = {}
    for key, required in keys.items():
        text = variables.get(key, '')
        if required not in text:
            raise ValueError('Missing MMC startup path in U-Boot environment: '+key)
        result[key] = text
    if ('mmc0' not in result['boot_targets'].split() or '/' not in result['boot_prefixes'].split()
            or 'boot.scr' not in result['boot_scripts'].split()):
        raise ValueError('U-Boot does not scan for /boot.scr on MMC0')
    return result


def unpack_script(binary):
    header = struct.unpack_from('>7I4B32s', binary)
    body = binary[64:]
    if (header[0] != 0x27051956 or header[7:11] != (5,2,6,0)
            or crc32(binary[:4]+bytes(4)+binary[8:64]) != header[1]
            or len(body) != header[3] or crc32(body) != header[6]):
        raise ValueError('Invalid U-Boot script header or payload')
    length, terminator = struct.unpack_from('>II', body)
    script = body[8:8+length]
    if terminator or script != (ROOT/'board/g0_receive_boot.cmd').read_bytes():
        raise ValueError('SD boot script differs from its source')
    return script


def review(args):
    manifest = json.loads((args.bundle/'RECEIVER_BOOT.json').read_text())
    sd = json.loads((args.sd/'SD_IMAGE.json').read_text())
    image = args.sd/'v3-receiver-candidate.img'
    if sd['image'] != identity(image) or manifest['files'] != sd['files_read_back']:
        raise ValueError('SD image or staged boot files differ from their receipts')
    env = {**os.environ, 'MTOOLSRC': '/dev/null'}
    mcopy = args.mcopy.absolute()
    volume = f'{image.absolute()}@@{sd["boot_partition_lba"]*SECTOR}'
    listing = subprocess.check_output([mcopy.with_name('mdir'), '-b', '-i', volume, '::/'], text=True, env=env)
    if sorted(listing.splitlines()) != sorted('::/'+name for name in FILES):
        raise ValueError('Unexpected FAT root entries or boot environment override')
    with tempfile.TemporaryDirectory(dir=args.sd) as temp:
        work = Path(temp)
        for name in FILES:
            target = work/name
            subprocess.run([mcopy, '-i', volume, '::/'+name, target], check=True, env=env)
            if identity(target) != manifest['files'][name]:
                raise ValueError('FAT boot file differs: '+name)
        start, end = manifest['boot_partition_byte_ranges'][2]
        uboot = (work/'BOOT.BIN').read_bytes()[start:end]
        control = embedded_control(uboot)
        board = verify_control_dtb(control)
        tree = fdt_properties(control)
        [scriptaddr] = struct.unpack('>Q', tree['/options/u-boot']['bootscr-address'])
        mmc = tree['/aliases']['mmc0'].rstrip(b'\0').decode('ascii')
        if tree[mmc].get('status') != b'okay\0':
            raise ValueError('ZedBoard U-Boot MMC0 is disabled')
        script = work/'boot.cmd'
        script.write_bytes(unpack_script((work/'boot.scr').read_bytes()))
        env = {**env, 'INPUTS': temp, 'SCRIPT': str(script)}
        control_path = subprocess.run(['bash','-c',MOCK], text=True, capture_output=True,
                                      env={**env,'OMIT':''}, check=True).stdout.splitlines()
        loads = [('boot.scr',scriptaddr)]
        for line in control_path[:3]:
            verb,address,name = line.split()
            if verb != 'FATLOAD':
                raise ValueError('Expected three checked FAT loads')
            loads.append((name,int(address,16)))
        if [name for name,_ in loads] != ['boot.scr','zImage','hdmi.dtb','v3-receive-initramfs.cpio.gz']:
            raise ValueError('Unexpected boot loader file order')
        ranges = sorted((address,address+manifest['files'][name]['bytes'],name) for name,address in loads)
        if (ranges[-1][1] > board['ram_bytes']
                or any(a[1] > b[0] for a,b in zip(ranges,ranges[1:]))):
            raise ValueError('Boot script and payload memory ranges overlap or escape DDR')
        args_by_file = dict(loads)
        initramfs = 'v3-receive-initramfs.cpio.gz'
        expected_bootz = (f'BOOTZ {args_by_file["zImage"]:#010x} {args_by_file[initramfs]:#010x}:'
                          f'{manifest["files"][initramfs]["bytes"]:x} {args_by_file["hdmi.dtb"]:#010x}')
        if control_path[3:] != ['BOOTARGS console=ttyPS0,115200 rdinit=/init cma=64M', expected_bootz]:
            raise ValueError('Unexpected Linux boot command or initramfs length')
        failed_loads = []
        for name,_ in loads[1:]:
            failure = subprocess.run(['bash','-c',MOCK], text=True, capture_output=True,
                                     env={**env,'OMIT':name})
            if failure.returncode != 1 or 'BOOTZ ' in failure.stdout:
                raise ValueError('Missing FAT file did not stop the script: '+name)
            failed_loads.append(name)
    return {
        'scope': __doc__, 'image': sd['image'], 'uboot_partition_sha256': sha256(uboot).hexdigest(),
        'uboot_board': board, 'uboot_MMC0_path': mmc, 'embedded_environment': boot_environment(uboot),
        'script_address_from_control_DT': scriptaddr, 'nonoverlapping_load_ranges': ranges,
        'host_mock_boot_path': control_path, 'missing_file_failure_checks': failed_loads,
        'physical_SD_written': False, 'hardware_cold_boot_passed': False,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'sd', 'mcopy', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    result = review(args)
    with args.output.open('x') as receipt:
        json.dump(result, receipt, indent=2)
        receipt.write('\n')
