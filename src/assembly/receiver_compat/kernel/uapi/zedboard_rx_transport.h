/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/*
 * RXTH v1 capture ABI.
 *
 * This device is deliberately separate from the AD9361 IIO character devices.
 * A successful read returns exactly one DMA-complete RXTH block.  It does not
 * turn a software delivery sequence into an ADC-continuity claim: consumers
 * must validate the in-band header and its flags.
 */
#ifndef _UAPI_ZEDBOARD_RX_TRANSPORT_H_
#define _UAPI_ZEDBOARD_RX_TRANSPORT_H_

#include <linux/ioctl.h>
#include <linux/types.h>

#define ZED_RXTH_ABI_VERSION             1U
#define ZED_RXTH_FORMAT_VERSION          1U
#define ZED_RXTH_HEADER_BYTES            32U
#define ZED_RXTH_SAMPLES_PER_BLOCK       1024U
#define ZED_RXTH_BYTES_PER_SAMPLE        8U
#define ZED_RXTH_BLOCK_BYTES             8224U
#define ZED_RXTH_TIMEOUT_MAX_MS          60000U

/* zed_rxth_info.flags */
#define ZED_RXTH_INFO_DMAENGINE_OWNER    (1U << 0)
#define ZED_RXTH_INFO_EXCLUSIVE_OPEN     (1U << 1)
#define ZED_RXTH_INFO_FIXED_BLOCK        (1U << 2)
#define ZED_RXTH_INFO_NO_MMAP            (1U << 3)
/* ABORT is retained by the current open owner even when capture is idle.
 * A later read returns -ECANCELED before descriptor refill; close/reopen is
 * the only normal cancellation-clear boundary. */
#define ZED_RXTH_INFO_STICKY_IDLE_ABORT  (1U << 4)

/* zed_rxth_status.state */
#define ZED_RXTH_STATE_IDLE              0U
#define ZED_RXTH_STATE_ACTIVE            1U
#define ZED_RXTH_STATE_POISONED          2U
#define ZED_RXTH_STATE_REMOVING          3U

struct zed_rxth_info {
	__u32 abi_version;
	__u32 format_version;
	__u32 block_bytes;
	__u32 header_bytes;
	__u32 samples_per_block;
	__u32 bytes_per_sample;
	__u32 flags;
	__u32 reserved;
};

/* Per-open timeout.  A timeout aborts the DMA transaction; it never returns
 * a prefix of a transport block. */
struct zed_rxth_timeout {
	__u32 timeout_ms; /* 1..ZED_RXTH_TIMEOUT_MAX_MS */
	__u32 reserved;
};

struct zed_rxth_status {
	__aligned_u64 submitted_blocks;
	__aligned_u64 completed_blocks;
	__aligned_u64 aborted_blocks;
	__aligned_u64 timeout_blocks;
	__aligned_u64 delivery_failures;
	__s32 last_error;
	__u32 state;
	__u32 timeout_ms;
	__u32 reserved;
};

#define ZED_RXTH_IOC_MAGIC 'R'
#define ZED_RXTH_IOC_GET_INFO \
	_IOR(ZED_RXTH_IOC_MAGIC, 0x00, struct zed_rxth_info)
#define ZED_RXTH_IOC_SET_TIMEOUT \
	_IOW(ZED_RXTH_IOC_MAGIC, 0x01, struct zed_rxth_timeout)
#define ZED_RXTH_IOC_GET_STATUS \
	_IOR(ZED_RXTH_IOC_MAGIC, 0x02, struct zed_rxth_status)
#define ZED_RXTH_IOC_ABORT \
	_IO(ZED_RXTH_IOC_MAGIC, 0x03)

#endif /* _UAPI_ZEDBOARD_RX_TRANSPORT_H_ */
