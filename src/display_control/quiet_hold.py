"""Bounded quiet hold after successful native exit; no rendering/decoding changes."""
import time


def wait_for_quiet_hold(video, *, deadline, stop_requested, snapshot,
                        baseline_halts, clock=time.monotonic, sleep=time.sleep):
    checks = 0
    next_check = clock()
    started = next_check

    def validate():
        nonlocal checks
        value = snapshot()
        checks += 1
        if (value['taint'] != 4096 or value['LO'] != ['1', '1']
                or value['ADC'] != '0' or value['ready_blocks'] != 0
                or value['ring_halts'] != baseline_halts):
            raise RuntimeError('quiet hold observed unsafe state: ' + repr(value))

    validate()
    while video.poll() is None:
        now = clock()
        if stop_requested():
            raise RuntimeError('owner cancelled quiet hold')
        if now >= deadline:
            raise TimeoutError('quiet hold overall deadline expired')
        if now >= next_check:
            validate()
            next_check = now + 1.0
        sleep(min(.1, max(0, deadline - now)))
    validate()
    if video.poll() != 0:
        raise RuntimeError('video failed during quiet hold')
    return dict(active=True,elapsed_s=clock()-started,safety_checks=checks,
                check_interval_s=1.0,stop_poll_max_s=.1,
                native_already_exited_successfully=True,
                kernel_errors_still_checked_after_teardown=True)
