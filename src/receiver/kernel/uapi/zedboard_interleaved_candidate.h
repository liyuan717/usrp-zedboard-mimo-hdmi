/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/*
 * Experimental-only ABI for the interleaved MHGD batch-envelope candidate.
 * This is intentionally a different device and ioctl magic from the released
 * zedboard_detector ABI.  It cannot bind to, or silently fall back to, the
 * legacy per-task detector DMA owner.
 */
#ifndef _UAPI_ZEDBOARD_INTERLEAVED_CANDIDATE_H_
#define _UAPI_ZEDBOARD_INTERLEAVED_CANDIDATE_H_

#include <linux/ioctl.h>
#include <linux/types.h>

#define ZED_INTERLEAVED_CANDIDATE_ABI_VERSION 1U
#define ZED_INTERLEAVED_CANDIDATE_INPUT_BYTES 120U
#define ZED_INTERLEAVED_CANDIDATE_OUTPUT_BYTES 64U
#define ZED_INTERLEAVED_CANDIDATE_MAX_TASKS 528U
#define ZED_INTERLEAVED_CANDIDATE_TIMEOUT_MAX_MS 60000U

#define ZED_INTERLEAVED_CANDIDATE_INFO_BATCH_ENVELOPE (1U << 0)
#define ZED_INTERLEAVED_CANDIDATE_INFO_TAG_REORDER_REQUIRED (1U << 1)
#define ZED_INTERLEAVED_CANDIDATE_INFO_POLL_FALLBACK (1U << 2)
/* ABORT is sticky for the current open owner even when no batch is active.
 * A following SUBMIT returns -ECANCELED before DMA/core start; only a new
 * open owner or a successful RESET clears the latch. */
#define ZED_INTERLEAVED_CANDIDATE_INFO_STICKY_IDLE_ABORT (1U << 3)

struct zed_interleaved_candidate_info {
	__u32 abi_version;
	__u32 input_bytes;
	__u32 output_bytes;
	__u32 max_tasks;
	__u32 build_id;
	__u32 flags;
};

/*
 * The variable-sized payloads live at user pointers solely to avoid an ioctl
 * whose encoded type size exceeds Linux's 14-bit IOC_SIZE field.  The driver
 * copies every input slot into private coherent memory before starting either
 * DMA channel, and copies the entire output only after the one envelope has
 * completed.  No mmap/physical-address ABI is provided.
 */
struct zed_interleaved_candidate_batch {
	__u32 count;       /* 1..MAX_TASKS */
	__u32 timeout_ms;  /* one total envelope budget */
	__u32 flags;       /* must be zero */
	__u32 reserved;    /* must be zero */
	__aligned_u64 input_user_ptr;  /* count * 120 readable bytes */
	__aligned_u64 output_user_ptr; /* count * 64 writable bytes */
};

struct zed_interleaved_candidate_status {
	__u32 core_id;
	__u32 core_version;
	__u32 build_id;
	__u32 candidate_status;
	__u32 candidate_error;
	__u32 mm2s_dmasr;
	__u32 s2mm_dmasr;
	__s32 last_error;
	__u32 recovery_state; /* 0 clean, 1 recovered, 2 poisoned/unknown */
};

#define ZED_INTERLEAVED_CANDIDATE_IOC_MAGIC 'I'
#define ZED_INTERLEAVED_CANDIDATE_IOC_GET_INFO \
	_IOR(ZED_INTERLEAVED_CANDIDATE_IOC_MAGIC, 0x00, struct zed_interleaved_candidate_info)
#define ZED_INTERLEAVED_CANDIDATE_IOC_SUBMIT_BATCH \
	_IOW(ZED_INTERLEAVED_CANDIDATE_IOC_MAGIC, 0x01, struct zed_interleaved_candidate_batch)
#define ZED_INTERLEAVED_CANDIDATE_IOC_ABORT \
	_IO(ZED_INTERLEAVED_CANDIDATE_IOC_MAGIC, 0x02)
#define ZED_INTERLEAVED_CANDIDATE_IOC_RESET \
	_IO(ZED_INTERLEAVED_CANDIDATE_IOC_MAGIC, 0x03)
#define ZED_INTERLEAVED_CANDIDATE_IOC_GET_STATUS \
	_IOR(ZED_INTERLEAVED_CANDIDATE_IOC_MAGIC, 0x04, struct zed_interleaved_candidate_status)

#endif /* _UAPI_ZEDBOARD_INTERLEAVED_CANDIDATE_H_ */
