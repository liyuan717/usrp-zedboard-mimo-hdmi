/*
 * Native bounded MRF1 receive front end.
 *
 * This is intentionally a detector-input producer.  It never uses TX payload
 * truth, never runs a software ML/ZF payload fallback, and never accepts a
 * packet based on a relaxed CRC.  A separately owned native batch transport
 * consumes the `task_bytes` buffer and reports MOP0/packet CRC outcomes.
 */
#ifndef MRF1_NATIVE_FRONTEND_H
#define MRF1_NATIVE_FRONTEND_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum {
  MRF1_RXTH_HEADER_BYTES = 32,
  MRF1_RXTH_BLOCK_SAMPLES = 1024,
  MRF1_RXTH_BLOCK_BYTES = MRF1_RXTH_HEADER_BYTES + MRF1_RXTH_BLOCK_SAMPLES * 8,
  MRF1_MRF_HEADER_BYTES = 20,
  MRF1_TASK_BYTES = 120,
  MRF1_TASK_COUNT = 528,
  MRF1_BATCH_TASK_BYTES = MRF1_TASK_COUNT * MRF1_TASK_BYTES,
};

typedef enum mrf1_nf_status {
  MRF1_NF_OK = 0,
  MRF1_NF_NEED_MORE = 1,
  MRF1_NF_REACQUIRING = 2,
  MRF1_NF_NO_FRAME = 3,
  MRF1_NF_ARGUMENT = -1,
  MRF1_NF_TRANSPORT = -2,
  MRF1_NF_UNSUPPORTED = -3,
  MRF1_NF_ACQUISITION = -4,
  MRF1_NF_CHANNEL = -5,
  MRF1_NF_HEADER = -6,
  MRF1_NF_QUANTIZATION = -7,
  MRF1_NF_CAPACITY = -8,
  MRF1_NF_INTERNAL = -9,
} mrf1_nf_status;

typedef struct mrf1_nf_config {
  /* Frozen MRF1-QPSK-2X2-4M-v1 profile. Zero selects each default. */
  uint32_t sample_rate_hz;        /* 4,000,000 */
  uint32_t samples_per_symbol;    /* 4 */
  uint32_t task_iterations;       /* e.g. 64; must be 1..65535 */
  uint32_t config_id;             /* e.g. 2; must be nonzero */
  uint32_t proposal_step_raw;     /* profile-0 MHGD acceptance threshold */
  uint32_t max_buffer_samples;    /* 4096..262144; zero -> 8192 */
  double repetition_threshold;    /* zero -> 0.55 */
  double preamble_threshold;      /* zero -> 0.75 */
  double max_channel_condition;   /* zero -> 1000 */
} mrf1_nf_config;

typedef struct mrf1_nf_provenance {
  uint32_t epoch;
  uint32_t first_sequence;
  uint64_t absolute_first_sample;
  uint64_t absolute_end_exclusive;
  uint32_t frame_id;
  uint32_t flags; /* bit0=absolute coordinates proven; bit1=initial acquisition */
  double cfo_hz;
  double timing_offset_samples;
  double sfo_ppm;
} mrf1_nf_provenance;

/* Output memory is owned by `mrf1_nf_frontend`; valid until next feed/reset/destroy. */
typedef struct mrf1_nf_batch_view {
  mrf1_nf_provenance provenance;
  const uint8_t *headers;   /* exactly MRF1_MRF_HEADER_BYTES * 2, stream0 then stream1 */
  const uint8_t *task_bytes;/* exactly MRF1_BATCH_TASK_BYTES: 528 contiguous MIP0 frames */
  size_t header_bytes;
  size_t task_bytes_len;
} mrf1_nf_batch_view;

/*
 * Production runtime allocates a finite ring of these slots.  The front end
 * writes directly into the acquired slot, then the runtime lends it to the
 * native batch transport.  It may not be reused until that transport returns.
 */
typedef struct mrf1_nf_output_slot {
  uint8_t *headers;       /* capacity >= 40 */
  size_t headers_capacity;
  uint8_t *task_bytes;   /* capacity >= 63360 */
  size_t task_bytes_capacity;
} mrf1_nf_output_slot;

typedef struct mrf1_nf_stats {
  uint64_t rxth_blocks;
  uint64_t rejected_blocks;
  uint64_t continuity_resets;
  uint64_t acquisition_attempts;
  uint64_t acquisition_failures;
  uint64_t emitted_batches;
  uint64_t buffered_samples;
  uint32_t current_epoch;
  uint32_t state; /* 0 searching, 1 locked */
} mrf1_nf_stats;

/* Zero unless compiled with MRF1_NF_ENABLE_PROFILE. These host-side counters
 * identify work partitions; they are not a board-time or real-time claim. */
typedef struct mrf1_nf_profile {
  uint64_t acquisition_ns;
  uint64_t nco_ns;
  uint64_t rrc_ns;
  uint64_t training_header_ns;
  uint64_t pilot_task_ns;
  uint64_t completed_frames;
  uint64_t repetition_candidates;
  uint64_t preamble_refinement_candidates;
  uint64_t header_rejects;
  uint64_t tracking_rejects;
} mrf1_nf_profile;

/* Diagnostic snapshot for test-oracle comparison only; not a detector result. */
typedef struct mrf1_nf_debug_snapshot {
  double cfo_hz;
  double timing_offset_samples;
  double sfo_ppm;
  double training_h_re_im_rowmajor[8];
  double first_detector_y_re_im[4];
  double first_pilot_phase_radians;
} mrf1_nf_debug_snapshot;

typedef struct mrf1_nf_frontend mrf1_nf_frontend;

mrf1_nf_status mrf1_nf_create(const mrf1_nf_config *config, mrf1_nf_frontend **out);
void mrf1_nf_destroy(mrf1_nf_frontend *frontend);
/* One caller owns a frontend. A malformed input poisons it; create a new one. */
mrf1_nf_status mrf1_nf_feed(mrf1_nf_frontend *frontend, const uint8_t *data,
                            size_t data_len, mrf1_nf_batch_view *out_batch);
/* Direct-to-slot production form; never copies a completed task batch. */
mrf1_nf_status mrf1_nf_feed_into(mrf1_nf_frontend *frontend, const uint8_t *data,
                                 size_t data_len, const mrf1_nf_output_slot *slot,
                                 mrf1_nf_batch_view *out_batch);
mrf1_nf_status mrf1_nf_finish(mrf1_nf_frontend *frontend, mrf1_nf_batch_view *out_batch);
void mrf1_nf_reset(mrf1_nf_frontend *frontend);
void mrf1_nf_get_stats(const mrf1_nf_frontend *frontend, mrf1_nf_stats *out_stats);
void mrf1_nf_get_profile(const mrf1_nf_frontend *frontend, mrf1_nf_profile *out_profile);
void mrf1_nf_get_debug_snapshot(const mrf1_nf_frontend *frontend,
                                mrf1_nf_debug_snapshot *out_snapshot);
const char *mrf1_nf_status_string(mrf1_nf_status status);

/* Exact task-wire helper, independently unit tested against ps.common.protocol. */
mrf1_nf_status mrf1_nf_pack_q16_task(
    uint32_t frame_id, uint32_t config_id, uint16_t iterations, uint32_t seed,
    uint32_t proposal_step_raw, const double *h_re_im_rowmajor_2x2,
    const double *y_re_im_2, uint8_t out_task[MRF1_TASK_BYTES]);

#ifdef __cplusplus
}
#endif
#endif
