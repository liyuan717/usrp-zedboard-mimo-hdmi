/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef V3_G0_SOURCE_RING_H
#define V3_G0_SOURCE_RING_H
#include <linux/types.h>
#include <linux/ioctl.h>
#define V3_SOURCE_RING_ABI 1U
#define V3_SOURCE_RING_SLOTS 8U
#define V3_SOURCE_RING_SLOT_BYTES 32768U
/* Finite G0 observation, ~67 seconds at the current batch cadence. The
 * coherent allocation stays eight USER slots plus one STOP drain slot. */
#define V3_SOURCE_RING_MAX_BATCHES 65536U
struct v3_source_ring_start { __u32 batches; __u32 reserved; };
struct v3_source_ring_desc {
	__u32 slot, generation, bytes, reserved;
	__aligned_u64 sequence;
};
/* planned is zero for continuous START. submitted/completed wrap at 32 bits.
 * accepted counts whole SOURCE records; delivered counts32KiB batches. */
struct v3_source_ring_status {
	__u32 abi, slots, slot_bytes, generation;
	__u32 running, poisoned, pinned, error;
	__u32 planned, submitted, completed, ready;
	__u32 queued, user, ingress_faults, rejected_records;
	__aligned_u64 accepted_sources, delivered_batches;
};
/* Optional clock snapshot. MONOTONIC bounds enclose the atomic low read.
 * adc_tag has epoch16/sample48 and age is in PL processing cycles. */
struct v3_source_ring_clock {
	__aligned_u64 before_ns, after_ns, adc_tag;
	__u32 age_cycles, reserved;
};
#define V3_SOURCE_RING_START _IOW('S', 0x40, struct v3_source_ring_start)
#define V3_SOURCE_RING_ACQUIRE _IOR('S', 0x41, struct v3_source_ring_desc)
#define V3_SOURCE_RING_RELEASE _IOW('S', 0x42, struct v3_source_ring_desc)
#define V3_SOURCE_RING_STATUS _IOR('S', 0x43, struct v3_source_ring_status)
#define V3_SOURCE_RING_STOP _IO('S', 0x44)
#define V3_SOURCE_RING_CLOCK _IOR('S', 0x45, struct v3_source_ring_clock)
#define V3_SOURCE_RING_START_CONTINUOUS _IO('S', 0x46)
#endif
