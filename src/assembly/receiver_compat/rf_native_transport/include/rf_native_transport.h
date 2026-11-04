/*
 * Native one-RF-frame IMBC transport.
 *
 * This is a C ABI so the RX frontend, a small Python binding, or a future
 * daemon can share the exact same hot path.  It does not tune RF hardware,
 * transmit samples, or own any IIO/RXTH resources.
 */
#ifndef RF_NATIVE_TRANSPORT_H
#define RF_NATIVE_TRANSPORT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
    RNT_TASK_BYTES = 120u,
    RNT_RESULT_BYTES = 64u,
    RNT_TASK_COUNT = 528u,
    RNT_TASK_BUFFER_BYTES = RNT_TASK_BYTES * RNT_TASK_COUNT,
    RNT_PUBLIC_HEADER_BYTES = 20u,
    RNT_PUBLIC_HEADERS_BYTES = 2u * RNT_PUBLIC_HEADER_BYTES,
    RNT_PACKET_BYTES = 152u,
    RNT_PACKETS_BYTES = 2u * RNT_PACKET_BYTES,
};

/* A nonzero return is terminal for that submission.  No prefix of packets is
 * ever returned.  RNT_E_CRC_REJECTED is deliberately separate from DMA/ioctl
 * failures so a receiver can count air-interface corruption accurately. */
typedef enum rnt_code {
    RNT_OK = 0,
    RNT_E_ARGUMENT = 1,
    RNT_E_STATE = 2,
    RNT_E_BUSY = 3,
    RNT_E_ABORTED = 4,
    RNT_E_IO = 5,
    RNT_E_PROTOCOL = 6,
    RNT_E_DEVICE = 7,
    RNT_E_CRC_REJECTED = 8,
    RNT_E_INTERNAL = 9,
} rnt_code;

/* All values are mandatory and deliberate.  expected_build_id must match the
 * IMBC info reply and every MOP0 result, preventing stale bitstream output.
 * rf_frame_id is the public MRF identifier, not the per-symbol MIP0 tag. */
typedef struct rnt_submit_config {
    uint32_t rf_frame_id;
    uint32_t expected_build_id;
    uint32_t timeout_ms;              /* 1..60000, one whole envelope budget */
    uint32_t required_config_id;
    uint32_t required_iterations;
    int32_t required_proposal_step_raw;
} rnt_submit_config;

typedef struct rnt_result_metadata {
    uint32_t build_id;
    uint32_t actual_iterations;
    uint32_t result_count;
    uint32_t reserved;
    /* Sum of legacy per-result compatibility counters only.  It is not an
     * interleaved-core wall-time or a valid throughput/FPS measurement. */
    uint64_t total_pl_cycles;
    /* Nonfatal MOP0 diagnostic counters.  They are aggregated only after
     * every result has passed all required MOP0 validation. */
    uint64_t total_overflow_count;
    uint64_t total_saturation_count;
} rnt_result_metadata;

typedef struct rnt_frame_result {
    uint8_t packets[RNT_PACKETS_BYTES]; /* stream 0 152 B then stream 1 152 B */
    rnt_result_metadata metadata;
} rnt_frame_result;

/* Injectable only for deterministic unit tests or a controlled integration
 * shim.  Return 0 for success or a negative errno-style value.  Production
 * callers pass NULL and receive the Linux /dev ioctl implementation. */
typedef struct rnt_io_vtable {
    void *context;
    int (*open)(void *context, const char *path, int *out_fd);
    int (*close)(void *context, int fd);
    int (*ioctl)(void *context, int fd, unsigned long request,
                 void *arg, size_t arg_bytes);
} rnt_io_vtable;

typedef struct rnt_transport rnt_transport;

rnt_code rnt_open(const char *device_path, const rnt_io_vtable *injected_io,
                  rnt_transport **out_transport);
rnt_code rnt_status(rnt_transport *transport);

/* task_bytes must contain exactly 528 contiguous MIP0 records, while
 * public_headers contains MRF stream0 then stream1 public headers.  The two
 * headers are checked before the single submit ioctl. */
rnt_code rnt_submit_mrf_frame(rnt_transport *transport,
                              const uint8_t *task_bytes, size_t task_bytes_len,
                              const uint8_t *public_headers,
                              size_t public_headers_len,
                              const rnt_submit_config *config,
                              rnt_frame_result *out_result);

/* Safe to call concurrently with submit: it never takes submit's operation
 * mutex. A successful call permanently cancels this open owner; do not submit
 * again until close/reopen. The required driver sticky-ABORT capability, not
 * this library's local latch, closes the kernel pre-submit race. */
rnt_code rnt_abort(rnt_transport *transport);

/* Waits for already-entered ioctls; callers must stop creating new work before
 * close.  It never attempts to send a new RF/DMA job. */
rnt_code rnt_close(rnt_transport *transport);
void rnt_destroy(rnt_transport *transport);

const char *rnt_code_string(rnt_code code);

#ifdef __cplusplus
}
#endif

#endif
