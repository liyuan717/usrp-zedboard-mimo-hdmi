/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef V3_G0_DMA_RING_H
#define V3_G0_DMA_RING_H
#include <linux/types.h>
#include <linux/ioctl.h>
#define V3_RING_ABI 1U
#define V3_RING_SLOTS 8U
#define V3_RING_SLOT_BYTES 32768U
struct v3_ring_start { __u32 batches; __u32 reserved; };
struct v3_ring_desc {
	__u32 slot, generation, bytes, reserved;
	__aligned_u64 sequence;
};
struct v3_ring_status {
	__u32 abi, slots, slot_bytes, generation;
	__u32 running, poisoned, pinned, error;
	__u32 planned, submitted, completed, ready;
	__u32 queued, user, overflow, bad_keep;
	__aligned_u64 accepted, delivered;
};
#define V3_RING_START _IOW('V', 0x40, struct v3_ring_start)
#define V3_RING_ACQUIRE _IOR('V', 0x41, struct v3_ring_desc)
#define V3_RING_RELEASE _IOW('V', 0x42, struct v3_ring_desc)
#define V3_RING_STATUS _IOR('V', 0x43, struct v3_ring_status)
#define V3_RING_STOP _IO('V', 0x44)
#endif
