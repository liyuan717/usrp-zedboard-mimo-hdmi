#!/usr/bin/env python3
"""One authorized online first-AU RF-to-HDMI round. No retry."""

from pathlib import Path
import json
import hashlib
import os
import signal
import shutil
import subprocess
import time
import traceback
import sys
sys.path.insert(0, "/tmp/link-live/photo-seu-reproduce-r2")
from acceptance_gate import check as acceptance_check
from scanout_health import Sampler, analyze

R = Path("/tmp/link-live")
O = Path("/tmp/link-share/photo-seu-reproduce-r2-au-hdmi")
P = Path("/sys/bus/iio/devices/iio:device0")
RING = Path("/sys/class/misc/zed-rxth/device").resolve()
STOP = R / "photo-seu-reproduce-r2.stop"
TRIGGER = R / "photo-seu-reproduce-r2.trigger"
CONSUMER_STOP = R / "photo-seu-reproduce-r2.consumer-stop"
RX_LO = P / "out_altvoltage0_RX_LO_powerdown"
TX_LO = P / "out_altvoltage1_TX_LO_powerdown"


def save(name, value):
    (O / name).write_text(
        json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def counters():
    return {
        name: int((RING / name).read_text())
        for name in ("ring_halts", "ready_blocks", "ring_capacity")
    }


def retire(child, grace=20):
    if child is None or child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=2)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main():
    auth=json.loads((R/'photo-seu-reproduce-r2/AUTHORIZATION.json').read_text())
    assert auth.get('authorized') is True and auth.get('scope')=='finite RF42 seu photo AU and HDMI with idle same-buffer scanout refresh; fresh ADC buffered, real PL; no SD/FPGA/reload'
    assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==auth['expected_boot_id']
    O.mkdir(parents=False, exist_ok=False)
    meminfo={line.split(":")[0]:int(line.split()[1])*1024 for line in Path("/proc/meminfo").read_text().splitlines()}
    assert meminfo["MemAvailable"] >= 96*1024*1024, "insufficient bounded working memory"
    assert shutil.disk_usage("/tmp").free >= 40*1024*1024, "insufficient RAM evidence storage"
    assert not STOP.exists() and not TRIGGER.exists() and not CONSUMER_STOP.exists()
    assert (
        Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        == (R / "EXPECTED_BOOT_ID").read_text().strip()
    )
    assert int(Path("/proc/sys/kernel/tainted").read_text()) == 4096
    assert "/dev/root / ext4 ro," in Path("/proc/mounts").read_text()
    assert Path("/sys/bus/iio/devices/iio:device3/buffer/enable").read_text().strip() == "0"
    assert RX_LO.read_text().strip() == TX_LO.read_text().strip() == "1"
    try:
        priors = json.loads((R / "receive-priors.json").read_text())
    except FileNotFoundError:
        # A fresh RAM extraction intentionally does not carry prior-run state.
        # The current-window CFO estimator supplies frequency from this run;
        # only the independently calibrated sample-clock offset is retained.
        priors = {
            "coarse_frequency_hz": 7223.213205776218,
            "sample_clock_offset_ppm": 39.98002049598881,
        }
    before = counters()
    save("rxth-before.json", before)
    with (O / "kernel-before.log").open("xb") as stream:
        subprocess.run(["/bin/busybox", "dmesg"], stdout=stream, check=True, timeout=5)

    native = video = None
    sampler = None
    sampler_started = False
    rx_dma_quiesced = False
    native_log = (O / "native.log").open("xb")
    video_log = (O / "video.log").open("xb")
    result = {
        "scope": "online 18 dB RF, duplicate frame1 prefix, unique frames1..41, real PL, first AU, HDMI idle refresh, hold-only read-only scanout sampling",
        "valid": False,
        "tx_gain_db": 18.0,
        "maximum_frames": 42,
        "unique_frames": 41,
        "duplicate_prefix_frame": 1,
    }
    try:
        python = os.readlink("/proc/self/exe")
        presenter_candidate=R/'photo-seu-reproduce-r2/hdmi-presenter'
        candidate_sha=hashlib.sha256(presenter_candidate.read_bytes()).hexdigest()
        assert candidate_sha=='33414cfa2ba8ae6e48f946ce098f8d90e0b65465333069aa41acb0930b72f295'
        assert hashlib.sha256((R/'photo-seu-reproduce-r2/live-rx-arm').read_bytes()).hexdigest()==auth['native_sha256']
        subprocess.run([python,str(R/'photo-seu-reproduce-r2/make_runtime_closure.py')],check=True,timeout=15)
        result["presenter_candidate_sha256"] = candidate_sha
        video_argv = [
            python,
            str(R / "photo-seu-reproduce-r2/rf_native_video_receiver.py"),
            "--delivery-socket",
            str(O / "live.sock"),
            "--runtime-closure",
            str(R / "photo-seu-reproduce-r2/runtime-closure.json"),
            "--proof-output",
            str(O / "hdmi-proof.json"),
            "--report",
            str(O / "video-report.json"),
            "--live-record-output",
            str(O / "received-live.rfd"),
            "--stop-file",
            str(CONSUMER_STOP),
            "--session-id",
            "714",
            "--imbc-build-id",
            "1",
            "--records",
            "42",
            "--max-records",
            "42",
            "--expected-access-units",
            "1",
            "--timeout-s",
            "30",
            "--idle-timeout-s",
            "130",
            "--finish-after-accepted",
            "--hold-final-frame-ms",
            "10000",
            "--max-pending",
            "2",
        ]
        video_env = dict(
            os.environ,
            PYTHONPATH=str(R / "python") + ":" + str(R / "python/tools"),
        )
        video = subprocess.Popen(
            video_argv,
            env=video_env,
            stdout=video_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 45
        while not (O / "live.sock").exists():
            if video.poll() is not None:
                raise RuntimeError("video consumer exited before socket ready")
            if time.monotonic() > deadline:
                raise TimeoutError("video socket startup timeout")
            time.sleep(0.02)

        RX_LO.write_text("0\n")
        assert RX_LO.read_text().strip() == "0"
        assert TX_LO.read_text().strip() == "1"
        (O / "READY").write_text(
            json.dumps({"ready": True, "monotonic_ns": time.monotonic_ns()}) + "\n"
        )
        deadline = time.monotonic() + 45
        while not TRIGGER.exists():
            if STOP.exists():
                raise RuntimeError("host stopped before RF trigger")
            if video.poll() is not None:
                raise RuntimeError("video consumer exited before RF trigger")
            if time.monotonic() > deadline:
                raise TimeoutError("RF trigger timeout")
            time.sleep(0.002)

        env = dict(
            os.environ,
            LINK_FINITE_CAPTURE_CFO="1",
            LINK_DIAGNOSTIC_COARSE_CFO_HZ=str(priors["coarse_frequency_hz"]),
            LINK_DIAGNOSTIC_SFO_PPM=str(priors["sample_clock_offset_ppm"]),
            LINK_DIAGNOSTIC_RF_FRAME_BUDGET="42",
            LINK_RXTH_EVIDENCE_OUT=str(O / "new-adc-output.rxth"),
        )
        native_argv = [
            str(R / "presenter/lib/ld-linux-armhf.so.3"),
            "--library-path",
            str(R / "presenter/lib"),
            str(R / "photo-seu-reproduce-r2/live-rx-arm"),
            "start-rx",
            "--blocks",
            "2048",
            "--imbc-build-id",
            "1",
            "--deadline-ms",
            "120000",
            "--delivery-socket",
            str(O / "live.sock"),
        ]
        native = subprocess.Popen(
            native_argv,
            env=env,
            stdout=native_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        overall = time.monotonic() + 150
        while native.poll() is None or video.poll() is None:
            # Fresh ADC capture is complete before CFO/DSP/PL starts.
            hold_seen = (O / "video.log").is_file() and "FINITE_VIDEO_HOLD_BEGIN" in (O / "video.log").read_text(errors="replace")
            if hold_seen and auth.get("unbind_rx_dma_before_hold") is True and not rx_dma_quiesced:
                if native.poll() is None:
                    result["rx_dma_quiesce_waiting_for_native_exit"] = True
                else:
                    Path("/sys/bus/platform/drivers/rxth-dmac-diag/unbind").write_text("7c440000.dma\n")
                    assert not Path("/sys/bus/platform/devices/7c440000.dma/driver").exists()
                    rx_dma_quiesced = True
                    result["rx_dma_unbound_during_hold"] = True
            if not sampler_started and hold_seen:
                sampler_started = True
                if auth.get("visual_only_no_scanout_sampler") is True:
                    result["scanout_sampling"] = {
                        "disabled_for_visual_isolation": True,
                        "dev_mem_reads_during_hold": 0,
                    }
                    result["acceptance_profile"] = "visual_only_no_scanout_sampler"
                else:
                    sampler = Sampler()
                    sampler.start()
                    result["scanout_sampling"] = {
                        "started_during_hold_only": True,
                        "sample_interval_ms": 10,
                        "period_below_display_interval": True,
                    }
            if not result.get('RX_LO_closed_after_capture'):
                native_log.flush()
                if 'LIVE_FINITE_ADC_CLOSED' in (O/'native.log').read_text(errors='replace'):
                    RX_LO.write_text('1\n');result['RX_LO_closed_after_capture']=True
            if STOP.exists():
                result["host_stop_requested"] = True
                retire(native)
                CONSUMER_STOP.touch()
                break
            if int(Path("/proc/sys/kernel/tainted").read_text()) != 4096:
                result["failure"] = "new kernel taint"
                retire(native)
                CONSUMER_STOP.touch()
                break
            if counters()["ring_halts"] != before["ring_halts"]:
                result["failure"] = "RXTH ring halt"
                retire(native)
                CONSUMER_STOP.touch()
                break
            if time.monotonic() > overall:
                result["failure"] = "bounded overall deadline"
                retire(native)
                CONSUMER_STOP.touch()
                break
            if native.poll() not in (None, 0):
                result["failure"] = "native receiver failed"
                CONSUMER_STOP.touch()
                break
            if video.poll() not in (None, 0):
                result["failure"] = "video pipeline failed"
                retire(native)
                break
            time.sleep(0.02)
        result["native_returncode"] = None if native is None else native.poll()
        result["video_returncode"] = None if video is None else video.poll()
        result["video_report"] = read_json(O / "video-report.json")
        result["hdmi_proof"] = read_json(O / "hdmi-proof.json")
        report = result["video_report"]
        sys.path.insert(0,str(R/'python/tools'))
        from read_rxonly_delivery import iter_delivery
        with (O/'received-live.rfd').open('rb') as stream:
            delivered=list(iter_delivery(stream))
        assert all(not record.rejected for record in delivered)
        frame_ids=[record.frame_id for record in delivered]
        summaries=[]
        for line in (O/'native.log').read_text().splitlines():
            if line.startswith('{'):
                try:summaries.append(json.loads(line))
                except ValueError:pass
        native_summary=next((x for x in reversed(summaries) if x.get('schema')=='mrf1-native-rx-backend-v1'), {})
        if not native_summary:
            result.setdefault('failure','native stopped before summary; see native.log')
        result['native_summary']=native_summary
        result['received_frame_ids']=frame_ids
        result['technical_gate_problems']=acceptance_check(native_summary,report or {},result['hdmi_proof'] or {},frame_ids)
        result['valid']=bool(result['native_returncode']==0 and result['video_returncode']==0 and not result['technical_gate_problems'])
        result['physical_visual_confirmation']=None
        result['scope_limit']='one fresh-ADC finite H264 AU; buffered processing, not continuous realtime throughput'
    except BaseException as error:
        result["failure"] = type(error).__name__ + ": " + str(error)
        traceback.print_exc()
    finally:
        try:
            RX_LO.write_text("1\n")
        except OSError as error:
            result.setdefault("cleanup_errors", []).append(str(error))
        try:
            TX_LO.write_text("1\n")
        except OSError as error:
            result.setdefault("cleanup_errors", []).append(str(error))
        retire(native)
        CONSUMER_STOP.touch()
        if video is not None:
            deadline = time.monotonic() + 10
            while video.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
        retire(video)
        result["native_returncode"] = None if native is None else native.poll()
        result["video_returncode"] = None if video is None else video.poll()
        result["video_report"] = read_json(O / "video-report.json")
        result["hdmi_proof"] = read_json(O / "hdmi-proof.json")
        if sampler is not None:
            sampler.close()
            (O/'health-samples.json').write_text(json.dumps(sampler.rows)+'\n')
            result['health']=analyze(sampler.rows,sampler.errors)
            if sampler.errors:
                result['valid']=False
                result.setdefault('failure','sampler error: '+repr(sampler.errors))
        else:
            result["health"] = {
                "visual_only_not_sampled": True,
                "dev_mem_reads_during_hold": 0,
                "inference_limit": "No scanout sampler was run; visual feedback is required.",
            }
        native_log.close()
        video_log.close()
        result["rx_lo_powerdown"] = RX_LO.read_text().strip()
        result["tx_lo_powerdown"] = TX_LO.read_text().strip()
        result["adc_buffer_enable"] = Path(
            "/sys/bus/iio/devices/iio:device3/buffer/enable"
        ).read_text().strip()
        result["rxth_after"] = counters()
        result["taint"] = int(Path("/proc/sys/kernel/tainted").read_text())
        with (O / "kernel-after.log").open("xb") as stream:
            subprocess.run(["/bin/busybox", "dmesg"], stdout=stream, check=True, timeout=5)
        (O / "interrupts-after.log").write_bytes(Path("/proc/interrupts").read_bytes())
        errors=[line for line in (O/'kernel-after.log').read_text(errors='replace').splitlines()
                if any(word in line for word in ['timed out','Oops:','Kernel panic','BUG:','WARNING:'])]
        result['kernel_error_lines']=errors
        if result['taint']!=4096 or errors or result.get('cleanup_errors') or result['rx_lo_powerdown']!='1' or result['tx_lo_powerdown']!='1' or result['adc_buffer_enable']!='0' or result['rxth_after']['ring_halts']!=before['ring_halts']:
            result['valid']=False
        save("result.json", result)
        print("FINITE_VIDEO_R1_SUPERVISOR_DONE " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
