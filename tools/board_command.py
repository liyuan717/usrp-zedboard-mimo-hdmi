#!/usr/bin/env python3
"""Exclusive development UART command; no resets, RF or SD actions implicit."""
import argparse
import fcntl
import os
from pathlib import Path
import select
import re
import termios
import time
import uuid
from hardware_lease import HardwareLease


def require_boot_id(identity, expected=None):
    ids = re.findall(r'(?m)^([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\r?$', identity)
    if len(ids) != 1:
        raise ValueError("Expected one live board boot ID")
    if expected is not None and ids[0] != expected:
        raise ValueError("Live board boot ID differs from the qualified RAM boot")
    return ids[0]


def command(serial, text, output, timeout, lease=None):
    if lease is None:
        with HardwareLease() as owned:
            return command(serial, text, output, timeout, lease=owned)
    lease.assert_held()
    fd = os.open(serial, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    raw = bytearray()
    exclusive = False
    old = None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        exclusive = True
        old = termios.tcgetattr(fd)
        # This exclusively owned hardware port stays raw/115200 between calls.
        # Restoring desktop9600/ECHO can reprogram the bridge and echo a board
        # login prompt back as input before the next caller configures raw mode.
        old[0] = old[1] = old[3] = 0
        old[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        old[4] = old[5] = termios.B115200
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        time.sleep(.1)
        token = uuid.uuid4().hex
        if any(len(line.encode()) > 470 for line in text.splitlines()):
            raise ValueError("UART command line exceeds shell input limit")
        wire = ("(\nset -e\n" + text.rstrip() + "\n)\nv3_rc=$?\nprintf '\\nV3_DONE_%s%s_%d\\n' '" + token[:16] + "' '" + token[16:] + "' \"$v3_rc\"\n").encode()
        while wire:
            select.select([], [fd], [], .5)
            try:
                sent = os.write(fd, wire)
                wire = wire[sent:]
            except BlockingIOError:
                pass
        end = time.monotonic() + timeout
        marker = ("\nV3_DONE_" + token + "_").encode()
        while time.monotonic() < end:
            if select.select([fd], [], [], .1)[0]:
                try:
                    raw.extend(os.read(fd, 65536))
                except BlockingIOError:
                    pass
            match = re.search(re.escape(marker) + rb"(\d+)\r?\n", raw)
            if match:
                if int(match.group(1)):
                    raise RuntimeError("Board command exited " + match.group(1).decode() + "; see " + str(output))
                return raw
        raise TimeoutError("No UART completion marker; inspect saved log before retrying")
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
        if old is not None:
            termios.tcsetattr(fd, termios.TCSANOW, old)
        if exclusive:
            fcntl.ioctl(fd, termios.TIOCNXCL)
        os.close(fd)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", default="/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00")
    p.add_argument("--script", type=Path, required=True)
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--timeout", type=float, default=30)
    a = p.parse_args()
    print(command(a.serial, a.script.read_text(), a.log, a.timeout).decode(errors="replace"))
