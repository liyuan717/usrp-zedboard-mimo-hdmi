"""Bounded Annex-B AU framing and baseline/single-slice admission checks."""
import re

START = re.compile(b'\x00\x00(?:\x00)?\x01')
MAX_AU = 1_638_364


def nals(data):
    starts = list(START.finditer(data))
    if not starts or starts[0].start() != 0:
        raise ValueError('Annex-B must start with a start code')
    return [data[m.end():(starts[i+1].start() if i+1 < len(starts) else len(data))]
            for i, m in enumerate(starts)]


def access_units(stream, cancelled, max_bytes=MAX_AU):
    """AUD-delimited, bounded streaming parser; supports arbitrary pipe splits."""
    buf = bytearray()
    scan = 0
    first = None
    while not cancelled.is_set():
        chunk = (stream.read1 if hasattr(stream, 'read1') else stream.read)(16384)
        if chunk:
            buf.extend(chunk)
        while True:
            match = START.search(buf, scan)
            if match is None:
                scan = max(scan, len(buf)-4)
                break
            if match.end() == len(buf):
                scan = match.start()
                break
            scan = match.end()+1
            if buf[match.end()] & 31 == 9:
                if first is None:
                    if match.start() != 0:
                        raise ValueError('non-AUD prefix before first AU')
                    first = match.start()
                elif match.start() != first:
                    boundary = match.start()
                    au = bytes(buf[:boundary])
                    if len(au) > max_bytes:
                        raise ValueError('AU exceeds bounded fragment capacity')
                    yield au
                    del buf[:boundary]
                    scan -= boundary
                    first = 0
        if len(buf) > max_bytes+16389:
            raise ValueError('AUD missing or encoded AU too large')
        if not chunk:
            if buf:
                if first != 0 or len(buf) > max_bytes:
                    raise ValueError('invalid final bounded AU')
                yield bytes(buf)
            return


class Bits:
    def __init__(self, data):
        self.data = data.replace(b'\x00\x00\x03', b'\x00\x00')
        self.pos = 0

    def get(self, count=1):
        if self.pos+count > len(self.data)*8:
            raise ValueError('truncated H264 syntax')
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.pos//8] >> (7-self.pos%8)) & 1)
            self.pos += 1
        return value

    def ue(self):
        zeros = 0
        while self.get() == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError('unbounded Exp-Golomb')
        return (1 << zeros)-1 + self.get(zeros)

    def se(self):
        x = self.ue()
        return (x+1)//2 if x & 1 else -(x//2)


def check_sps(nal):
    b = Bits(nal[1:])
    profile, constraints = b.get(8), b.get(8)
    b.get(8)  # level
    if profile != 66 or not constraints & 0x40:
        raise ValueError('constrained-baseline SPS required')
    b.ue(); b.ue()
    order = b.ue()
    if order == 0:
        b.ue()
    elif order == 1:
        b.get(); b.se(); b.se()
        for _ in range(b.ue()):
            b.se()
    elif order != 2:
        raise ValueError('invalid pic_order_cnt_type')
    b.ue(); b.get()
    width, height = (b.ue()+1)*16, (b.ue()+1)*16
    if b.get() != 1:
        raise ValueError('interlaced SPS is not admitted')
    b.get()
    if b.get():
        left, right, top, bottom = (b.ue() for _ in range(4))
        width -= 2*(left+right); height -= 2*(top+bottom)
    if (width, height) != (1024, 600):
        raise ValueError('SPS must describe 1024x600 progressive YUV420')


def validate_au(data):
    units = nals(data)
    types = [n[0] & 31 for n in units if n]
    if not units[0] or types[0] != 9 or types.count(9) != 1:
        raise ValueError('one leading AUD per AU required')
    slices = [n for n in units if n and n[0] & 31 in (1, 5)]
    if len(slices) != 1:
        raise ValueError('one VCL slice per AU required')
    if any(not n or n[0] & 0x80 for n in units):
        raise ValueError('empty or forbidden NAL')
    bits = Bits(slices[0][1:])
    if bits.ue() != 0 or bits.ue() % 5 not in (0, 2):
        raise ValueError('only single-slice I/P pictures supported; no B frames')
    for n in units:
        if n[0] & 31 == 7:
            check_sps(n)
        if n[0] & 31 == 8:
            p = Bits(n[1:]); p.ue(); p.ue()
            if p.get():
                raise ValueError('CABAC is outside baseline contract')
    idr = 5 in types
    if idr and not (7 in types and 8 in types):
        raise ValueError('independent IDR must carry SPS and PPS in the same AU')
    return int(idr) | (int(7 in types) << 1) | (int(8 in types) << 2)
