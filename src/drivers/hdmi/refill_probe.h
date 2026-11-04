/* SPDX-License-Identifier: GPL-2.0 */
#ifndef HDMI_REFILL_PROBE_H
#define HDMI_REFILL_PROBE_H
#ifdef REFILL_PROBE_HOST_TEST
#include <stdint.h>
#include <stdbool.h>
typedef uint64_t u64;
#else
#include <linux/types.h>
#endif

enum { REFILL_QUEUE_TO_WORK, REFILL_WORK_DURATION, REFILL_BOUNDARY_TO_ISSUE,
       REFILL_METRICS };
static const char *const refill_metric_names[REFILL_METRICS] = {
	"queue_to_worker_observation", "worker_duration", "boundary_irq_to_repeat_issue"
};
struct refill_probe_metric {
	u64 valid, missing, reversed, sum_ns, max_ns, bins[6];
	bool sum_saturated;
};
struct refill_probe_stats {
	u64 queue_requests;
	struct refill_probe_metric metric[REFILL_METRICS];
};

/* Caller serializes all accesses. Coalesced notifications keep the earliest
 * timestamp; requeue while work is running can yield an untimed later run.
 * Such runs remain explicit in the missing denominator, never zero latency.
 */
static inline void refill_probe_queue(u64 *pending, u64 now)
{
	if (!*pending)
		*pending = now;
}
static inline u64 refill_probe_take(u64 *pending)
{
	u64 result = *pending;
	*pending = 0;
	return result;
}
static inline void refill_probe_record(struct refill_probe_metric *m,
				       u64 start, u64 end)
{
	u64 delta;
	unsigned int bin;
	if (!start) {
		m->missing++;
		return;
	}
	if (end < start) {
		m->reversed++;
		return;
	}
	delta = end - start;
	m->valid++;
	if (delta > m->max_ns)
		m->max_ns = delta;
	if (~(u64)0 - m->sum_ns < delta) {
		m->sum_ns = ~(u64)0;
		m->sum_saturated = true;
	} else {
		m->sum_ns += delta;
	}
	bin = delta < 50000 ? 0 : delta < 250000 ? 1 :
	      delta < 500000 ? 2 : delta < 1000000 ? 3 :
	      delta < 2000000 ? 4 : 5;
	m->bins[bin]++;
}
#endif
