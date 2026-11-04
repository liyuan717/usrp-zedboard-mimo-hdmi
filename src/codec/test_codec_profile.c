#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <time.h>
static unsigned calls;
static int fail_clock;
static int fake_clock_gettime(clockid_t clock_id, struct timespec *ts) {
    assert(clock_id == CLOCK_MONOTONIC || clock_id == CLOCK_THREAD_CPUTIME_ID);
    ++calls;
    if (fail_clock) return -1;
    ts->tv_sec = 0;
    ts->tv_nsec = (long)calls * 1000;
    return 0;
}
#define clock_gettime fake_clock_gettime
#include "codec_profile.h"
#undef clock_gettime
#include <stdio.h>
int main(void) {
    struct codec_profile p = {0};
    struct codec_profile_stamp s = codec_profile_begin(&p);
    assert(!s.valid && calls == 0);
    codec_profile_end(&p, CODEC_PROFILE_DECODE, s);
    assert(calls == 0 && p.calls[CODEC_PROFILE_DECODE] == 0);
    p.enabled = true;
    s = codec_profile_begin(&p);
    codec_profile_end(&p, CODEC_PROFILE_CONVERT, s);
    assert(p.calls[CODEC_PROFILE_CONVERT] == 1);
    assert(p.wall_ns[CODEC_PROFILE_CONVERT] == 2000);
    assert(p.cpu_ns[CODEC_PROFILE_CONVERT] == 2000);
    assert(p.calls[CODEC_PROFILE_DECODE] == 0 && !p.clock_error);
    fail_clock = 1;
    s = codec_profile_begin(&p);
    assert(!s.valid && p.clock_error);
    codec_profile_end(&p, CODEC_PROFILE_CONVERT, s);
    assert(p.calls[CODEC_PROFILE_CONVERT] == 1);
    fail_clock = 0;
    s = codec_profile_begin(&p);
    s.wall_ns = UINT64_MAX;
    codec_profile_end(&p, CODEC_PROFILE_WRITE, s);
    assert(p.calls[CODEC_PROFILE_WRITE] == 0);
    puts("CODEC_PROFILE_PASS disabled_no_clocks separate_stages clock_failure no_underflow");
    return 0;
}
