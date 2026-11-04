"""Low-rate video frame transport for the ZedBoard HDMI demo.

The first milestone deliberately uses only the Python standard library on the
board. Frames are converted to RGB565, compressed with zlib and sent over TCP.
The receiver writes the decoded image to a Linux framebuffer, centered inside
the active screen mode. This is a transport/display bring-up path, not the
eventual RF modem or a hardware video codec.
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

MAGIC = b"ZVID"
VERSION = 1
CODEC_ZLIB_RGB565 = 1
MAX_DIMENSION = 1920
MAX_RAW_BYTES = 4 * 1024 * 1024
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 240
DEFAULT_FPS = 5.0
DEFAULT_PORT = 8022

HEADER = struct.Struct("<4sBBHIHHIIIQ")


class FrameProtocolError(ValueError):
    """Raised when a received frame is malformed or exceeds a safety bound."""


@dataclass(frozen=True)
class FrameHeader:
    magic: bytes
    version: int
    codec: int
    flags: int
    sequence: int
    width: int
    height: int
    raw_size: int
    compressed_size: int
    payload_crc32: int
    timestamp_ms: int

    def encode(self) -> bytes:
        return HEADER.pack(
            self.magic,
            self.version,
            self.codec,
            self.flags,
            self.sequence,
            self.width,
            self.height,
            self.raw_size,
            self.compressed_size,
            self.payload_crc32,
            self.timestamp_ms,
        )

    @classmethod
    def decode(cls, raw: bytes) -> FrameHeader:
        if len(raw) != HEADER.size:
            raise FrameProtocolError(
                f"frame header has {len(raw)} bytes, expected {HEADER.size}"
            )
        header = cls(*HEADER.unpack(raw))
        if header.magic != MAGIC:
            raise FrameProtocolError("invalid frame magic")
        if header.version != VERSION:
            raise FrameProtocolError(f"unsupported frame version {header.version}")
        if header.codec != CODEC_ZLIB_RGB565:
            raise FrameProtocolError(f"unsupported codec {header.codec}")
        if not (0 < header.width <= MAX_DIMENSION and 0 < header.height <= MAX_DIMENSION):
            raise FrameProtocolError(
                f"invalid frame dimensions {header.width}x{header.height}"
            )
        expected_raw = header.width * header.height * 2
        if header.raw_size != expected_raw:
            raise FrameProtocolError(
                f"raw size {header.raw_size} does not match {header.width}x"
                f"{header.height} RGB565 ({expected_raw})"
            )
        if header.raw_size > MAX_RAW_BYTES:
            raise FrameProtocolError("uncompressed frame exceeds the safety limit")
        if not (0 < header.compressed_size <= MAX_COMPRESSED_BYTES):
            raise FrameProtocolError("compressed frame size is outside the safety limit")
        return header


@dataclass(frozen=True)
class DecodedFrameMetadata:
    """Presentation metadata, not a serializable legacy ZVID wire header."""

    sequence: int
    width: int
    height: int
    timestamp_ms: int
    source_codec: str
    session_id: int
    decode_token: int


@dataclass(frozen=True)
class ReceivedFrame:
    header: FrameHeader | DecodedFrameMetadata
    rgb565: bytes


@dataclass(frozen=True)
class FramebufferInfo:
    width: int
    height: int
    bits_per_pixel: int
    stride: int
    size: int


def rgb24_to_rgb565(data: bytes, width: int, height: int) -> bytes:
    """Convert tightly packed RGB888 bytes to little-endian RGB565."""
    expected = width * height * 3
    if len(data) != expected:
        raise ValueError(f"RGB24 payload has {len(data)} bytes, expected {expected}")
    out = bytearray(width * height * 2)
    source = 0
    target = 0
    pixels = width * height
    for _ in range(pixels):
        red = data[source]
        green = data[source + 1]
        blue = data[source + 2]
        value = ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)
        out[target] = value & 0xFF
        out[target + 1] = value >> 8
        source += 3
        target += 2
    return bytes(out)


def encode_frame(
    rgb565: bytes,
    width: int,
    height: int,
    sequence: int,
    timestamp_ms: int = 0,
    compression_level: int = 1,
) -> bytes:
    expected = width * height * 2
    if len(rgb565) != expected:
        raise ValueError(f"RGB565 payload has {len(rgb565)} bytes, expected {expected}")
    compressed = zlib.compress(rgb565, level=compression_level)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise FrameProtocolError("compressed frame exceeds the safety limit")
    header = FrameHeader(
        magic=MAGIC,
        version=VERSION,
        codec=CODEC_ZLIB_RGB565,
        flags=0,
        sequence=sequence,
        width=width,
        height=height,
        raw_size=len(rgb565),
        compressed_size=len(compressed),
        payload_crc32=zlib.crc32(compressed) & 0xFFFFFFFF,
        timestamp_ms=timestamp_ms,
    )
    return header.encode() + compressed


def _reader(stream: BinaryIO | socket.socket):
    recv = getattr(stream, "recv", None)
    if callable(recv):
        return recv
    read = getattr(stream, "read", None)
    if callable(read):
        return read
    raise TypeError("frame stream must provide recv() or read()")


def read_exact(stream: BinaryIO | socket.socket, count: int) -> bytes:
    reader = _reader(stream)
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = reader(remaining)
        if not chunk:
            raise EOFError("frame stream ended before the expected payload was read")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def decode_frame_packet(header_bytes: bytes, payload: bytes) -> ReceivedFrame:
    header = FrameHeader.decode(header_bytes)
    if len(payload) != header.compressed_size:
        raise FrameProtocolError(
            f"compressed payload has {len(payload)} bytes, "
            f"expected {header.compressed_size}"
        )
    actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if actual_crc != header.payload_crc32:
        raise FrameProtocolError(
            f"payload CRC mismatch: received {actual_crc:08x}, "
            f"expected {header.payload_crc32:08x}"
        )
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(payload, header.raw_size + 1)
        if decompressor.unconsumed_tail:
            raise FrameProtocolError("compressed payload expands beyond its declared size")
        raw += decompressor.flush()
    except zlib.error as exc:
        raise FrameProtocolError(f"invalid zlib payload: {exc}") from exc
    if not decompressor.eof:
        raise FrameProtocolError("truncated zlib payload")
    if len(raw) != header.raw_size:
        raise FrameProtocolError(
            f"decompressed payload has {len(raw)} bytes, expected {header.raw_size}"
        )
    return ReceivedFrame(header=header, rgb565=raw)


def receive_frame(stream: BinaryIO | socket.socket) -> ReceivedFrame:
    header_bytes = read_exact(stream, HEADER.size)
    header = FrameHeader.decode(header_bytes)
    payload = read_exact(stream, header.compressed_size)
    return decode_frame_packet(header_bytes, payload)


def send_frame(stream: socket.socket, packet: bytes) -> None:
    if hasattr(stream, "sendall"):
        stream.sendall(packet)
    else:
        written = stream.write(packet)
        if written != len(packet):
            raise OSError(f"short frame write: {written}/{len(packet)}")


def read_framebuffer_info(
    framebuffer: Path = Path("/dev/fb0"),
    sysfs: Path | None = None,
) -> FramebufferInfo:
    sysfs_path = sysfs or Path("/sys/class/graphics") / framebuffer.name
    try:
        virtual_size = (sysfs_path / "virtual_size").read_text().strip()
        width_text, height_text = virtual_size.split(",", maxsplit=1)
        width = int(width_text)
        height = int(height_text)
        bits_per_pixel = int((sysfs_path / "bits_per_pixel").read_text().strip())
        stride_path = sysfs_path / "stride"
        stride = (
            int(stride_path.read_text().strip())
            if stride_path.exists()
            else width * bits_per_pixel // 8
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"cannot read framebuffer geometry from {sysfs_path}: {exc}"
        ) from exc
    if bits_per_pixel not in (16, 24, 32):
        raise RuntimeError(
            f"unsupported framebuffer depth {bits_per_pixel}; supported: 16/24/32 bpp"
        )
    computed_size = stride * height
    try:
        file_size = framebuffer.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"cannot stat framebuffer {framebuffer}: {exc}") from exc
    size = max(computed_size, file_size)
    if width <= 0 or height <= 0 or stride < width * bits_per_pixel // 8:
        raise RuntimeError(
            f"invalid framebuffer geometry {width}x{height}, stride={stride}, "
            f"bpp={bits_per_pixel}"
        )
    return FramebufferInfo(width, height, bits_per_pixel, stride, size)


def _rgb565_row_to_rgb32(row: bytes) -> bytes:
    out = bytearray(len(row) * 2)
    target = 0
    for source in range(0, len(row), 2):
        value = row[source] | (row[source + 1] << 8)
        red5 = (value >> 11) & 0x1F
        green6 = (value >> 5) & 0x3F
        blue5 = value & 0x1F
        out[target] = (blue5 << 3) | (blue5 >> 2)
        out[target + 1] = (green6 << 2) | (green6 >> 4)
        out[target + 2] = (red5 << 3) | (red5 >> 2)
        out[target + 3] = 0
        target += 4
    return bytes(out)


def _rgb565_row_to_rgb24(row: bytes) -> bytes:
    out = bytearray(len(row) // 2 * 3)
    target = 0
    for source in range(0, len(row), 2):
        value = row[source] | (row[source + 1] << 8)
        red5 = (value >> 11) & 0x1F
        green6 = (value >> 5) & 0x3F
        blue5 = value & 0x1F
        out[target] = (blue5 << 3) | (blue5 >> 2)
        out[target + 1] = (green6 << 2) | (green6 >> 4)
        out[target + 2] = (red5 << 3) | (red5 >> 2)
        target += 3
    return bytes(out)


def _blit_rgb565_numpy(
    target,
    info: FramebufferInfo,
    rgb565: bytes,
    width: int,
    height: int,
    x0: int,
    y0: int,
) -> bool:
    """Use NumPy's vectorized conversion when it is available on the board."""
    try:
        import numpy as np
    except ImportError:
        return False

    bytes_per_pixel = info.bits_per_pixel // 8
    pixels = np.frombuffer(rgb565, dtype="<u2").reshape(height, width)
    if info.bits_per_pixel == 16:
        encoded = np.frombuffer(rgb565, dtype=np.uint8).reshape(height, width, 2)
    else:
        red5 = (pixels >> 11) & 0x1F
        green6 = (pixels >> 5) & 0x3F
        blue5 = pixels & 0x1F
        encoded = np.empty((height, width, bytes_per_pixel), dtype=np.uint8)
        encoded[..., 0] = (blue5 << 3) | (blue5 >> 2)
        encoded[..., 1] = (green6 << 2) | (green6 >> 4)
        encoded[..., 2] = (red5 << 3) | (red5 >> 2)
        if bytes_per_pixel == 4:
            encoded[..., 3] = 0

    destination = np.ndarray(
        shape=(height, width, bytes_per_pixel),
        dtype=np.uint8,
        buffer=target,
        offset=y0 * info.stride + x0 * bytes_per_pixel,
        strides=(info.stride, bytes_per_pixel, 1),
    )
    destination[...] = encoded
    return True


