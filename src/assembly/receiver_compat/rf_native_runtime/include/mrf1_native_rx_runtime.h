/* Runtime contract for the independent RX-only executable.
 *
 * Capture owns /dev/zed-rxth, frontend owns one mrf1_nf_frontend, batch owns
 * one rnt_transport. The frontend writes directly into a caller-owned,
 * preallocated frame slot. That slot is returned to the pool only after
 * batch/delivery completes; a subsequent feed cannot overwrite queued work.
 * Kernel/user DMA copies still exist: this is not end-to-end zero-copy.
 */
#ifndef MRF1_NATIVE_RX_RUNTIME_H
#define MRF1_NATIVE_RX_RUNTIME_H

#define MRF1_NATIVE_RUNTIME_SCHEMA "mrf1-native-rx-backend-v1"
#define MRF1_NATIVE_RUNTIME_PROTOCOL_COMMIT "8021db6c59a77e65bebbedabca961ac82c90ab70"
#define MRF1_NATIVE_RUNTIME_PIPELINE_RXTH_BYTES 8224u
#define MRF1_NATIVE_RUNTIME_PIPELINE_TASK_BYTES 120u
#define MRF1_NATIVE_RUNTIME_PIPELINE_RESULT_BYTES 64u
#define MRF1_NATIVE_RUNTIME_PIPELINE_TASKS 528u

#endif
