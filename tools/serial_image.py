"""XMODEM-1K transfer for U-Boot loadx; no SD writes or persistent baud changes."""
import binascii
import json
import os
import select
import termios
import time


def transfer(fd, path, address, write, read_until, logs, baud=115200):
    original = termios.tcgetattr(fd)
    try:
        return _transfer(fd, path, address, write, read_until, logs, baud)
    except BaseException:
        write(b"\x18" * 8)
        time.sleep(.2)
        termios.tcsetattr(fd, termios.TCSANOW, original)
        write(b"\x1b\r")
        raise


def _transfer(fd, path, address, write, read_until, logs, baud):
    original = termios.tcgetattr(fd)
    started = time.monotonic()
    retries = 0
    def speed(value):
        attrs = termios.tcgetattr(fd)
        attrs[4] = attrs[5] = value
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    def control(expected, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if select.select([fd], [], [], .1)[0]:
                raw = os.read(fd, 1)
                if raw == b"\x18":
                    raise RuntimeError("U-Boot cancelled XMODEM")
                if raw in expected:
                    return raw
        raise TimeoutError("XMODEM control acknowledgement")
    write(f"loadx 0x{address:08x} {baud}\n".encode())
    if baud != 115200:
        read_until("serial-" + path.name + "-switch", lambda raw: b"press ENTER" in raw, 5)
        time.sleep(.15)
        speed(getattr(termios, "B" + str(baud)))
        write(b"\r")
    read_until("serial-" + path.name + "-ready", lambda raw: b"bps...\r\n" in raw, 5)
    control((b"C",), 10)
    total = path.stat().st_size
    with path.open("rb") as f:
        block = 1
        while True:
            raw = f.read(1024)
            if not raw:
                break
            data = raw.ljust(1024, b"\x1a")
            packet = bytes((2, block & 255, 255 - (block & 255))) + data + binascii.crc_hqx(data, 0).to_bytes(2, "big")
            for attempt in range(10):
                write(packet)
                if control((b"\x06", b"\x15")) == b"\x06":
                    break
                retries += 1
            else:
                raise RuntimeError("XMODEM repeated NAK")
            if block % 512 == 0:
                print(f"Serial {path.name}: {min(block * 1024,total)}/{total} bytes", flush=True)
            block += 1
    write(b"\x04")
    if control((b"\x06", b"\x15")) == b"\x15":
        write(b"\x04")
        control((b"\x06",))
    if baud != 115200:
        read_until("serial-" + path.name + "-complete", lambda raw: b"press ESC" in raw, 5)
        time.sleep(.15)
        termios.tcsetattr(fd, termios.TCSANOW, original)
        write(b"\x1b")
    read_until("serial-" + path.name + "-prompt", lambda raw: b"Zynq>" in raw, 5)
    result = {"file":path.name,"bytes":total,"retries":retries,"seconds":time.monotonic()-started,"baud":baud}
    (logs / ("serial-" + path.name + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
