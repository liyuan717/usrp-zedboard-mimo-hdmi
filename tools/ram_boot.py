#!/usr/bin/env python3
"""G0 fresh RAM boot using explicit existing assets; SD stays read-only."""
import argparse
import fcntl
import gzip
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import termios
import threading
import time
import urllib.request
import uuid
import zlib
from serial_image import transfer


def receiver_ram_inputs(a):
    if not a.receiver_sd_bundle and not a.diagnostic_ram_root:
        return None
    if not (a.receiver_sd_bundle and a.diagnostic_ram_root and a.serial_load and a.kernel_from_sd):
        raise ValueError('Receiver SD RAM diagnostics require both assets, serial PL load and SD kernel read')
    bundle = a.receiver_sd_bundle
    manifest = json.loads((bundle / 'RECEIVER_BOOT.json').read_text())
    initrd = (bundle / 'v3-receive-initramfs.cpio.gz').read_bytes()
    if manifest['files']['v3-receive-initramfs.cpio.gz'] != {
            'bytes': len(initrd), 'sha256': hashlib.sha256(initrd).hexdigest()}:
        raise ValueError('Receiver initramfs differs from the verified bundle')
    packed = a.diagnostic_ram_root.read_bytes()
    raw = gzip.decompress(packed)
    if len(raw) != 40 * 1024**2 or raw[1080:1082] != bytes.fromhex('53ef'):
        raise ValueError('Expected the 40 MiB diagnostic ext4 RAM image')
    return dict(initrd_bytes=len(initrd), initrd_crc=f'{zlib.crc32(initrd):08x}',
                gzip_bytes=len(packed), gzip_crc=f'{zlib.crc32(packed):08x}', root_crc=f'{zlib.crc32(raw):08x}',
                root_sha256=hashlib.sha256(raw).hexdigest())


