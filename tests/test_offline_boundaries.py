import binascii
import importlib.util
import os
from pathlib import Path
import select
import tempfile
import termios
import threading
import time
import tty
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
def module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class Boundaries(unittest.TestCase):
    def test_parameter_sets_before_first_aud_do_not_invent_a_frame(self):
        c = module("codec_sweep")
        nal = lambda kind, raw=b"abc": b"\0\0\0\1" + bytes([kind]) + raw
        data = nal(7) + nal(8) + nal(9) + nal(5) + nal(5) + nal(9) + nal(1) + nal(1)
        result = c.measure_stream(data)
        self.assertEqual(result["au_bytes"]["count"], 2)
        self.assertEqual(result["slices_per_au"]["mean"], 2)
        self.assertEqual(result["source_symbols"], 8)
        self.assertEqual(result["idr_au_count"], 1)

    def test_empty_nal_and_metadata_only_au_are_errors(self):
        c = module("codec_sweep")
        for raw in (b"\0\0\1", b"\0\0\1\0\0\1\x65x"):
            with self.assertRaises(ValueError): c.annexb_units(raw)
        with self.assertRaises(ValueError): c.access_units([(9,b"aud"),(7,b"sps")])

    def test_mds_failure_is_block_tail_not_mean_loss(self):
        p = module("capacity_budget")
        self.assertAlmostEqual(p.residual_probability(24,32,.15),.0412979981478336)
        self.assertEqual(p.residual_probability(24,32,0),0)
        self.assertEqual(p.residual_probability(24,32,1),1)

    def test_serial_binary_transfer_retries_crc_checked_packet(self):
        image = module("serial_image")
        master, slave = os.openpty()
        tty.setraw(master); tty.setraw(slave)
        errors = []
        payload = bytes(range(256)) * 49
        def exact(n):
            result = bytearray(); end = time.monotonic()+10
            while len(result)<n:
                if time.monotonic()>end: raise TimeoutError("fake U-Boot peer")
                if select.select([master],[],[],.1)[0]: result.extend(os.read(master,n-len(result)))
            return bytes(result)
        def peer():
            try:
                line=b""
                while not line.endswith(b"\n"): line+=exact(1)
                self.assertIn(b"loadx",line)
                os.write(master,b"## Switch baudrate and press ENTER ...\r\n")
                self.assertEqual(exact(1),b"\r")
                os.write(master,b"## Ready for binary download at 921600 bps...\r\n")
                # Wait for receiver's initial control retry; sender can have
                # consumed a C in the preceding console read.
                time.sleep(.05); os.write(master,b"C")
                data=bytearray(); number=1; injected=False
                while len(data)<len(payload):
                    packet=exact(1029)
                    self.assertEqual(packet[:3],bytes([2,number&255,255-(number&255)]))
                    self.assertEqual(int.from_bytes(packet[-2:],"big"),binascii.crc_hqx(packet[3:-2],0))
                    if number==3 and not injected:
                        injected=True; os.write(master,b"\x15"); continue
                    data.extend(packet[3:-2]); number+=1; os.write(master,b"\x06")
                self.assertEqual(bytes(data[:len(payload)]),payload)
                self.assertEqual(exact(1),b"\x04")
                os.write(master,b"\x15")
                self.assertEqual(exact(1),b"\x04")
                os.write(master,b"\x06## Switch baudrate and press ESC ...\r\n")
                self.assertEqual(exact(1),b"\x1b")
                os.write(master,b"Zynq> ")
            except BaseException as e:
                errors.append(e)
        worker=threading.Thread(target=peer,daemon=True);worker.start()
        def write(raw):
            while raw: raw=raw[os.write(slave,raw):]
        def until(name,predicate,timeout):
            raw=bytearray();end=time.monotonic()+timeout
            while time.monotonic()<end:
                if select.select([slave],[],[],.05)[0]:raw.extend(os.read(slave,65536))
                if predicate(raw):return bytes(raw)
            raise TimeoutError(name)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);path=root/"image.bin";path.write_bytes(payload)
                image.transfer(slave,path,0x4000000,write,until,root,baud=921600)
                import json
                self.assertEqual(json.loads((root/"serial-image.bin.json").read_text())["retries"],1)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            if errors:raise errors[0]
        finally:
            os.close(master);os.close(slave)


if __name__ == "__main__": unittest.main()
