"""Socket-activated, unprivileged, fixed three-file RRQ-only TFTP service."""
import json
import os
import socket
import stat
import struct
import threading
import time

from config import BOOT, HOST, BOARD, LEASE, sha


def active():
    try:
        info = LEASE.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return False
        lease = json.loads(LEASE.read_text())
        return (set(lease) == {'run_id', 'expires_monotonic'}
                and len(lease['run_id']) == 32 and all(c in '0123456789abcdef' for c in lease['run_id'])
                and 0 < lease['expires_monotonic'] - time.monotonic() <= 180)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def parse_request(raw, assets):
    parts = raw[2:].split(b'\0')
    if len(raw) > 2048 or raw[:2] != b'\0\1' or parts[-1] != b'' or len(parts) % 2 != 1:
        raise ValueError('RRQ only')
    name, mode = parts[:2]
    name = name.decode('ascii')
    if name not in assets or mode.lower() != b'octet':
        raise ValueError('fixed filename/octet only')
    options = {}
    for i in range(2, len(parts) - 1, 2):
        key, value = parts[i].decode('ascii').lower(), parts[i + 1].decode('ascii')
        if key in options or not value.isdigit():
            raise ValueError('invalid option')
        if key == 'blksize':
            options[key] = str(min(1468, max(8, int(value))))
        elif key == 'tsize' and value == '0':
            options[key] = str(len(assets[name]))
        elif key == 'timeout' and 1 <= int(value) <= 3:
            options[key] = value
    return name, options


def transfer(peer, request, assets):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
        stream.bind((HOST, 0))
        stream.settimeout(1)
        try:
            if not active() or peer[0] != BOARD:
                raise ValueError('not enabled for this peer')
            name, options = parse_request(request, assets)
            blocksize = int(options.get('blksize', '512'))
            def exchange(packet, number):
                for _ in range(4):
                    if not active():
                        raise ValueError('run stopped/lease expired')
                    stream.sendto(packet, peer)
                    end = time.monotonic() + 1
                    while time.monotonic() < end:
                        try:
                            ack, address = stream.recvfrom(2048)
                        except socket.timeout:
                            break
                        if address == peer and ack == struct.pack('!HH', 4, number):
                            return
                raise TimeoutError('ACK timeout')
            if options:
                exchange(b'\0\6' + b''.join(k.encode() + b'\0' + v.encode() + b'\0' for k, v in options.items()), 0)
            data = assets[name]
            for offset in range(0, len(data) + 1, blocksize):
                payload = data[offset:offset + blocksize]
                number = offset // blocksize + 1
                if number > 65535:
                    raise ValueError('block range')
                exchange(struct.pack('!HH', 3, number) + payload, number)
                if len(payload) < blocksize:
                    break
            print(json.dumps(dict(file=name, peer=peer[0], bytes=len(data), sent=True)), flush=True)
        except (OSError, ValueError, TimeoutError, UnicodeError) as error:
            try:
                stream.sendto(b'\0\5\0\2request rejected\0', peer)
            except OSError:
                pass
            print(json.dumps(dict(peer=peer[0], error=str(error))), flush=True)


def main():
    if int(os.environ.get('LISTEN_PID', 0)) != os.getpid() or os.environ.get('LISTEN_FDS') != '1':
        raise RuntimeError('requires dedicated systemd UDP socket')
    assets = {}
    for row in json.loads((BOOT / 'BOOT_MANIFEST.json').read_text())['files']:
        path = BOOT / row['file']
        if path.name not in ('hdmi_pipeline.bit', 'hdmi.dtb', 'zImage') or sha(path) != row['sha256']:
            raise RuntimeError('boot asset hash mismatch')
        assets[path.name] = path.read_bytes()
    sock = socket.socket(fileno=3)
    assert sock.getsockname() == (HOST, 69)
    sock.settimeout(.25)
    sessions = {}
    last_request = time.monotonic()
    try:
        while time.monotonic() - last_request < 180:
            try:
                request, peer = sock.recvfrom(2049)
            except socket.timeout:
                continue
            last_request = time.monotonic()
            sessions = {key: thread for key, thread in sessions.items() if thread.is_alive()}
            if peer in sessions or len(sessions) >= 2:
                continue
            thread = threading.Thread(target=transfer, args=(peer, request, assets), daemon=True)
            sessions[peer] = thread
            thread.start()
    finally:
        sock.close()


if __name__ == '__main__':
    main()
