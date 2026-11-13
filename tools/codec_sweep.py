#!/usr/bin/env python3
"""G0 offline measurements; never marks native/RF/subjective gates passed."""
import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import time


def run(argv, log, commands):
    commands.append(argv)
    started = time.monotonic()
    with log.open("w") as stream:
        subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, check=True)
    return time.monotonic() - started


def probe(path, packets=False):
    argv = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams"]
    if packets:
        argv += ["-show_packets"]
    return json.loads(subprocess.check_output(argv + ["-of", "json", str(path)]))


def distribution(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {"count": len(values), "mean": sum(values) / len(values),
            "p95": ordered[math.ceil(.95 * len(values)) - 1],
            "p99": ordered[math.ceil(.99 * len(values)) - 1], "max": ordered[-1]}


def annexb_units(data):
    starts = list(re.finditer(b"\x00\x00\x00\x01|\x00\x00\x01", data))
    if not starts or starts[0].start() != 0:
        raise ValueError("Annex-B stream must start with a start code")
    result = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(data)
        if m.end() >= end:
            raise ValueError("empty or truncated Annex-B NAL")
        result.append((data[m.end()] & 31, data[m.start():end]))
    return result


def access_units(units):
    result, current = [], []
    for kind, raw in units:
        if kind == 9 and any(t in (1, 5) for t, _ in current):
            result.append(current)
            current = []
        current.append((kind, raw))
    if current:
        result.append(current)
    if any(not any(t in (1, 5) for t, _ in au) for au in result):
        raise ValueError("AUD-delimited access unit has no VCL NAL")
    return result


def measure_stream(raw, symbol_bytes=1024):
    if symbol_bytes <= 40:
        raise ValueError("FEC symbol must fit a video header and payload")
    aus = access_units(annexb_units(raw))
    sizes = [sum(len(b) for _, b in au) for au in aus]
    slices = [len(b) for au in aus for t, b in au if t in (1, 5)]
    fragments = [sum(math.ceil(len(b) / (symbol_bytes - 40)) for _, b in au) for au in aus]
    windows = {}
    for count in (1, 2, 5, 10, 20, 60):
        if count <= len(sizes):
            windows[str(count * 50) + "ms"] = max(
                sum(sizes[i:i + count]) * 8 * 20 / count
                for i in range(len(sizes) - count + 1))
    duration = len(aus) / 20
    return {"au_bytes": distribution(sizes), "slice_bytes": distribution(slices),
            "slices_per_au": distribution([sum(t in (1, 5) for t, _ in a) for a in aus]),
            "idr_au_count": sum(any(t == 5 for t, _ in a) for a in aus),
            "annexb_bitrate_bps": len(raw) * 8 / duration,
            "peak_sliding_bitrate_bps": windows,
            "source_symbol_bytes": symbol_bytes, "source_symbols": sum(fragments),
            "source_symbol_fill_ratio": len(raw) / (sum(fragments) * (symbol_bytes - 40)),
            "link_bitrate_rs24_32_bps": sum(fragments) * (symbol_bytes + 36) * 8 * 32 / 24 / duration,
            "per_au": [{"capture_id": i, "capture_time_us": i * 50000,
                        "bytes": sizes[i], "source_symbols": fragments[i],
                        "idr": any(t == 5 for t, _ in au)} for i, au in enumerate(aus)]}


def normalize(source_index, end=None):
    # Both selected sources are HD; explicitly map their HD matrix to BT.601.
    trim = "trim=duration=" + str(end or 10)
    return (f"[{source_index}:v]{trim},setpts=PTS-STARTPTS,fps=20,"
            "scale=1024:576:flags=lanczos:in_color_matrix=bt709:out_color_matrix=bt601:out_range=tv,"
            "pad=1024:600:0:12:black,setsar=1,format=yuv420p")


def sweep(source, cut_source, output, slice_count):
    if not 2 <= slice_count <= 32:
        raise ValueError("G0 slice comparison requires 2..32 slices")
    output.mkdir(parents=True, exist_ok=False)
    commands = []
    inputs = {"source": probe(source), "cut_source": probe(cut_source)}
    for label, metadata in inputs.items():
        stream = metadata["streams"][0]
        num, den = map(int, stream["r_frame_rate"].split("/"))
        if num / den < 20:
            raise ValueError(label + " is below 20fps; cannot invent new source frames")
    summary = {"status": "offline_measurements_only", "sources":
               {"normal": str(source.resolve()), "cut": str(cut_source.resolve())},
               "inputs": inputs, "candidates": [], "native_arm_passed": False,
               "subjective_quality_passed": False, "g0_passed": False}
    try:
        for scene in ("normal", "cut"):
            reference = output / (scene + "-reference.mkv")
            inputs_args = ["-i", str(source)]
            if scene == "normal":
                graph = normalize(0) + "[v]"
            else:
                inputs_args += ["-i", str(cut_source)]
                graph = normalize(0, 5) + "[a];" + normalize(1, 5) + "[b];[a][b]concat=n=2:v=1:a=0[v]"
            run(["ffmpeg", "-y", "-v", "warning", *inputs_args,
                 "-filter_complex", graph, "-map", "[v]", "-frames:v", "200",
                 "-an", "-c:v", "ffv1", "-threads", "2", str(reference)],
                output / (scene + "-normalize.log"), commands)
            for mode in ("gop", "refresh"):
                for rate in (600000, 900000, 1200000):
                    name = f"{scene}-{mode}-{rate}"
                    directory = output / name
                    directory.mkdir()
                    mp4, h264 = directory / "video.mp4", directory / "video.h264"
                    x264 = (f"slices={slice_count}:sliced-threads=0:scenecut=0:open-gop=0:weightp=0:8x8dct=0:"
                            "aud=1:repeat-headers=1:intra-refresh=" + str(int(mode == "refresh")))
                    seconds = run(["ffmpeg", "-y", "-v", "info", "-i", str(reference),
                        "-map", "0:v:0", "-an", "-map_metadata", "-1", "-map_chapters", "-1",
                        "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
                        "-preset", "veryfast", "-tune", "zerolatency", "-threads", "2",
                        "-pix_fmt", "yuv420p", "-b:v", str(rate), "-maxrate", str(rate),
                        "-bufsize", str(rate), "-bf", "0", "-refs", "1", "-g", "20",
                        "-keyint_min", "20", "-color_primaries", "bt470bg", "-color_trc", "bt709",
                        "-colorspace", "smpte170m", "-color_range", "tv", "-x264-params", x264,
                        "-movflags", "+faststart", str(mp4)], directory / "encode.log", commands)
                    run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-map", "0:v:0",
                         "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(h264)],
                        directory / "extract.log", commands)
                    run(["ffmpeg", "-v", "error", "-xerror", "-i", str(mp4), "-f", "null", "-"],
                        directory / "full-decode.log", commands)
                    run(["ffmpeg", "-y", "-v", "info", "-i", str(mp4), "-i", str(reference),
                         "-filter_complex", "[0:v][1:v]ssim=stats_file=" + str(directory / "ssim-frames.log"),
                         "-an", "-f", "null", "-"], directory / "ssim.log", commands)
                    run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4),
                         "-vf", "fps=1,scale=256:150,tile=5x2", "-frames:v", "1",
                         "-update", "1", str(directory / "contact.png")], directory / "contact.log", commands)
                    metadata = probe(mp4, packets=True)
                    stream = metadata["streams"][0]
                    if (stream["width"], stream["height"], stream["r_frame_rate"],
                        stream["has_b_frames"], stream["nb_frames"], stream["pix_fmt"]) != (
                            1024, 600, "20/1", 0, "200", "yuv420p"):
                        raise ValueError(name + ": encoded stream metadata mismatch")
                    ssim = re.search(r"All:([0-9.]+)", (directory / "ssim.log").read_text().split("SSIM Y:")[-1])
                    result = {"name": name, "mode": mode, "scene": scene, "target_bps": rate,
                              "encode_wall_seconds": seconds, "ssim": float(ssim.group(1)),
                              "stream": stream, **measure_stream(h264.read_bytes())}
                    if result["au_bytes"]["count"] != 200 or result["slices_per_au"]["max"] != slice_count:
                        raise ValueError(name + ": actual AU/slice count differs from requested profile")
                    (directory / "metrics.json").write_text(json.dumps(result, indent=2) + "\n")
                    summary["candidates"].append({k: v for k, v in result.items() if k != "per_au"})
                    print(json.dumps({"candidate": name, "ssim": result["ssim"],
                                      "bitrate": result["annexb_bitrate_bps"],
                                      "link_bps": result["link_bitrate_rs24_32_bps"]}), flush=True)
    finally:
        (output / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cut-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--slice-count", type=int, default=8)
    args = parser.parse_args()
    sweep(args.source, args.cut_source, args.output.resolve(), args.slice_count)
