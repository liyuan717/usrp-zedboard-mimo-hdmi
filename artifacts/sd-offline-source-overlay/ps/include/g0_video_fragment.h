#ifndef V3_G0_VIDEO_FRAGMENT_H
#define V3_G0_VIDEO_FRAGMENT_H
#include <stddef.h>
#include <stdint.h>
/* Unfrozen finite G0 candidate. SOURCE CRC/FEC and session admission belong upstream.
 * Complete unit callbacks borrow immutable bytes only until they return. The
 * consumer copies/accepts or drops promptly; callback0 means bounded output drop.
 * No AU completeness rule or wait loop is part of this adapter. */
#define V3VF_SYMBOL_BYTES 512U
#define V3VF_UNIT_MAX_BYTES (1024U*1024U)
#define V3VF_CACHE_MAX_BYTES (2U*1024U*1024U)
#define V3VF_MAX_UNITS 32U
#define V3VF_REORDER_UNITS 64U
#define V3VF_LOCAL_TTL_US 500000ULL
#define V3VF_SOURCE_AGE_US 3000000ULL
#define V3VF_REORDER_WAIT_US 200000ULL
struct v3vf;
struct v3vf_info {
 uint32_t session,unit_id,frame_id,bytes;
 uint64_t capture_time_us;
 uint32_t duration_us;
 uint8_t type,flags;
};
struct v3vf_stats {
 uint64_t accepted_fragments,units_completed,units_delivered,payload_bytes_delivered;
 uint64_t invalid_fragments,duplicate_fragments,stale_fragments,expired_units,evicted_units;
 uint64_t output_dropped,alloc_failures,padding_symbols,session_changes;
 uint64_t current_bytes,peak_bytes,current_units,peak_units;
 uint64_t ordered_skipped_units,ordered_waits;
};
typedef int (*v3vf_deliver)(void *,const struct v3vf_info *,const uint8_t *);
struct v3vf *v3vf_create(void);
void v3vf_destroy(struct v3vf *);
/* Called by the owner on an admitted session boundary, never by each packet. */
void v3vf_session(struct v3vf *,uint32_t);
/* Configure while empty. Production video uses ordered=1; no second payload
 * queue is allocated. Completed NALs retain their existing cache ownership. */
int v3vf_set_ordered(struct v3vf *,int ordered);
/* Periodic tick, including idle input. A known incomplete NAL may be skipped
 * once it and a later complete NAL have each waited200ms. Without later useful
 * content it retains500ms idle TTL. Unknown holes wait at most200ms from the
 * nearest later unit's arrival. expired_units includes ordering deadlines.
 * At most32 callbacks per call, without waiting for a complete AU. */
void v3vf_drain(struct v3vf *,uint64_t local_us,uint64_t source_us,int source_clock_valid,v3vf_deliver,void *);
/* Periodic monotonic tick also expires incomplete units when input goes idle.
 * source_clock_valid requires an independently established source time mapping;
 * otherwise only local TTL applies, and end-to-end source age is not verified. */
void v3vf_expire(struct v3vf *,uint64_t local_us,uint64_t source_us,int source_clock_valid);
/* Packet-local outcomes never request process termination or block for missing data. */
int v3vf_push(struct v3vf *,uint32_t source_session,const uint8_t *,size_t,
 uint64_t local_us,uint64_t source_us,int source_clock_valid,v3vf_deliver,void *);
void v3vf_get_stats(const struct v3vf *,struct v3vf_stats *);
size_t v3vf_context_bytes(void);
#endif
