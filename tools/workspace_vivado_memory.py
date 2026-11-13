"""Select and monitor Vivado concurrency using measured host memory."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

MIB = 1024 * 1024
RAM_RESERVE_MIB = 1536
SWAP_RESERVE_MIB = 768
INTERVAL_S = 5


def memory():
    mem = dict(re.findall(r'^(MemAvailable|SwapFree):\s+(\d+)', Path('/proc/meminfo').read_text(), re.M))
    swaps = dict(re.findall(r'^(pswpin|pswpout) (\d+)', Path('/proc/vmstat').read_text(), re.M))
    return dict(ram_mib=int(mem['MemAvailable']) // 1024, swap_mib=int(mem['SwapFree']) // 1024,
                swap_pages_in=int(swaps['pswpin']), swap_pages_out=int(swaps['pswpout']))


def select_threads(ram_mib, peak_mib, reference_threads, limit=8, increment_mib=384, *, margin_percent=10):
    # Extrapolate only the thread-dependent part. Keep the host reserve
    # separate from the allowance for variation in the measured peak.
    budget = ram_mib - RAM_RESERVE_MIB
    reference_estimate = (peak_mib * (100 + margin_percent) + 99) // 100
    for threads in range(limit, 0, -1):
        estimate = reference_estimate + (threads - reference_threads) * increment_mib
        if estimate <= budget:
            return threads, estimate
    raise RuntimeError(f'Vivado memory budget {budget}MiB cannot cover one worker')


def group_memory(pgid):
    pids = subprocess.check_output(['ps', '-e', '-o', 'pid=,pgid='], text=True)
    rss_kib = swap_kib = members = 0
    for row in pids.splitlines():
        pid, group = map(int, row.split())
        if group != pgid:
            continue
        try:
            status = Path(f'/proc/{pid}/status').read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        members += 1
        amounts = dict(re.findall(r'^(VmRSS|VmSwap):\s+(\d+)', status, re.M))
        rss_kib += int(amounts.get('VmRSS', 0))
        swap_kib += int(amounts.get('VmSwap', 0))
    return members, (rss_kib + 1023) // 1024, (swap_kib + 1023) // 1024


def monitor(pgid, running, report):
    if pgid <= 1 or pgid == os.getpgrp():
        raise ValueError('Vivado must own a separate process group')
    previous = memory()
    then = time.monotonic()
    low = paging = peak = 0
    with report.open('x', buffering=1) as log:
        while running():
            row = memory()
            now = time.monotonic()
            members, rss, swap = group_memory(pgid)
            peak = max(peak, rss + swap)
            pages = row['swap_pages_in'] + row['swap_pages_out']
            prior = previous['swap_pages_in'] + previous['swap_pages_out']
            rate = (pages-prior)*os.sysconf('SC_PAGE_SIZE')/MIB/max(now-then, 1)
            low = low+1 if row['ram_mib'] < RAM_RESERVE_MIB else 0
            near_reserve = row['ram_mib'] < 2560 or (
                row['ram_mib'] < 3072 and row['swap_mib'] < SWAP_RESERVE_MIB)
            paging = paging+1 if rate >= 32 and near_reserve else 0
            reason = ('hard memory floor' if row['ram_mib'] < 512 else
                      'sustained low available memory' if low >= 3 else
                      'sustained paging near RAM reserve' if paging >= 3 else None)
            row.update(unix=time.time(), pgid=pgid, members=members, build_rss_mib=rss,
                       build_swap_mib=swap, peak_build_rss_plus_swap_mib=peak,
                       host_swap_mib_s=round(rate, 2), stop_reason=reason)
            log.write(json.dumps(row)+'\n')
            if reason and running():
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    return None, peak
                return reason, peak
            previous, then = row, now
            time.sleep(INTERVAL_S)
    return None, peak