def receiver_sd_assets(a, receiver_ram, manifest):
    if not a.receiver_pl_from_sd:
        return None
    if not receiver_ram or not a.wait_for_reset or a.already_uboot:
        raise ValueError('Reusing the SD PL requires the receiver bundle and a fresh physical reset')
    bundle = a.receiver_sd_bundle
    qualified = json.loads((bundle / 'RECEIVER_BOOT.json').read_text())
    bit = next(row for row in manifest['files'] if row['file'] == 'hdmi_pipeline.bit')
    if (not qualified['FSBL_and_U_Boot_unchanged_from_archived_R9']
            or bit['crc32'] != qualified['PL_bit_crc32']):
        raise ValueError('Receiver BOOT.BIN does not match the qualified RAM PL')
    assets = {}
    for name in ('BOOT.BIN', 'hdmi.dtb', 'zImage'):
        payload = (bundle / name).read_bytes()
        if qualified['files'][name] != {
                'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}:
            raise ValueError('Receiver SD bundle asset differs: ' + name)
        if name != 'BOOT.BIN' and payload != (a.assets / name).read_bytes():
            raise ValueError('SD and qualified RAM asset differ: ' + name)
        assets[name] = (len(payload), f'{zlib.crc32(payload):08x}')
    return assets


def read_sd_asset(command, name, address, length, crc, log):
    raw = command(f'if fatload mmc 0:1 0x{address:08x} {name} && '
                  f'test "${{filesize}}" = "{length:x}"; then '
                  f'crc32 0x{address:08x} 0x{length:x}; echo SD_FILE_OK; fi', log)
    if b'\nSD_FILE_OK\r\n' not in raw or not re.search(rb'==>\s*' + crc.encode() + rb'\r?\n', raw):
        raise RuntimeError('Receiver SD readback differs: ' + name)


def switch_to_diagnostic_root(a, inputs, command, read_until, write):
    mount = command(
        'export PATH=/bin:/usr/bin; /bin/busybox mount -t devtmpfs devtmpfs /dev && '
        'busybox mount -t proc proc /proc && busybox mount -t sysfs sysfs /sys && busybox mkdir -p /tmp /newroot && '
        'busybox mount -t tmpfs -o size=128m tmpfs /tmp && '
        f'busybox ip link set eth0 up && busybox ip addr add {a.board}/16 dev eth0 && echo RAM_MOUNTS_OK',
        '07-mounts', False)
    if b'\nRAM_MOUNTS_OK\r\n' not in mount:
        raise RuntimeError('Receiver initramfs mounts or management network failed')

    class RootHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.client_address[0] != a.board or self.path != '/diagnostic-root.ext4.gz':
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(inputs['gzip_bytes']))
            self.end_headers()
            with a.diagnostic_ram_root.open('rb') as source:
                shutil.copyfileobj(source, self.wfile)

    with http.server.ThreadingHTTPServer((a.host, 0), RootHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            raw = command(
                f'busybox wget -q -O /tmp/ram-root.gz http://{a.host}:{server.server_port}/diagnostic-root.ext4.gz && '
                f'test "$(busybox crc32 /tmp/ram-root.gz | busybox cut -d " " -f1)" = {inputs["gzip_crc"]} && '
                'test -b /dev/ram0 && busybox gzip -dc /tmp/ram-root.gz >/dev/ram0 && '
                f'test "$(busybox crc32 /dev/ram0 | busybox cut -d " " -f1)" = {inputs["root_crc"]} && '
                'busybox mount -t ext4 -o ro,noload /dev/ram0 /newroot && busybox rm /tmp/ram-root.gz && echo RAM_IMAGE_OK',
                '07-diagnostic-image', False, 90)
        finally:
            server.shutdown()
            thread.join(5)
    if b'\nRAM_IMAGE_OK\r\n' not in raw:
        raise RuntimeError('RAM root transfer, CRC or read-only ext4 mount failed')
    write(b'mount -o move /dev /newroot/dev && mount -o move /proc /newroot/proc && '
          b'mount -o move /sys /newroot/sys && mount -o move /tmp /newroot/tmp && '
          b'exec /bin/busybox switch_root /newroot /bin/ram-diagnostics-init\n')
    read_until('07-switch-root', lambda raw: b'\nV3_RAM_ROOT_READY\r\n' in raw, 15)
    raw = command("python3 -c 'import json,mmap,socket,struct,subprocess; print(31210)'",
                  '07-python', False)
    if b'\n31210\r\n' not in raw:
        raise RuntimeError('ARM diagnostic Python imports failed')


def preflight(a):
    if not 60 <= a.reset_listen_seconds <= 3600:
        raise ValueError("Physical-reset listening window must be60..3600 seconds")
    receiver_ram = receiver_ram_inputs(a)
    manifest = json.loads((a.assets / "BOOT_MANIFEST.json").read_text())
    for row in manifest["files"]:
        path = a.assets / row["file"]
        if path.stat().st_size != row["bytes"]:
            raise ValueError("boot asset size differs: " + row["file"])
        if f"{zlib.crc32(path.read_bytes()):08x}" != row["crc32"]:
            raise ValueError("boot asset CRC differs: " + row["file"])
    if receiver_ram or not a.serial_load:
        # The HTTP RAM root and TFTP paths depend on the management IP.
        # Detect a missing host address before interrupting a working board.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            try:
                server.bind((a.host, 0))
            except OSError as error:
                raise RuntimeError(f"Management IP {a.host} cannot be bound on the host") from error
    sd_assets = receiver_sd_assets(a, receiver_ram, manifest)
    return receiver_ram, manifest, sd_assets


def boot(a):
    receiver_ram, manifest, sd_assets = preflight(a)
    a.logs.mkdir(parents=True, exist_ok=False)
    with Path(f"/run/user/{os.getuid()}/mimo-photo-console/operation.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for name in ("state", "video/state"):
            with opener.open("http://127.0.0.1:8098/api/" + name, timeout=3) as response:
                if json.load(response)["state"].get("active"):
                    raise RuntimeError("Existing operation is active; wait for it")
        fd = os.open(a.serial, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        lease = Path(f"/run/user/{os.getuid()}/mimo-photo-console/tftp.json")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.ioctl(fd, termios.TIOCEXCL)
            attrs = termios.tcgetattr(fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = termios.B115200
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            def write(data):
                while data:
                    if select.select([], [fd], [], .2)[1]:
                        try:
                            data = data[os.write(fd, data):]
                        except BlockingIOError:
                            pass
            def read_until(name, predicate, timeout):
                raw = bytearray()
                end = time.monotonic() + timeout
                try:
                    while time.monotonic() < end:
                        if select.select([fd], [], [], .1)[0]:
                            raw.extend(os.read(fd, 65536))
                        if b"Hit any key" in raw and b"Zynq>" not in raw:
                            write(b" ")
                        if predicate(raw):
                            return raw
                    raise TimeoutError(name)
                finally:
                    (a.logs / (name + ".log")).write_bytes(raw)
            def command(text, name, uboot=True, timeout=35):
                token = uuid.uuid4().hex
                marker = "V3_" + token
                ending = "; echo " + marker + "\n" if uboot else "\nprintf '\\nV3_%s%s\\n' '" + token[:16] + "' '" + token[16:] + "'\n"
                write((text + ending).encode())
                return read_until(name, lambda raw: b"\n" + marker.encode() + b"\r\n" in raw, timeout)
            print(("Listening for physical reset, window=" + str(a.reset_listen_seconds) + "s") if a.wait_for_reset else "Entering U-Boot", flush=True)
            if a.wait_for_reset:
                pass
            elif a.already_uboot:
                write(b"\n")
            else:
                write(b"reboot -f\n")
            read_until("01-reset", lambda raw: b"Zynq>" in raw, a.reset_listen_seconds if a.wait_for_reset else 45)
            # Installed U-Boot has CONFIG_TFTP_PORT disabled. Reuse the existing
            # diagnostic three-asset service instead of adding a privileged daemon.
            if not a.serial_load:
                lease.write_text(json.dumps({"run_id": uuid.uuid4().hex,
                    "expires_monotonic": time.monotonic() + 170}))
                lease.chmod(0o600)
            command("setenv ipaddr " + a.board + "; setenv serverip " + a.host + "; setenv netmask 255.255.0.0; setenv tftpdstp", "02-network")
            addresses = {"hdmi_pipeline.bit": 0x04000000, "hdmi.dtb": 0x10000000, "zImage": 0x02000000}
            if sd_assets:
                # FSBL loaded the bitstream in this BOOT.BIN during the observed
                # reset. Read it back without reprogramming the running PL.
                for name, address in (('BOOT.BIN', 0x06000000), ('hdmi.dtb', addresses['hdmi.dtb']),
                                      ('zImage', addresses['zImage'])):
                    read_sd_asset(command, name, address, *sd_assets[name], '03-SD-' + name)
            else:
                transfer_rows = sorted(manifest["files"], key=lambda r: r["bytes"]) if a.serial_load else manifest["files"]
                for i, row in enumerate(transfer_rows):
                    if a.kernel_from_sd and row['file']=='zImage':
                        # Read-only reuse of the kernel already checked on SD.
                        # U-Boot rechecks its bytes before executing it; no SD write.
                        text=f"fatload mmc 0:1 0x{addresses[row['file']]:08x} zImage; echo TFTP_RC=$?; crc32 0x{addresses[row['file']]:08x} 0x{row['bytes']:x}"
                    elif a.serial_load:
                        transfer(fd, a.assets / row['file'], addresses[row['file']], write, read_until, a.logs)
                        text = f"echo TFTP_RC=0; crc32 0x{addresses[row['file']]:08x} 0x{row['bytes']:x}"
                    else:
                        text = f"tftpboot 0x{addresses[row['file']]:08x} {row['file']}; echo TFTP_RC=$?; crc32 0x{addresses[row['file']]:08x} 0x{row['bytes']:x}"
                    raw = command(text, f"03-{i}-" + row["file"])
                    if b"TFTP_RC=0" not in raw or not re.search(rb"==>\s*" + row["crc32"].encode(), raw):
                        raise RuntimeError("TFTP/CRC failed " + row["file"])
                bit_bytes = next(x['bytes'] for x in manifest['files'] if x['file'] == 'hdmi_pipeline.bit')
                raw = command(f"fpga loadb 0 0x04000000 0x{bit_bytes:x}; echo FPGA_RC=$?", "04-fpga")
                if b"FPGA_RC=0" not in raw:
                    raise RuntimeError("FPGA load failed")
            if receiver_ram:
                size = receiver_ram['initrd_bytes']
                raw = command(f'fatload mmc 0:1 0x08000000 v3-receive-initramfs.cpio.gz; '
                              f'echo SD_INITRD_RC=$?; crc32 0x08000000 0x{size:x}', '05-receiver-initrd')
                if b'SD_INITRD_RC=0' not in raw or not re.search(
                        rb'==>\s*' + receiver_ram['initrd_crc'].encode(), raw):
                    raise RuntimeError('Receiver SD initramfs read or CRC failed')
                command('setenv bootargs console=ttyPS0,115200 rdinit=/bin/sh cma=64M ramdisk_size=40960',
                        '05-bootargs')
                write(f'bootz 0x02000000 0x08000000:{size:x} 0x10000000\n'.encode())
            else:
                command("setenv tftpdstp; setenv bootargs console=ttyPS0,115200 root=/dev/mmcblk0p2 rootwait ro rootflags=noload rootfstype=ext4 cma=64M init=/bin/sh", "05-bootargs")
                write(b"bootz 0x02000000 - 0x10000000\n")
            read_until("06-kernel", lambda raw: b"job control turned off" in raw or b"~ #" in raw, 45)
            if receiver_ram:
                switch_to_diagnostic_root(a, receiver_ram, command, read_until, write)
            else:
                command("mount -t proc proc /proc; mount -t sysfs sysfs /sys; mount -t tmpfs -o size=128m tmpfs /tmp; ip link set eth0 up; ip addr add " + a.board + "/16 dev eth0", "07-mounts", False)
            raw = command('v3_rf_count=0; for p in /sys/bus/iio/devices/iio:device*; do if [ "$(cat "$p/name")" = ad9361-phy ]; then echo 1 > "$p/out_altvoltage0_RX_LO_powerdown" && echo 1 > "$p/out_altvoltage1_TX_LO_powerdown" && v3_rf_count=$((v3_rf_count+1)); fi; done; echo LO_OFF_OK=$v3_rf_count', '08-LO-off', False)
            if b'\nLO_OFF_OK=1\r\n' not in raw:
                raise RuntimeError('Could not confirm a single AD9361 with both LOs powered down')
            if sd_assets:
                raw = command('if test "$(busybox devmem 0x7c430000)" = 0x50533356 && '
                              'test "$(busybox devmem 0x7c430008)" = 0x00000000; '
                              'then echo SD_SOURCE_PL_IDLE; fi', '08-SD-PL', False, 12)
                if b'\nSD_SOURCE_PL_IDLE\r\n' not in raw:
                    raise RuntimeError('SD-configured SOURCE PL is absent or not idle')
            raw = command("uname -r; cat /proc/sys/kernel/random/boot_id; cat /proc/cmdline; grep ' / ext4 ro,' /proc/mounts", "08-identity", False)
            ids = re.findall(rb"\n([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\r?\n", raw)
            root_ok = (b'/dev/ram0 / ext4 ro,' in raw and b'rdinit=/bin/sh' in raw) if receiver_ram else (
                b'rootflags=noload' in raw and b'/dev/root / ext4 ro,' in raw)
            if len(ids) != 1 or not root_ok:
                raise RuntimeError("Fresh boot identity/read-only root not confirmed")
            receipt = {"boot_id": ids[0].decode(), "SD_written": False, "saveenv": False,
                       "RF_started": False, "kernel_read_from_SD": a.kernel_from_sd,
                       "scope": manifest.get('scope', "existing baseline only for native G0 measurement")}
            if receiver_ram:
                receipt['root_on_readonly_RAM'] = True
                receipt['diagnostic_root_sha256'] = receiver_ram['root_sha256']
            if sd_assets:
                receipt['PL_configured_by_SD_FSBL'] = True
                receipt['SD_boot_bin_crc32'] = sd_assets['BOOT.BIN'][1]
                receipt['SOURCE_PL_idle_verified'] = True
            (a.logs / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt), flush=True)
        finally:
            if not a.serial_load:
                lease.unlink(missing_ok=True)
            fcntl.ioctl(fd, termios.TIOCNXCL)
            os.close(fd)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets", type=Path, required=True)
    p.add_argument("--logs", type=Path, required=True)
    p.add_argument("--already-uboot", action="store_true")
    p.add_argument("--wait-for-reset", action="store_true")
    p.add_argument("--reset-listen-seconds", type=int, default=180, help="bounded physical-reset listener60..3600s; no reset or SD write occurs while waiting")
    p.add_argument("--serial-load", action="store_true", help="load arbitrary explicit diagnostic assets via XMODEM into RAM")
    p.add_argument("--kernel-from-sd", action="store_true", help="read the exact manifest-matching kernel from SD, with RAM CRC check; never writes SD")
    p.add_argument("--receiver-sd-bundle", type=Path, help="use the verified receiver SD initramfs as bootstrap")
    p.add_argument("--receiver-pl-from-sd", action="store_true",
                   help="after physical reset, verify and retain the receiver SD PL instead of reloading it")
    p.add_argument("--diagnostic-ram-root", type=Path, help="40 MiB ext4 diagnostic root compressed with gzip")
    p.add_argument("--host", default="169.254.209.83")
    p.add_argument("--board", default="169.254.209.84")
    p.add_argument("--serial", default="/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00")
    boot(p.parse_args())
