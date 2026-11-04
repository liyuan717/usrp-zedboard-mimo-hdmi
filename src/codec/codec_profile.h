/* SPDX-License-Identifier: MIT
 * Opt-in finite-run stage timing. Never emits binary protocol data. */
#ifndef MIMO_CODEC_PROFILE_H
#define MIMO_CODEC_PROFILE_H
#include <stdbool.h>
#include <stdint.h>
#include <time.h>

enum codec_profile_stage { CODEC_PROFILE_VALIDATE, CODEC_PROFILE_DECODE,
    CODEC_PROFILE_CONVERT, CODEC_PROFILE_CRC, CODEC_PROFILE_WRITE,
    CODEC_PROFILE_STAGE_COUNT };
struct codec_profile_stamp { uint64_t wall_ns, cpu_ns; bool valid; };
struct codec_profile {
    bool enabled, clock_error;
    uint64_t wall_ns[CODEC_PROFILE_STAGE_COUNT];
    uint64_t cpu_ns[CODEC_PROFILE_STAGE_COUNT];
    uint64_t calls[CODEC_PROFILE_STAGE_COUNT];
    uint64_t frames;
};
static inline struct codec_profile_stamp codec_profile_begin(struct codec_profile *p) {
    struct codec_profile_stamp s = {0, 0, false};
    struct timespec wall, cpu;
    if (!p->enabled) return s;
    if (clock_gettime(CLOCK_MONOTONIC, &wall) ||
        clock_gettime(CLOCK_THREAD_CPUTIME_ID, &cpu)) {
        p->clock_error = true;
        return s;
    }
    s.wall_ns = (uint64_t)wall.tv_sec * UINT64_C(1000000000) + (uint64_t)wall.tv_nsec;
    s.cpu_ns = (uint64_t)cpu.tv_sec * UINT64_C(1000000000) + (uint64_t)cpu.tv_nsec;
    s.valid = true;
    return s;
}
static inline void codec_profile_end(struct codec_profile *p, enum codec_profile_stage stage,
                                     struct codec_profile_stamp start) {
    struct codec_profile_stamp end;
    if (!p->enabled || !start.valid) return;
    end = codec_profile_begin(p);
    if (!end.valid) return;
    if (end.wall_ns < start.wall_ns || end.cpu_ns < start.cpu_ns) {
        p->clock_error = true;
        return;
    }
    p->wall_ns[stage] += end.wall_ns - start.wall_ns;
    p->cpu_ns[stage] += end.cpu_ns - start.cpu_ns;
    ++p->calls[stage];
}
#endif