def blit_rgb565(
    target,
    info: FramebufferInfo,
    rgb565: bytes,
    width: int,
    height: int,
    *,
    center: bool = True,
) -> tuple[int, int]:
    """Copy an RGB565 image into a framebuffer and return its top-left offset."""
    expected = width * height * 2
    if len(rgb565) != expected:
        raise ValueError(f"RGB565 image has {len(rgb565)} bytes, expected {expected}")
    if len(target) < info.size:
        raise ValueError(f"framebuffer target has {len(target)} bytes, expected {info.size}")
    if width > info.width or height > info.height:
        raise ValueError(
            f"image {width}x{height} does not fit framebuffer {info.width}x{info.height}"
    )
    x0 = (info.width - width) // 2 if center else 0
    y0 = (info.height - height) // 2 if center else 0
    if _blit_rgb565_numpy(target, info, rgb565, width, height, x0, y0):
        return x0, y0
    for row_index in range(height):
        source_start = row_index * width * 2
        row = rgb565[source_start : source_start + width * 2]
        if info.bits_per_pixel == 16:
            encoded = row
        elif info.bits_per_pixel == 24:
            encoded = _rgb565_row_to_rgb24(row)
        else:
            encoded = _rgb565_row_to_rgb32(row)
        destination = (y0 + row_index) * info.stride + x0 * (info.bits_per_pixel // 8)
        target[destination : destination + len(encoded)] = encoded
    return x0, y0


def blit_rgb565_at(
    target,
    info: FramebufferInfo,
    rgb565: bytes,
    width: int,
    height: int,
    x0: int,
    y0: int,
) -> None:
    """Copy an RGB565 image to an explicit framebuffer position."""
    expected = width * height * 2
    if len(rgb565) != expected:
        raise ValueError(f"RGB565 image has {len(rgb565)} bytes, expected {expected}")
    if x0 < 0 or y0 < 0 or x0 + width > info.width or y0 + height > info.height:
        raise ValueError("RGB565 patch lies outside framebuffer")
    if _blit_rgb565_numpy(target, info, rgb565, width, height, x0, y0):
        return
    for row_index in range(height):
        source_start = row_index * width * 2
        row = rgb565[source_start : source_start + width * 2]
        if info.bits_per_pixel == 16:
            encoded = row
        elif info.bits_per_pixel == 24:
            encoded = _rgb565_row_to_rgb24(row)
        else:
            encoded = _rgb565_row_to_rgb32(row)
        destination = (y0 + row_index) * info.stride + x0 * (info.bits_per_pixel // 8)
        target[destination : destination + len(encoded)] = encoded


class Framebuffer:
    """Small no-dependency Linux framebuffer writer used by the board demo."""

    def __init__(self, path: Path = Path("/dev/fb0"), sysfs: Path | None = None):
        self.path = path
        self.info = read_framebuffer_info(path, sysfs)
        self._fd = os.open(path, os.O_RDWR)
        try:
            self._memory = mmap.mmap(
                self._fd, self.info.size, access=mmap.ACCESS_WRITE
            )
        except Exception:
            os.close(self._fd)
            raise

    def flush_rectangle(self, x: int, y: int, width: int, height: int) -> None:
        bytes_per_pixel = self.info.bits_per_pixel // 8
        dirty_start = y * self.info.stride + x * bytes_per_pixel
        dirty_end = (
            (y + height - 1) * self.info.stride
            + (x + width) * bytes_per_pixel
        )
        # Flush only the pages touched by this frame. Flushing the entire
        # 1024x600 framebuffer made a 320x240 update take about 900 ms on the
        # ZedBoard even though only 77k pixels changed.
        page_size = mmap.PAGESIZE
        flush_start = dirty_start - dirty_start % page_size
        flush_end = (dirty_end + page_size - 1) // page_size * page_size
        flush_end = min(self.info.size, flush_end)
        self._memory.flush(flush_start, flush_end - flush_start)

    def display_rgb565(self, rgb565: bytes, width: int, height: int) -> tuple[int, int]:
        x, y = blit_rgb565(
            self._memory, self.info, rgb565, width, height, center=True
        )
        self.flush_rectangle(x, y, width, height)
        return x, y

    def display_rgb565_patch(
        self,
        rgb565: bytes,
        width: int,
        height: int,
        x: int,
        y: int,
        *,
        flush: bool = True,
    ) -> None:
        blit_rgb565_at(
            self._memory, self.info, rgb565, width, height, x, y
        )
        if flush:
            self.flush_rectangle(x, y, width, height)

    def clear(self) -> None:
        self._memory[:] = b"\0" * self.info.size
        self._memory.flush()

    def close(self) -> None:
        if hasattr(self, "_memory"):
            self._memory.close()
            del self._memory
        if hasattr(self, "_fd"):
            os.close(self._fd)
            del self._fd

    def __enter__(self) -> Framebuffer:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def test_pattern_rgb565(
    width: int,
    height: int,
    frame_index: int,
    *,
    marker_visible: bool = True,
) -> bytes:
    """Generate an RGB565 color-bar pattern with a moving vertical marker."""
    if width <= 0 or height <= 0:
        raise ValueError("test-pattern dimensions must be positive")
    out = bytearray(width * height * 2)
    marker_x = (frame_index * 7) % width
    bars = (
        (0xF800, "red"),
        (0x07E0, "green"),
        (0x001F, "blue"),
        (0xFFE0, "yellow"),
        (0x07FF, "cyan"),
        (0xF81F, "magenta"),
    )
    colors = [item[0] for item in bars]
    marker = 0xFFFF
    target = 0
    for y in range(height):
        for x in range(width):
            if marker_visible and abs(x - marker_x) <= 3:
                value = marker
            elif y < height // 6:
                value = colors[min(len(colors) - 1, x * len(colors) // width)]
            else:
                red5 = x * 31 // max(1, width - 1)
                green6 = y * 63 // max(1, height - 1)
                blue5 = frame_index * 5 % 32
                value = (red5 << 11) | (green6 << 5) | blue5
            out[target] = value & 0xFF
            out[target + 1] = value >> 8
            target += 2
    return bytes(out)


def rgb565_rectangle(
    source: bytes,
    source_width: int,
    x: int,
    y: int,
    width: int,
    height: int,
) -> bytes:
    """Extract a tightly packed RGB565 rectangle."""
    source_height = len(source) // (source_width * 2)
    if (
        len(source) != source_width * source_height * 2
        or x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or x + width > source_width
        or y + height > source_height
    ):
        raise ValueError("invalid RGB565 source rectangle")
    row_bytes = width * 2
    return b"".join(
        source[
            ((y + row) * source_width + x) * 2:
            ((y + row) * source_width + x) * 2 + row_bytes
        ]
        for row in range(height)
    )


def iter_test_frames(width: int, height: int, fps: float) -> Iterator[tuple[bytes, int]]:
    index = 0
    start = time.monotonic()
    interval = 1.0 / fps
    while True:
        deadline = start + index * interval
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        yield test_pattern_rgb565(width, height, index), int(index * 1000 / fps)
        index += 1


def _ffmpeg_command(
    video: Path,
    width: int,
    height: int,
    fps: float,
) -> list[str]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to read a video file on the PC")
    video_filter = (
        f"fps={fps},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    return [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(video),
        "-an",
        "-sn",
        "-dn",
        "-vf",
        video_filter,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]


def iter_video_frames(
    video: Path,
    width: int,
    height: int,
    fps: float,
) -> Iterator[tuple[bytes, int]]:
    command = _ffmpeg_command(video, width, height, fps)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("failed to open ffmpeg pipes")
    frame_bytes = width * height * 3
    frame_index = 0
    start = time.monotonic()
    try:
        while True:
            try:
                rgb24 = read_exact(process.stdout, frame_bytes)
            except EOFError:
                break
            deadline = start + frame_index / fps
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            yield (
                rgb24_to_rgb565(rgb24, width, height),
                int(frame_index * 1000 / fps),
            )
            frame_index += 1
    finally:
        process.stdout.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
        process.stderr.close()
        return_code = process.wait()
    if return_code != 0 and frame_index == 0:
        raise RuntimeError(f"ffmpeg failed ({return_code}): {stderr}")


def run_sender(args: argparse.Namespace) -> int:
    if args.video is None and not args.test_pattern:
        raise ValueError("choose --video or --test-pattern")
    if args.video is not None and args.test_pattern:
        raise ValueError("--video and --test-pattern are mutually exclusive")
    if not (0 < args.fps <= 60):
        raise ValueError("--fps must be between 0 and 60")
    if args.video is not None and not args.video.is_file():
        raise ValueError(f"video file does not exist: {args.video}")

    with socket.create_connection((args.host, args.port), timeout=args.timeout) as sock:
        sock.settimeout(args.timeout)
        sequence = 0
        sent = 0
        repetitions = 1 if args.test_pattern else args.repeat
        start = time.monotonic()
        for repetition in range(repetitions):
            frames = (
                iter_test_frames(args.width, args.height, args.fps)
                if args.test_pattern
                else iter_video_frames(args.video, args.width, args.height, args.fps)
            )
            for rgb565, timestamp_ms in frames:
                packet = encode_frame(
                    rgb565,
                    args.width,
                    args.height,
                    sequence,
                    timestamp_ms,
                    args.compression_level,
                )
                send_frame(sock, packet)
                sequence += 1
                sent += 1
                if args.max_frames and sent >= args.max_frames:
                    break
                if args.test_pattern and args.max_frames == 0 and sent >= 50:
                    break
            if args.max_frames and sent >= args.max_frames:
                break
            if args.test_pattern and args.max_frames == 0:
                break
            if args.repeat > 1:
                print(
                    f"completed video repetition {repetition + 1}/{args.repeat}",
                    flush=True,
                )
        elapsed = max(time.monotonic() - start, 1e-9)
        print(
            json.dumps(
                {
                    "event": "sender_complete",
                    "frames": sent,
                    "elapsed_s": round(elapsed, 3),
                    "fps": round(sent / elapsed, 3),
                    "host": args.host,
                    "port": args.port,
                }
            ),
            flush=True,
        )
    return 0


def run_receiver(args: argparse.Namespace) -> int:
    framebuffer = Framebuffer(args.fb, args.fb_sysfs)
    dump_dir: Path | None = args.dump_dir
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
    print(
        json.dumps(
            {
                "event": "display_ready",
                "fb": str(args.fb),
                "geometry": asdict(framebuffer.info),
                "host": args.host,
                "port": args.port,
            }
        ),
        flush=True,
    )
    received = 0
    with framebuffer, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        if args.timeout > 0:
            server.settimeout(args.timeout)
        while True:
            try:
                connection, address = server.accept()
            except socket.timeout:
                print(
                    json.dumps(
                        {
                            "event": "listen_timeout",
                            "timeout_s": args.timeout,
                        }
                    ),
                    flush=True,
                )
                return 2
            with connection:
                if args.timeout > 0:
                    connection.settimeout(args.timeout)
                print(
                    json.dumps(
                        {
                            "event": "client_connected",
                            "address": address[0],
                            "port": address[1],
                        }
                    ),
                    flush=True,
                )
                try:
                    while True:
                        frame_started = time.monotonic()
                        received_frame = receive_frame(connection)
                        header = received_frame.header
                        x, y = framebuffer.display_rgb565(
                            received_frame.rgb565, header.width, header.height
                        )
                        elapsed_ms = (time.monotonic() - frame_started) * 1000
                        received += 1
                        if dump_dir is not None:
                            name = f"frame-{header.sequence:08d}.rgb565"
                            (dump_dir / name).write_bytes(received_frame.rgb565)
                        print(
                            json.dumps(
                                {
                                    "event": "frame_displayed",
                                    "sequence": header.sequence,
                                    "width": header.width,
                                    "height": header.height,
                                    "compressed_bytes": header.compressed_size,
                                    "raw_bytes": header.raw_size,
                                    "offset": [x, y],
                                    "elapsed_ms": round(elapsed_ms, 3),
                                }
                            ),
                            flush=True,
                        )
                        if args.max_frames and received >= args.max_frames:
                            return 0
                except EOFError:
                    print(json.dumps({"event": "client_disconnected"}), flush=True)
                    if args.once:
                        return 0
                except socket.timeout:
                    print(
                        json.dumps(
                            {
                                "event": "receive_timeout",
                                "timeout_s": args.timeout,
                            }
                        ),
                        flush=True,
                    )
                    return 2


def run_self_test(args: argparse.Namespace) -> int:
    with Framebuffer(args.fb, args.fb_sysfs) as framebuffer:
        framebuffer.clear()
        interval = 1.0 / args.fps
        for index in range(args.frames):
            frame = test_pattern_rgb565(args.width, args.height, index)
            x, y = framebuffer.display_rgb565(frame, args.width, args.height)
            print(
                json.dumps(
                    {
                        "event": "self_test_frame",
                        "index": index,
                        "offset": [x, y],
                        "geometry": asdict(framebuffer.info),
                    }
                ),
                flush=True,
            )
            time.sleep(interval)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ZedBoard HDMI video bring-up: PC sender and board receiver"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sender = subparsers.add_parser("send", help="send video frames to the board")
    sender.add_argument("--host", default="127.0.0.1")
    sender.add_argument("--port", type=int, default=DEFAULT_PORT)
    sender.add_argument("--video", type=Path)
    sender.add_argument("--test-pattern", action="store_true")
    sender.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    sender.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    sender.add_argument("--fps", type=float, default=DEFAULT_FPS)
    sender.add_argument("--repeat", type=int, default=1)
    sender.add_argument("--max-frames", type=int, default=0)
    sender.add_argument("--compression-level", type=int, choices=range(1, 10), default=1)
    sender.add_argument("--timeout", type=float, default=10.0)

    receiver = subparsers.add_parser("receive", help="receive frames and write /dev/fb0")
    receiver.add_argument("--host", default="127.0.0.1")
    receiver.add_argument("--port", type=int, default=DEFAULT_PORT)
    receiver.add_argument("--fb", type=Path, default=Path("/dev/fb0"))
    receiver.add_argument("--fb-sysfs", type=Path)
    receiver.add_argument("--max-frames", type=int, default=0)
    receiver.add_argument("--dump-dir", type=Path)
    receiver.add_argument(
        "--once",
        action="store_true",
        help="return after the first sender disconnects",
    )
    receiver.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="seconds to wait for a sender or the next complete frame",
    )

    self_test = subparsers.add_parser(
        "self-test", help="display a moving HDMI test pattern without networking"
    )
    self_test.add_argument("--fb", type=Path, default=Path("/dev/fb0"))
    self_test.add_argument("--fb-sysfs", type=Path)
    self_test.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    self_test.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    self_test.add_argument("--fps", type=float, default=DEFAULT_FPS)
    self_test.add_argument("--frames", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "send":
            return run_sender(args)
        if args.command == "receive":
            return run_receiver(args)
        if args.command == "self-test":
            return run_self_test(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"video-demo: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unsupported command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
