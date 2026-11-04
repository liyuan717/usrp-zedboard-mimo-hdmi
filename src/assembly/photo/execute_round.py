#!/usr/bin/env python3
"""Execute one authorized finite 36-burst first-AU RF/HDMI round; no retry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import urllib.request

HERE = Path(__file__).resolve().parent
INTEGRATION = Path('/home/leeeo/Documents/ChatGPT/zedboard+fmcomms3/deliverables/usrp-zedboard-integration-20261026')
sys.path.insert(0, str(INTEGRATION))
from serial_query import query  # noqa: E402

BOOT_ID = "93ddbf22-cb17-4900-9ff6-3ecb0fbc7ff8"
RESULTS = HERE / "board-evidence"


def uart(command: str, name: str, timeout: float = 20) -> bytes:
    try:
        payload = query(command, timeout=timeout)
    except BaseException as error:
        (HERE / f"uart-{name}.log").write_text(str(error), encoding="utf-8")
        raise
    (HERE / f"uart-{name}.log").write_bytes(payload)
    return payload


def save(name: str, value: dict) -> None:
    (HERE / name).write_text(
        json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def main() -> int:
    authorization = json.loads((HERE / "AUTHORIZATION.json").read_text())
    if (
        authorization.get("authorized") is not True
        or authorization.get("tx_gain_db") != 18.0
        or authorization.get("maximum_frames") != 42
        or authorization.get("unique_frames") != 41
        or authorization.get("duplicate_prefix_frame") != 1
    ):
        raise ValueError("authorization scope mismatch")
    if authorization.get("scope") != "finite RF42 seu photo AU and HDMI with idle same-buffer scanout refresh; fresh ADC buffered, real PL; no SD/FPGA/reload":
        raise ValueError("specific finite-video authorization required")
    if (HERE / "HOST-RESULT.json").exists():
        raise ValueError("round already attempted")

    manifest=json.loads((HERE/'CANDIDATE-MANIFEST.json').read_text())
    for name,digest in manifest['critical_sha256'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest:
            raise ValueError('prepared artifact changed: '+name)
    state = {
        "scope": authorization["scope"],
        "valid": False,
        "transmit_attempted": False,
        "events": {},
    }
    server = None
    tx_process = None
    try:
        wire=uart("python3 -c \"from pathlib import Path;Path('/tmp/link-live/photo-seu-reproduce-r2').mkdir();print('FINITE_DIR_CREATED')\"",'create-board-dir',10)
        assert b'\nFINITE_DIR_CREATED\r\n' in wire,'candidate RAM directory already exists'


        def transfer(local_file: Path, resource: str, label: str) -> None:
            nonlocal server
            digest = hashlib.sha256(local_file.read_bytes()).hexdigest()
            receipt = HERE / f"transfer-{label}-receipt.json"
            server_log = HERE / f"transfer-{label}-server.log"
            with server_log.open("xb") as output:
                server = subprocess.Popen(
                    [
                        sys.executable,
                        str(INTEGRATION / "serve_once.py"),
                        "--file",
                        str(local_file),
                        "--resource",
                        resource,
                        "--receipt",
                        str(receipt),
                    ],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                deadline = time.monotonic() + 5
                while True:
                    if server_log.read_text(errors="replace").find('"ready": true') >= 0:
                        break
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(f"{label} transfer server failed")
                    time.sleep(0.05)
                destination = f"/tmp/link-live/photo-seu-reproduce-r2/{local_file.name}"
                uart(
                    f"wget -O {destination} http://169.254.209.83:8002{resource}; "
                    f"sha256sum {destination}",
                    f"transfer-{label}",
                    30,
                )
                if server.wait(timeout=5) != 0:
                    raise RuntimeError(f"{label} transfer did not complete")
                if json.loads(receipt.read_text()).get("served") is not True:
                    raise RuntimeError(f"{label} transfer receipt missing")
                transfer_log = (HERE / f"uart-transfer-{label}.log").read_text(
                    errors="replace"
                )
                if digest not in transfer_log:
                    raise RuntimeError(f"{label} SHA verification failed")
            server = None

        transfer(
            INTEGRATION
            / "realtime-hdmi/candidates/hdmi-lazy-r6-idle-repeat/build-arm/hdmi-presenter",
            "/photo-seu-reproduce-r2-presenter",
            "presenter",
        )
        for local,resource,label in [
            (INTEGRATION/'realtime-hdmi/candidates/finite-video-long-r2/live-rx-arm','/photo-seu-reproduce-r2-native','native'),
            (INTEGRATION/'realtime-hdmi/candidates/finite-video-long-r2/runtime-tools/rf_native_video_receiver.py','/photo-seu-reproduce-r2-receiver','receiver'),
            (HERE/'make_runtime_closure.py','/photo-seu-reproduce-r2-closure','closure'),
            (HERE/'acceptance_gate.py','/photo-seu-reproduce-r2-gate','gate'),
            (HERE/'scanout_health.py','/photo-seu-reproduce-r2-sampler','sampler'),
            (HERE/'AUTHORIZATION.json','/photo-seu-reproduce-r2-authorization','authorization')]:
            transfer(local,resource,label)
        transfer(
            HERE / "board_supervisor.py",
            "/photo-seu-reproduce-r2-supervisor",
            "supervisor",
        )
        transfer(
            HERE / "evidence_http_gain18.py",
            "/photo-seu-reproduce-r2-http",
            "http",
        )

        state["events"]["supervisor_launch_host_monotonic"] = time.monotonic()
        uart(
            "python3 /tmp/link-live/photo-seu-reproduce-r2/board_supervisor.py "
            "> /tmp/link-share/photo-seu-reproduce-r2-supervisor.log 2>&1 &",
            "supervisor-launch",
            15,
        )
        deadline = time.monotonic() + 45
        while True:
            ready = uart(
                "python3 -c \"from pathlib import Path;"
                "print('READY='+str(Path('/tmp/link-share/photo-seu-reproduce-r2-au-hdmi/READY').exists()));"
                "print('DONE='+str(Path('/tmp/link-share/photo-seu-reproduce-r2-au-hdmi/result.json').exists()));"
                "v=Path('/tmp/link-share/photo-seu-reproduce-r2-au-hdmi/video.log');"
                "print('VIDEO_HOLD='+str(v.exists() and 'FINITE_VIDEO_HOLD_BEGIN' in v.read_text(errors='replace')));"
                "n=Path('/tmp/link-share/photo-seu-reproduce-r2-au-hdmi/native.log');"
                "print('NATIVE_TAIL='+repr(n.read_text(errors='replace')[-120:] if n.exists() else 'not started'))\"",
                f"ready-{int((time.monotonic() - state['events']['supervisor_launch_host_monotonic']) * 5)}",
                10,
            )
            if b"\nREADY=True\r\n" in ready:
                break
            if b"\nDONE=True\r\n" in ready:
                startup_detail = uart(
                    "cat /tmp/link-share/photo-seu-reproduce-r2-supervisor.log; "
                    "cat /tmp/link-share/photo-seu-reproduce-r2-au-hdmi/result.json",
                    "startup-failure",
                    10,
                )
                raise RuntimeError(
                    "board supervisor ended during startup: "
                    + startup_detail.decode(errors="replace")[-4000:]
                )
            if time.monotonic() > deadline:
                raise TimeoutError("board receiver startup timeout")
            time.sleep(0.10)

        state["transmit_attempted"] = True
        state["events"]["tx_start_host_monotonic"] = time.monotonic()
        with (HERE / "host-tx-run.log").open("xb") as log:
            tx_process = subprocess.Popen(
                [
                    sys.executable,
                    str(HERE / "send_gain18_12f.py"),
                    "--out",
                    str(HERE / "tx-live-gain18"),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            tx_deadline = time.monotonic() + 15
            while True:
                try:
                    actual = json.loads(
                        (HERE / "tx-live-gain18" / "actual-readback.json").read_text()
                    )
                except (FileNotFoundError, json.JSONDecodeError):
                    actual = None
                if actual is not None:
                    break
                if tx_process.poll() is not None:
                    raise RuntimeError("TX exited before actual readback")
                if time.monotonic() > tx_deadline:
                    raise TimeoutError("TX actual readback timeout")
                time.sleep(0.005)
            if actual.get("configuration_matches_requested") is not True:
                raise RuntimeError("USRP actual configuration mismatch")
            configured = time.monotonic()
            state["events"]["actual_readback_host_monotonic"] = configured
            # Launch later than gain18e. Its capture caught only the sharp LO
            # onset at block 961; the actual frames follow after the 100 ms
            # zero-IQ preroll. This window brackets the data rather than onset.
            while time.monotonic() < configured + 0.65:
                if tx_process.poll() is not None:
                    raise RuntimeError("TX exited before RF trigger")
                time.sleep(0.001)
            uart("touch /tmp/link-live/photo-seu-reproduce-r2.trigger", "rf-trigger", 10)
            state["events"]["rf_trigger_host_monotonic"] = time.monotonic()
            try:
                completed_code = tx_process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                tx_process.terminate()
                tx_process.wait(timeout=5)
                raise TimeoutError("TX process timeout")
        state["events"]["tx_end_host_monotonic"] = time.monotonic()
        completed = subprocess.CompletedProcess([], completed_code)
        tx_result = json.loads((HERE / "tx-live-gain18" / "RESULT.json").read_text())
        state["tx_result"] = tx_result
        if completed.returncode != 0 or tx_result.get("valid") is not True:
            raise RuntimeError("host TX validation failed")

        # Keep the UART silent for the entire decode/HDMI hold window.  A
        # single bounded board-side wait avoids repeated host command launches
        # and UART interrupts competing with the display worker.
        done = uart(
            "i=0; while [ ! -f /tmp/link-share/photo-seu-reproduce-r2-au-hdmi/result.json ] && [ $i -lt 340 ]; "
            "do sleep 0.5; i=$((i+1)); done; "
            "python3 -c \"from pathlib import Path;"
            "print('DONE='+str(Path('/tmp/link-share/photo-seu-reproduce-r2-au-hdmi/result.json').exists()));"
            "v=Path('/tmp/link-share/photo-seu-reproduce-r2-au-hdmi/video.log');"
            "print('VIDEO_HOLD='+str(v.exists() and 'FINITE_VIDEO_HOLD_BEGIN' in v.read_text(errors='replace')));"
            "n=Path('/tmp/link-share/photo-seu-reproduce-r2-au-hdmi/native.log');"
            "print('NATIVE_TAIL='+repr(n.read_text(errors='replace')[-120:] if n.exists() else 'not started'))\"",
            "round-complete-silent-wait",
            180,
        )
        print(done.decode(errors="replace")[-400:], flush=True)
        if b"\nDONE=True\r\n" not in done:
            raise TimeoutError("board round completion marker missing")

        uart(
            "python3 /tmp/link-live/photo-seu-reproduce-r2/evidence_http_gain18.py "
            "> /tmp/link-share/photo-seu-reproduce-r2-http.log 2>&1 &",
            "http-launch",
            10,
        )
        deadline = time.monotonic() + 10
        while True:
            http_log = uart(
                "cat /tmp/link-share/photo-seu-reproduce-r2-http.log",
                f"http-ready-{int(time.monotonic())}",
                10,
            )
            if b"\nFINITE_VIDEO_R1_HTTP_READY\r\n" in http_log:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("evidence HTTP startup timeout")
            time.sleep(0.2)

        hash_command = (
            "python3 - <<'PYE'\n"
            "from pathlib import Path\n"
            "import hashlib,json\n"
                "r=Path('/tmp/link-share/photo-seu-reproduce-r2-au-hdmi')\n"
            "print('FILE_SHA_JSON:'+json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in r.iterdir() if p.is_file()}))\n"
            "PYE"
        )
        hash_wire = uart(hash_command, "evidence-hashes", 20)
        hashes = json.loads(
            next(
                line.split(":", 1)[1]
                for line in hash_wire.decode(errors="replace").splitlines()
                if line.startswith("FILE_SHA_JSON:")
            )
        )
        RESULTS.mkdir()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        names = [
            "new-adc-output.rxth",
            "new-adc-output.rxth.failure.rxth",
            "received-live.rfd",
            "result.json",
            "native.log",
            "video.log",
            "video-report.json",
            "hdmi-proof.json",
            "kernel-before.log",
            "kernel-after.log",
            "rxth-before.json",
            "interrupts-after.log",
            "health-samples.json",
        ]
        for name in names:
            if name not in hashes:
                continue
            with opener.open("http://169.254.209.84:8001/photo-seu-reproduce-r2/" + name, timeout=20) as response:
                payload = response.read(32 * 1024 * 1024 + 1)
            if len(payload) > 32 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != hashes[name]:
                raise RuntimeError("evidence transfer hash mismatch: " + name)
            (RESULTS / name).write_bytes(payload)
        with opener.open(
            "http://169.254.209.84:8001/photo-seu-reproduce-r2/supervisor.log",
            timeout=5,
        ) as response:
            supervisor_log = response.read(256 * 1024)
        (RESULTS / "supervisor.log").write_bytes(supervisor_log)
        save("BOARD-EVIDENCE-HASHES.json", hashes)
        board_result = json.loads((RESULTS / "result.json").read_text())
        state["board_result"] = board_result
        from grade_evidence import grade
        try:
            independent=grade(RESULTS)
        except BaseException as error:
            independent={'valid':False,'failure':type(error).__name__+': '+str(error)}
        save('INDEPENDENT-GRADE.json',independent)
        state['independent_grade']=independent
        visual_only = authorization.get("visual_only_no_scanout_sampler") is True
        state["visual_only_diagnostic"] = visual_only
        state["visual_pass"] = False
        state["visual_confirmation"] = None
        state["valid"] = bool(
            tx_result.get("valid")
            and board_result.get("valid")
            and independent.get("valid")
        )
    except BaseException as error:
        state["failure"] = type(error).__name__ + ": " + str(error)
        traceback.print_exc()
        try:
            uart("touch /tmp/link-live/photo-seu-reproduce-r2.stop", "host-stop", 10)
        except BaseException:
            pass
    finally:
        try:
            uart("touch /tmp/link-live/photo-seu-reproduce-r2-http.stop", "http-stop", 10)
        except BaseException:
            pass
        if tx_process is not None and tx_process.poll() is None:
            tx_process.terminate()
            try:tx_process.wait(timeout=5)
            except subprocess.TimeoutExpired:tx_process.kill();tx_process.wait(timeout=3)
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=2)
        save("HOST-RESULT.json", state)

    print(json.dumps(state))
    return 0 if state.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
