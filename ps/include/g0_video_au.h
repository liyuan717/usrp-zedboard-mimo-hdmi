#ifndef V3_G0_VIDEO_AU_H
#define V3_G0_VIDEO_AU_H
#include "g0_video_fragment.h"
#define V3AU_MAX_BYTES (1024U*1024U)
#define V3AU_IDLE_US 500000ULL
#define V3AU_MAX_HOLD_US 1000000ULL
struct v3au;
struct v3au_info {uint32_t session,frame_id,bytes,damaged,missing_units,capture_uncertainty_us;uint64_t capture_time_us;};
struct v3au_stats {uint64_t submitted,damaged,no_vcl,invalid,stale,overflow,output_dropped,session_dropped,peak_bytes;};
typedef int (*v3au_deliver)(void *,const struct v3au_info *,const uint8_t *);
/* Borrowed until callback returns: copy/queue or return0 promptly. Never block
 * the DMA owner on the decoder/display; a full output queue is a local drop. */
struct v3au *v3au_create(void);
void v3au_destroy(struct v3au *);
size_t v3au_context_bytes(void);
void v3au_session(struct v3au *,uint32_t);
/* Takes ordered COMPLETE NALs, not all NALs in an AU. A next frame, AU-end,
 * or deadline submits the useful partial AU. Idle is since last accepted NAL;
 * the absolute hold bound cannot be prolonged by slow or invalid input.
 * No decoder reset on loss. */
int v3au_push(struct v3au *,const struct v3vf_info *,const uint8_t *,uint64_t local_us,v3au_deliver,void *);
void v3au_tick(struct v3au *,uint64_t local_us,v3au_deliver,void *);
void v3au_get_stats(const struct v3au *,struct v3au_stats *);
#endif
