#!/usr/bin/env python3
"""Read the two G7 picture markers from an independent HDMI recording."""
import argparse
from fractions import Fraction
from itertools import groupby
import json
from pathlib import Path
import subprocess

WIDTH = 192
HEIGHT = 32
FRAME_BYTES = WIDTH * HEIGHT
SOURCE_FRAMES = 200
MAX_CAPTURE_FRAMES = 18000


def marker(frame):
    cells = [
        [frame[(row * 16 + 8) * WIDTH + column * 16 + 8] for column in range(12)]
        for row in range(2)
    ]
    low = max(cells[0][0], cells[1][1])
    high = min(cells[0][1], cells[1][0])
    if high - low < 80:
        return None
    threshold = (low + high) // 2
    top, bottom = [[value > threshold for value in row] for row in cells]
    if top[:2] != [False, True] or any(a == b for a, b in zip(top, bottom)) or sum(top[2:]) % 2:
        return None
    source_id = 0
    for bit in top[3:11]:
        source_id = (source_id << 1) | bit
    return ("B" if top[2] else "A", source_id) if source_id < SOURCE_FRAMES else None


def capture_timestamps(recording, time_base, frame_count):
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp", "-of", "compact=p=0", str(recording),
    ]
    ticks = []
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        for line in process.stdout:
            if not line.startswith("best_effort_timestamp="):
                continue
            value = line.partition("|")[0].partition("=")[2].strip()
            if value == "N/A" or len(ticks) >= frame_count:
                process.kill()
                raise ValueError("Recording timestamps do not match decoded capture frames")
            tick = int(value)
            if ticks and tick <= ticks[-1]:
                process.kill()
                raise ValueError("Recording timestamps must increase in presentation order")
            ticks.append(tick)
        if process.wait() or len(ticks) != frame_count:
            raise ValueError("Recording timestamps do not match decoded capture frames")
    return [float((tick - ticks[0]) * time_base) for tick in ticks]


def picture_runs(markers, times):
    repeats, unrecognized = [], []
    start = 0
    for found, frames in groupby(markers):
        end = start + sum(1 for _ in frames)
        if found is None or end - start > 1:
            row = {
                "first_capture_frame": start, "last_capture_frame": end - 1,
                "first_capture_s": times[start], "last_capture_s": times[end - 1],
                "next_capture_s": times[end] if end < len(times) else None,
                "observed_span_s": round(times[end - 1] - times[start], 9),
            }
            if found is None:
                unrecognized.append(row)
            else:
                row["burst"], row["source_frame"] = found
                repeats.append(row)
        start = end
    return repeats, unrecognized


def review(recording, box):
    x, y, width, height = box
    if not recording.is_file():
        raise ValueError("Expected an HDMI recording file")
    if min(x, y) < 0 or min(width, height) <= 0:
        raise ValueError("Marker box requires nonnegative offsets and positive dimensions")
    stream_probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,time_base",
        "-of", "json", str(recording),
    ], text=True)
    stream, = json.loads(stream_probe)["streams"]
    input_width, input_height = stream["width"], stream["height"]
    time_base = Fraction(stream["time_base"])
    if x + width > input_width or y + height > input_height:
        raise ValueError("Marker box exceeds the recording dimensions")
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
        "-threads", "2", "-noautorotate", "-i", str(recording), "-map", "0:v:0", "-an",
        "-filter_threads", "1", "-vf", f"format=gray,crop={width}:{height}:{x}:{y}:exact=1,"
        f"scale={WIDTH}:{HEIGHT}:flags=neighbor",
        "-fps_mode", "passthrough", "-frames:v", str(MAX_CAPTURE_FRAMES + 1),
        "-f", "rawvideo", "pipe:1",
    ]
    occurrences = []
    markers = []
    capture_frames = 0
    unrecognized = 0
    repeated = 0
    previous = None
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        while frame := process.stdout.read(FRAME_BYTES):
            capture_frames += 1
            if len(frame) != FRAME_BYTES or capture_frames > MAX_CAPTURE_FRAMES:
                process.kill()
                raise ValueError("Incomplete capture frame or recording exceeds review limit")
            found = marker(frame)
            markers.append(found)
            if found is None:
                unrecognized += 1
            elif found == previous:
                repeated += 1
            else:
                burst, source_id = found
                occurrences.append({
                    "capture_frame": capture_frames - 1, "burst": burst, "source_frame": source_id,
                })
            previous = found
        if process.wait() or not capture_frames:
            raise ValueError("HDMI recording could not be decoded")
    times = capture_timestamps(recording, time_base, capture_frames)
    for row in occurrences:
        row["capture_s"] = times[row["capture_frame"]]
    repeat_runs, unrecognized_runs = picture_runs(markers, times)
    missing = {}
    for burst in ("A", "B"):
        seen = {row["source_frame"] for row in occurrences if row["burst"] == burst}
        missing[burst] = sorted(set(range(SOURCE_FRAMES)) - seen)
    return {
        "capture_frames": capture_frames, "marker_box": box, "marker_occurrences": occurrences,
        "unrecognized_capture_frames": unrecognized, "repeated_capture_frames": repeated,
        "missing_source_frames_by_burst": missing,
        "timing_basis": "FFprobe best-effort presentation timestamps, normalized to the first decoded frame.",
        "max_capture_interval_s": round(max((b - a for a, b in zip(times, times[1:])), default=0), 9),
        "repeated_marker_runs": repeat_runs, "unrecognized_marker_runs": unrecognized_runs,
        "scope": "Picture marker evidence at observed capture times. Verify the physical cold boot, cabling, power and RF events separately.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--box", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"),
                        default=(32, 32, WIDTH, HEIGHT))
    args = parser.parse_args()
    print(json.dumps(review(args.recording.resolve(strict=True), args.box), indent=2))
