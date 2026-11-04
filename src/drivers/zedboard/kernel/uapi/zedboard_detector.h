/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/*
 * Stable userspace ABI for the ZedBoard Detector DMA owner.
 *
 * ABI v2 copies the 120-byte task into a driver-private dma_alloc_coherent()
 * buffer and copies the 64-byte result back by ioctl.  It deliberately has no
 * mmap operation: a second process must never mutate an in-flight DMA buffer.
 */
#ifndef _UAPI_ZEDBOARD_DETECTOR_H_
#define _UAPI_ZEDBOARD_DETECTOR_H_

#include <linux/ioctl.h>
#include <linux/types.h>

#define ZED_DETECTOR_ABI_VERSION 2U
#define ZED_DETECTOR_INPUT_BYTES 120U
#define ZED_DETECTOR_OUTPUT_BYTES 64U
#define ZED_DETECTOR_BATCH_MAX_TASKS 60U
#define ZED_DETECTOR_BATCH_TIMEOUT_MAX_MS 60000U

/* info.flags */
#define ZED_DETECTOR_INFO_IRQ_AVAILABLE (1U << 0)
#define ZED_DETECTOR_INFO_POLL_FALLBACK (1U << 1)
#define ZED_DETECTOR_INFO_BATCH_SUBMIT (1U << 2)

struct zed_detector_info {
	__u32 abi_version;
	__u32 input_bytes;
	__u32 output_bytes;
	__u32 build_id;
	__u32 flags;
};

struct zed_detector_submit {
	__u32 input_bytes;   /* must be exactly ZED_DETECTOR_INPUT_BYTES */
	__u32 output_bytes;  /* must be exactly ZED_DETECTOR_OUTPUT_BYTES */
	__u32 timeout_ms;    /* 1..60000; includes both DMA directions */
	__u32 flags;         /* reserved, must be zero */
	__u8 input[ZED_DETECTOR_INPUT_BYTES];
};

struct zed_detector_result {
	__u8 output[ZED_DETECTOR_OUTPUT_BYTES];
};

/*
 * Optional ABI-v2 extension.  This is one owner-locked sequence of ordinary
 * 120-byte/64-byte transactions, not a wider AXI stream or a PL pipeline.
 * `total_timeout_ms` bounds the complete batch; each submit's timeout_ms
 * remains an upper bound for that individual transaction.  All unused submit
 * slots and all result slots must be zero on entry.  Results are copied to
 * userspace only after every requested transaction completed successfully.
 */
struct zed_detector_batch {
	__u32 count;            /* 1..ZED_DETECTOR_BATCH_MAX_TASKS */
	__u32 total_timeout_ms; /* 1..ZED_DETECTOR_BATCH_TIMEOUT_MAX_MS */
	__u32 reserved[2];      /* must be zero */
	struct zed_detector_submit submit[ZED_DETECTOR_BATCH_MAX_TASKS];
	struct zed_detector_result result[ZED_DETECTOR_BATCH_MAX_TASKS];
};

struct zed_detector_status {
	__u32 core_id;
	__u32 core_version;
	__u32 build_id;
	__u32 detector_status;
	__u32 error_code;
	__u32 mm2s_dmasr;
	__u32 s2mm_dmasr;
	__s32 last_error;    /* errno from last SUBMIT/ABORT/RESET */
};

#define ZED_DETECTOR_IOC_MAGIC 'M'
#define ZED_DETECTOR_IOC_GET_INFO \
	_IOR(ZED_DETECTOR_IOC_MAGIC, 0x00, struct zed_detector_info)
#define ZED_DETECTOR_IOC_SUBMIT \
	_IOW(ZED_DETECTOR_IOC_MAGIC, 0x01, struct zed_detector_submit)
#define ZED_DETECTOR_IOC_ABORT _IO(ZED_DETECTOR_IOC_MAGIC, 0x02)
#define ZED_DETECTOR_IOC_RESET _IO(ZED_DETECTOR_IOC_MAGIC, 0x03)
#define ZED_DETECTOR_IOC_GET_STATUS \
	_IOR(ZED_DETECTOR_IOC_MAGIC, 0x04, struct zed_detector_status)
#define ZED_DETECTOR_IOC_GET_RESULT \
	_IOR(ZED_DETECTOR_IOC_MAGIC, 0x05, struct zed_detector_result)
#define ZED_DETECTOR_IOC_SUBMIT_BATCH \
	_IOWR(ZED_DETECTOR_IOC_MAGIC, 0x06, struct zed_detector_batch)

#endif /* _UAPI_ZEDBOARD_DETECTOR_H_ */
