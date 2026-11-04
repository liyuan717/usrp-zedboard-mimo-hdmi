"""Standalone, standard-library MRF1-v1 2x2 QPSK air-interface reference.

Does not import the FPGA project, UHD, NumPy, or any device API. Generates
test data only; executing this file never enables RF transmission.
"""

import argparse
import hashlib
import json
import math
import struct
import unittest
import zlib
from pathlib import Path

PROFILE = {
    "profile_id": "MRF1-QPSK-2X2-4M-v1",
    "sample_rate_hz": 4_000_000,
    "symbol_rate_hz": 1_000_000,
    "samples_per_symbol": 4,
    "rolloff": 0.35,
    "rrc_span_symbols": 10,
    "rrc_tap_count": 41,
    "payload_bytes_per_stream": 128,
    "pilot_interval_symbols": 64,
    "iq_peak_component": 0.125,
    "frame_symbols": 950,
    "frame_scans": 3840,
}
POINTS = tuple(value / math.sqrt(2) for value in (1 + 1j, -1 + 1j, -1 - 1j, 1 - 1j))
HEADER = struct.Struct("<4sBBBBII")


def uint32(value, name):
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(name + " must be uint32")
    return value


def deterministic_bytes(domain, seed, frame_id, stream, count):
    for name, value in (("seed", seed), ("frame_id", frame_id), ("stream", stream)):
        uint32(value, name)
    prefix = domain + b"\0" + struct.pack("<III", seed, frame_id, stream)
    return b"".join(
        hashlib.sha256(prefix + struct.pack("<I", index)).digest()
        for index in range((count + 31) // 32)
    )[:count]


def indices(data):
    return [byte >> shift & 3 for byte in data for shift in (6, 4, 2, 0)]


def known(domain, count):
    raw = deterministic_bytes(domain, 0, 0, 0, (count + 3) // 4)
    return [POINTS[index] for index in indices(raw)[:count]]


def packet(payload, frame_id, stream):
    uint32(frame_id, "frame_id")
    if type(stream) is not int or stream not in (0, 1):
        raise ValueError("stream must be 0 or 1")
    if type(payload) is not bytes or len(payload) != 128:
        raise ValueError("this profile requires exactly 128 immutable payload bytes")
    prefix = HEADER.pack(b"MRF1", 1, stream, 1, 0, frame_id, len(payload))
    return (
        prefix
        + struct.pack("<I", zlib.crc32(prefix))
        + payload
        + struct.pack("<I", zlib.crc32(payload))
    )


def parse_packet(data, expected_stream):
    if type(data) is not bytes or len(data) != 152:
        raise ValueError("exactly 152 packet bytes required")
    magic, version, stream, modulation, flags, frame_id, size = HEADER.unpack(data[:16])
    if (magic, version, stream, modulation, flags, size) != (
        b"MRF1",
        1,
        expected_stream,
        1,
        0,
        128,
    ):
        raise ValueError("packet identity/profile mismatch")
    if zlib.crc32(data[:16]) != struct.unpack_from("<I", data, 16)[0]:
        raise ValueError("header CRC mismatch")
    if zlib.crc32(data[20:148]) != struct.unpack_from("<I", data, 148)[0]:
        raise ValueError("payload CRC mismatch")
    return frame_id, data[20:148]


def frame_symbols(payloads, frame_id):
    if type(payloads) is not tuple or len(payloads) != 2:
        raise ValueError("two payloads required")
    packets = [packet(payloads[stream], frame_id, stream) for stream in (0, 1)]
    symbols = [[POINTS[index] for index in indices(data)] for data in packets]
    left, right, sections = [], [], []

    def add(name, a, b):
        if len(a) != len(b):
            raise AssertionError("lane lengths differ")
        sections.append(
            {"name": name, "start_symbol": len(left), "symbol_count": len(a)}
        )
        left.extend(a)
        right.extend(b)

    def zero(count):
        return [0j] * count

    preamble = known(b"MRF1-PREAMBLE", 32) * 2 + known(b"MRF1-SFD", 32)
    training = known(b"MRF1-TRAINING", 64)
    pilots = known(b"MRF1-PILOT", 2)
    add("leading_guard", zero(32), zero(32))
    add("preamble_tx0", preamble, zero(96))
    add("separator_before_training", zero(12), zero(12))
    add("training_tx0", training, zero(64))
    add("training_separator", zero(12), zero(12))
    add("training_tx1", zero(64), training)
    add("separator_before_header", zero(12), zero(12))
    add("headers", symbols[0][:80], symbols[1][:80])
    for number, start in enumerate(range(0, 528, 64)):
        add("pilots_" + str(number), [pilots[0], 0j], [0j, pilots[1]])
        add(
            "payload_" + str(number),
            symbols[0][80 + start : 80 + start + 64],
            symbols[1][80 + start : 80 + start + 64],
        )
    add("trailing_guard", zero(32), zero(32))
    assert len(left) == len(right) == 950
    return (left, right), packets, sections


def rrc_taps():
    beta = 0.35
    taps = []
    for index in range(-20, 21):
        t = index / 4
        if abs(t) < 1e-12:
            value = 1 + beta * (4 / math.pi - 1)
        elif abs(abs(t) - 1 / (4 * beta)) < 1e-12:
            value = (
                beta
                / math.sqrt(2)
                * (
                    (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                    + (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta))
                )
            )
        else:
            value = (
                math.sin(math.pi * t * (1 - beta))
                + 4 * beta * t * math.cos(math.pi * t * (1 + beta))
            ) / (math.pi * t * (1 - (4 * beta * t) ** 2))
        taps.append(value)
    norm = math.sqrt(sum(value * value for value in taps))
    return [value / norm for value in taps]


def frame(payloads, frame_id):
    symbols, packets, sections = frame_symbols(payloads, frame_id)
    taps = rrc_taps()
    samples = [[0j] * 3840 for _ in (0, 1)]
    for stream in (0, 1):
        for index, symbol in enumerate(symbols[stream]):
            for tap, gain in enumerate(taps):
                samples[stream][index * 4 + tap] += symbol * gain
    peak = max(
        abs(component)
        for lane in samples
        for value in lane
        for component in (value.real, value.imag)
    )
    scale = 0.125 / peak
    lanes = [
        component * scale
        for index in range(3840)
        for stream in (0, 1)
        for component in (samples[stream][index].real, samples[stream][index].imag)
    ]
    return {
        "fc32le": struct.pack("<" + "f" * len(lanes), *lanes),
        "sc16le": struct.pack(
            "<" + "h" * len(lanes), *(round(value * 32767) for value in lanes)
        ),
        "packets": packets,
        "sections": sections,
        "amplitude_scale": scale,
    }


def generate(output):
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    payload_cases = [
        (
            "deterministic_0",
            0,
            tuple(
                deterministic_bytes(b"MRF1-PAYLOAD", seed, 0, stream, 128)
                for stream, seed in enumerate((717, 1717))
            ),
        ),
        ("ascending_1", 1, (bytes(range(128)), bytes(range(255, 127, -1)))),
        (
            "deterministic_65535",
            65535,
            tuple(
                deterministic_bytes(b"MRF1-PAYLOAD", seed, 65535, stream, 128)
                for stream, seed in enumerate((717, 1717))
            ),
        ),
    ]
    for name, frame_id, payloads in payload_cases:
        built = frame(payloads, frame_id)
        files = {}
        for kind in ("fc32le", "sc16le"):
            files[name + "." + kind] = built[kind]
        for stream in (0, 1):
            files[f"{name}.stream{stream}.packet.bin"] = built["packets"][stream]
        artifacts = []
        for filename, data in files.items():
            (output / filename).write_bytes(data)
            artifacts.append(
                {
                    "path": filename,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        rows.append(
            {
                "name": name,
                "frame_id": frame_id,
                "payload_hex": [payload.hex() for payload in payloads],
                "header_hex": [data[:20].hex() for data in built["packets"]],
                "sections": built["sections"],
                "amplitude_scale": built["amplitude_scale"],
                "artifacts": artifacts,
            }
        )
    manifest = {
        "schema": "mrf1-air-interface-v1-vectors",
        "profile": PROFILE,
        "iq_layout": "sample-major I0,Q0,I1,Q1; little-endian components",
        "hardware_tested": False,
        "uhd_transmit_executed": False,
        "reference_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "rrc_taps": rrc_taps(),
        "vectors": rows,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def verify(output):
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("schema") != "mrf1-air-interface-v1-vectors"
        or manifest["profile"] != PROFILE
    ):
        raise ValueError("profile differs from reference")
    if (
        manifest["reference_sha256"]
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise ValueError("reference recipe changed; regenerate and review vectors")
    expected_names = ("deterministic_0", "ascending_1", "deterministic_65535")
    if (
        manifest.get("rrc_taps") != rrc_taps()
        or manifest.get("iq_layout")
        != "sample-major I0,Q0,I1,Q1; little-endian components"
        or manifest.get("hardware_tested") is not False
        or manifest.get("uhd_transmit_executed") is not False
    ):
        raise ValueError("invalid filter/layout or unsupported hardware claims")
    if tuple(row["name"] for row in manifest["vectors"]) != expected_names:
        raise ValueError("missing, duplicate or unexpected mandatory vectors")
    for row in manifest["vectors"]:
        payloads = tuple(bytes.fromhex(value) for value in row["payload_hex"])
        built = frame(payloads, row["frame_id"])
        expected_paths = {
            row["name"] + suffix
            for suffix in (
                ".fc32le",
                ".sc16le",
                ".stream0.packet.bin",
                ".stream1.packet.bin",
            )
        }
        if (
            len(row["artifacts"]) != 4
            or {entry["path"] for entry in row["artifacts"]} != expected_paths
            or row["sections"] != built["sections"]
            or row.get("amplitude_scale") != built["amplitude_scale"]
            or row["header_hex"] != [data[:20].hex() for data in built["packets"]]
        ):
            raise ValueError("incomplete or inconsistent vector metadata")
        for artifact in row["artifacts"]:
            name = artifact["path"]
            if Path(name).name != name:
                raise ValueError("unsafe vector path")
            data = (output / name).read_bytes()
            if (
                len(data) != artifact["bytes"]
                or hashlib.sha256(data).hexdigest() != artifact["sha256"]
            ):
                raise ValueError("vector hash/size mismatch: " + name)
            if name.endswith(".packet.bin"):
                stream = 0 if ".stream0." in name else 1
                if data != built["packets"][stream] or parse_packet(data, stream) != (
                    row["frame_id"],
                    payloads[stream],
                ):
                    raise ValueError("packet vector mismatch")
            elif data != built[name.rsplit(".", 1)[1]]:
                raise ValueError("IQ regeneration mismatch")
    print("MRF1_VECTOR_VERIFY_PASS: 3 frames; packet bytes + fc32/sc16 IQ; no hardware")


class ReferenceTests(unittest.TestCase):
    def test_crc_and_bit_order(self):
        self.assertEqual(zlib.crc32(b"123456789"), 0xCBF43926)
        self.assertEqual(indices(b"\x1b"), [0, 1, 2, 3])
        self.assertEqual(POINTS[1], (-1 + 1j) / math.sqrt(2))

    def test_layout_and_crc_rejection(self):
        payloads = (bytes(range(128)), bytes(reversed(range(128))))
        built = frame(payloads, 1)
        self.assertEqual(len(built["fc32le"]), 3840 * 4 * 4)
        self.assertEqual(len(built["sc16le"]), 3840 * 4 * 2)
        for stream in (0, 1):
            self.assertEqual(
                parse_packet(built["packets"][stream], stream), (1, payloads[stream])
            )
            broken = bytearray(built["packets"][stream])
            broken[20] ^= 1
            with self.assertRaisesRegex(ValueError, "payload CRC"):
                parse_packet(bytes(broken), stream)
        self.assertEqual(sum(value["symbol_count"] for value in built["sections"]), 950)

    def test_training_and_pilots_are_time_orthogonal(self):
        symbols, _, sections = frame_symbols((bytes(128), bytes(128)), 0)
        by_name = {row["name"]: row for row in sections}
        for name, muted in (
            ("preamble_tx0", 1),
            ("training_tx0", 1),
            ("training_tx1", 0),
        ):
            row = by_name[name]
            self.assertTrue(
                all(
                    value == 0
                    for value in symbols[muted][
                        row["start_symbol"] : row["start_symbol"] + row["symbol_count"]
                    ]
                )
            )
        for number in range(9):
            start = by_name["pilots_" + str(number)]["start_symbol"]
            self.assertEqual(symbols[1][start], 0)
            self.assertEqual(symbols[0][start + 1], 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "verify", "self-test"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "self-test":
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(ReferenceTests)
        )
        raise SystemExit(0 if result.wasSuccessful() else 1)
    if args.output is None:
        parser.error("--output is required")
    generate(args.output) if args.action == "generate" else verify(args.output)
