"""Measure the local finite video sender group, including UHD and FFmpeg."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time


FIELDS = ('Rss', 'Pss', 'SwapPss')
SAMPLE_SECONDS = 0.2
STREAM_ROLES = {'run_g0_video_air.py', 'g0_camera_live_stream.py', 'ffmpeg', 'g0_uhd_probe'}


def sample_group(group):
    members = []
    unavailable = []
    listing = subprocess.check_output(['ps', '-e', '-o', 'pid=,pgid='], text=True)
    for line in listing.splitlines():
        pid, pgid = map(int, line.split())
        if pgid != group:
            continue
        proc = Path('/proc') / str(pid)
        try:
            stat = (proc / 'stat').read_text().rsplit(') ', 1)[1].split()
            if stat[0] in ('Z', 'X'):
                continue
            if int(stat[2]) != group:
                continue
            executable = Path(os.readlink(proc / 'exe')).name
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            role = Path(os.fsdecode(argv[1])).name if executable.startswith('python') and len(argv) > 2 else executable
            memory = {}
            for row in (proc / 'smaps_rollup').read_text().splitlines():
                key, _, value = row.partition(':')
                if key in FIELDS:
                    memory[key] = int(value.split()[0])
            if memory.keys() != set(FIELDS):
                raise ValueError('missing smaps_rollup fields')
        except (OSError, ValueError) as error:
            unavailable.append({'pid': pid, 'error': str(error)})
            continue
        members.append(dict(pid=pid, start_ticks=int(stat[19]), role=role,
                            cpu_ticks=int(stat[11]) + int(stat[12]), **memory))
    return members, unavailable


def peak_kib(samples):
    return {field: max((sum(row[field] for row in sample['members']) for sample in samples), default=None)
            for field in FIELDS}


def stream_summary(samples, first, last):
    window = [sample for sample in samples if first <= sample['monotonic_ns'] and sample['sample_end_monotonic_ns'] <= last]
    together = [sample for sample in window if STREAM_ROLES <= {row['role'] for row in sample['members']}]
    by_process = {}
    for sample in window:
        for row in sample['members']:
            identity = (row['pid'], row['start_ticks'])
            by_process.setdefault(identity, []).append(row['cpu_ticks'])
    cpu_ticks = sum(ticks[-1] - ticks[0] for ticks in by_process.values())
    stamps = [first, *(sample['monotonic_ns'] for sample in window), last]
    return dict(clock_window_ns=[first, last], samples=len(window), all_sender_roles_together=len(together),
                maximum_observation_gap_ns=max(end - start for start, end in zip(stamps, stamps[1:])),
                sampled_process_CPU_seconds_lower_bound=cpu_ticks / os.sysconf('SC_CLK_TCK'),
                unreadable_member_samples=sum(bool(sample['unavailable']) for sample in window),
                sampled_group_memory_peak_KiB=peak_kib(window),
                sampled_all_sender_roles_memory_peak_KiB=peak_kib(together))


def measure_sender(command, output, rf_receipt):
    output.mkdir()
    samples = []
    observer_start = time.process_time()
    time_path = output / 'time.txt'
    time_command = ['/usr/bin/time', '-f', '%e %U %S %M', '-o', str(time_path), *command]
    (output / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
    with (output / 'process-samples.jsonl').open('x', buffering=1) as log:
        child = subprocess.Popen(time_command, start_new_session=True)
        sampling_error = None
        try:
            while child.poll() is None:
                start = time.monotonic_ns()
                try:
                    members, unavailable = sample_group(child.pid)
                    sample = dict(monotonic_ns=start, sample_end_monotonic_ns=time.monotonic_ns(),
                                  members=members, unavailable=unavailable)
                    log.write(json.dumps(sample) + '\n')
                    samples.append(sample)
                except (OSError, subprocess.SubprocessError, ValueError) as error:
                    sampling_error = repr(error)
                    break
                time.sleep(SAMPLE_SECONDS)
            returncode = child.wait()
        except KeyboardInterrupt:
            # Let the coordinator drain the burst and run its own hardware cleanup.
            # Signal only that process, so it retains control of UHD and its producer.
            if child.poll() is None:
                try:
                    coordinator = Path(f'/proc/{child.pid}/task/{child.pid}/children').read_text().split()
                    if len(coordinator) == 1:
                        os.kill(int(coordinator[0]), signal.SIGINT)
                except OSError:
                    pass
                child.wait()
            raise
    result = dict(returncode=returncode, observer_CPU_seconds=time.process_time() - observer_start,
                  observed_roles=sorted({row['role'] for sample in samples for row in sample['members']}),
                  sample_count=len(samples), sampling_error=sampling_error,
                  unreadable_member_samples=sum(bool(sample['unavailable']) for sample in samples),
                  sampled_group_memory_peak_KiB=peak_kib(samples),
                  kernel_or_IRQ_CPU_included=False)
    try:
        elapsed, user, system, rss = time_path.read_text().splitlines()[-1].split()
        elapsed, user, system = (float(value.replace(',', '.')) for value in (elapsed, user, system))
        cpu = user + system
        result.update(process_group_wall_seconds=elapsed, process_group_CPU_seconds=cpu,
                      process_group_average_core_equivalents=cpu / elapsed if elapsed else None,
                      max_single_process_RSS_KiB=int(rss))
    except (OSError, ValueError, IndexError) as error:
        result['CPU_accounting_unavailable'] = repr(error)
    try:
        radio = json.loads(rf_receipt.read_text())
        if radio['EOB_confirmed']:
            clock = json.loads((rf_receipt.parent / 'tx-clock.json').read_text())
            first, last = clock[0], radio['TX_clock_end_bracket'][1]
            if not (samples and samples[0]['monotonic_ns'] <= first < last <= time.monotonic_ns()):
                raise ValueError('TX clock window is outside sample time base')
            result['GO_through_dual_EOB_observation'] = stream_summary(samples, first, last)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        result['TX_window_unavailable'] = repr(error)
    (output / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    return returncode
