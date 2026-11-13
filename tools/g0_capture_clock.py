"""Bound a V4L2 MONOTONIC microsecond timestamp in the host RAW clock.

Ordered observations enclose the capture time using monotonicity. The bridge
has no assumed frequency or constant offset between the two host clocks.
"""
from collections import deque
from math import ceil
import time
from typing import NamedTuple


class CaptureInterval(NamedTuple):
    lower_ns: int
    upper_ns: int
    before: tuple
    after: tuple


class CaptureClockBridge:
    capacity = 256

    def __init__(self, raw_resolution_ns=None):
        self.observations = deque(maxlen=self.capacity)
        self.raw_resolution_ns = (max(1, ceil(time.clock_getres(time.CLOCK_MONOTONIC_RAW)*1e9))
                                  if raw_resolution_ns is None else raw_resolution_ns)
        if type(self.raw_resolution_ns) is not int or self.raw_resolution_ns <= 0:
            raise ValueError('RAW clock resolution')

    def observe(self, mono_before_ns, raw_ns, mono_after_ns):
        if not (all(type(value) is int for value in (mono_before_ns, raw_ns, mono_after_ns))
                and 0 < mono_before_ns <= mono_after_ns and raw_ns > 0):
            raise ValueError('clock observation boundary')
        if self.observations:
            _, previous_raw, previous_after = self.observations[-1]
            if mono_before_ns < previous_after or raw_ns < previous_raw:
                raise ValueError('clock observation order')
        self.observations.append((mono_before_ns, raw_ns, mono_after_ns))

    def sample(self):
        before = time.monotonic_ns()
        raw = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
        after = time.monotonic_ns()
        self.observe(before, raw, after)

    def capture_interval(self, monotonic_us):
        if type(monotonic_us) is not int or monotonic_us <= 0:
            raise ValueError('original V4L2 timestamp')
        # FFmpeg's input time base is 1/1,000,000. Include the complete
        # microsecond represented by the original integer timestamp.
        start = monotonic_us*1000
        end = start+999
        lower = None
        for observation in self.observations:
            before, raw, after = observation
            if after < start:
                lower = observation
            if before > end:
                return (CaptureInterval(lower[1]-self.raw_resolution_ns,
                                        raw+self.raw_resolution_ns, lower, observation)
                        if lower is not None else None)
        return None
