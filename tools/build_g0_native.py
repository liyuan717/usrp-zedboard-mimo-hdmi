#!/usr/bin/env python3
"""Cross-build the G0 prototype against an explicit FFmpeg 6.1.2 runtime."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess


def build(a):
    project = Path(__file__).resolve().parents[1]
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = a.ffmpeg_source.resolve()
    if subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() != "b1a4534186ca51b0457579fc05a5739eb2cc45cd":
        raise ValueError("Expected upstream FFmpeg n6.1.2")
    config = out / "ffmpeg-config"
    config.mkdir()
    commands = []
    def run(name, argv, cwd=out):
        commands.append(argv)
        (out / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        with (out / (name + ".log")).open("w") as log:
            subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=True)
    run("configure", [str(source / "configure"), "--prefix=/usr", "--arch=arm", "--cpu=cortex-a9",
        "--target-os=linux", "--enable-cross-compile", "--cross-prefix=" + a.cross_prefix,
        "--enable-neon", "--enable-asm", "--disable-everything", "--enable-decoder=h264",
        "--enable-parser=h264", "--disable-programs", "--disable-doc", "--disable-zlib"], config)
    vendor = project / "vendor"
    common = ["-O3", "-g", "-Wall", "-Wextra", "-Werror", "-mcpu=cortex-a9", "-mfpu=neon",
              "-mfloat-abi=hard", "-I" + str(vendor / "pixels"), "-I" + str(vendor / "hdmi/include"),
              "-I" + str(config), "-I" + str(source), "-pthread"]
    run("pixels", [a.cross_prefix + "gcc", "-std=c11", *common, "-c",
                   str(vendor / "pixels/yuv420p_rgb565_neon.c"), "-o", str(out / "pixels.o")])
    objects = [out / "pixels.o"]
    sources = [project / "ps/g0_decode_display.cpp"] + [vendor / "hdmi/src" / (n + ".cpp") for n in
                ("protocol", "rgb_expand", "presenter", "null_backend", "drm_backend")]
    for src in sources:
        obj = out / (src.stem + ".o")
        run(src.stem, [a.cross_prefix + "g++", "-std=c++17", *common, "-c", str(src), "-o", str(obj)])
        objects.append(obj)
    lib = a.codec_lib.resolve()
    run("link", [a.cross_prefix + "g++", "-pthread", *map(str, objects), str(lib / "libavcodec.so.60"),
         str(lib / "libavutil.so.58"), "-Wl,-rpath-link," + str(lib), "-o", str(out / "g0-decode-display")])
    runtime = out / "runtime"
    runtime.mkdir()
    # Copy explicitly supplied deployed ABI dependencies; never change their source.
    for directory in (lib, a.presenter_lib.resolve()):
        for f in directory.iterdir():
            if f.is_file():
                target = runtime / f.name
                if not target.exists():
                    shutil.copyfile(f, target)
    (out / "build-receipt.json").write_text(json.dumps({"ffmpeg_commit": "b1a4534186ca51b0457579fc05a5739eb2cc45cd",
        "compiler": subprocess.check_output([a.cross_prefix + "gcc", "--version"], text=True).splitlines()[0],
        "codec_lib": str(lib), "presenter_lib": str(a.presenter_lib.resolve()),
        "scope": "G0 measurement executable; not a complete receiver or clean SD package"}, indent=2) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cross-prefix", required=True)
    p.add_argument("--ffmpeg-source", type=Path, required=True)
    p.add_argument("--codec-lib", type=Path, required=True)
    p.add_argument("--presenter-lib", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    build(p.parse_args())
