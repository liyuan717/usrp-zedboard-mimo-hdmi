"""Verified codec outputs to the existing complete-frame display sink.

Call only after CodecOutputTracker.consume accepted a FRAME record. None
(suppressed retired-epoch output) and status-only records are never displayed.
No sink start/stop or framebuffer open is performed by this adapter.

Decoder flush is an output/callback admission barrier, not a display queue
purge. Complete frames accepted before that barrier may still be displayed;
WAIT_IDR may retain the last good image. A single sink worker and increasing
sequence admission prevent an older frame following a newer accepted frame.
This adapter does not claim a physical scanout or end-to-end flush barrier.
"""
import zlib

from .video_codec_ipc import FRAME, HEIGHT, RGB_BYTES, WIDTH, CodecOutputHeader
from .video_stream import DecodedFrameMetadata, ReceivedFrame


class DecodedFramePresenter:
    def __init__(self, *, session_id, sink):
        if type(session_id) is not int or not 0 <= session_id <= 0xFFFFFFFF:
            raise ValueError("session_id must be uint32")
        self.session_id, self.sink = session_id, sink
        self.high_watermark = -1
        self.submitted = self.stale = 0
        self.failed = False

    def submit_output(self, header, pixels):
        if self.failed:
            raise RuntimeError("presenter failed")
        try:
            if header is None:
                return False
            if type(header) is not CodecOutputHeader:
                raise TypeError("validated codec header required")
            if header.status != FRAME:
                return False
            if header.identity.session_id != self.session_id:
                raise ValueError("decoded frame session mismatch")
            if (header.width, header.height, header.pixel_format, header.payload_bytes) != (
                    WIDTH, HEIGHT, 1, RGB_BYTES):
                raise ValueError("decoded frame geometry mismatch")
            if (type(pixels) is not bytes or len(pixels) != RGB_BYTES or
                    zlib.crc32(pixels) != header.payload_crc):
                raise ValueError("decoded pixel payload mismatch")
            if header.identity.sequence <= self.high_watermark:
                self.stale += 1
                return False
            metadata = DecodedFrameMetadata(
                header.identity.sequence, WIDTH, HEIGHT, header.identity.source_pts,
                "h264-annexb", header.identity.session_id, header.identity.token)
            if not self.sink.submit(ReceivedFrame(metadata, pixels)):
                raise RuntimeError("display sink rejected decoded frame")
            self.high_watermark = header.identity.sequence
            self.submitted += 1
            return True
        except BaseException:
            self.failed = True
            raise
