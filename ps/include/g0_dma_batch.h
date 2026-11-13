/* G0 transport candidate, not frozen production SESSION/timebase ABI. */
#ifndef V3_G0_DMA_BATCH_H
#define V3_G0_DMA_BATCH_H
#include <stddef.h>
#include <stdint.h>
#define V3B_BYTES 32768U
enum v3b_result { V3B_BAD_METADATA=1, V3B_DUPLICATE=2, V3B_SEQUENCE_GAP=4,
 V3B_DAMAGED_PREFIX=8, V3B_BAD_RECORD=16, V3B_LATE_BATCH=32 };
struct v3b_reader {
 uint64_t batches,records,bad_metadata,bad_records,duplicate_batches,late_batches,
  missing_batches,damaged_batches,unknown_records,consumer_dropped,last_sequence;
 unsigned seen_sequence;
};
/* Callback is bounded and synchronous: copy/enqueue or retain the DMA USER
 * lease before returning. A record pointer cannot outlive release of its batch.
 * Return0 to reject the current record without stopping later reception. */
typedef int (*v3b_record_fn)(void *,const uint8_t *,uint32_t,uint16_t);
/* While holding an immutable complete DMA USER batch, snapshot its fixed
 * header/footer and bounded record region into a private V3B_BYTES buffer.
 * Padding is intentionally unspecified. v3b_consume still validates metadata
 * and records after RELEASE; no mmap pointer survives that release.
 * Return the bytes copied, or0 for an invalid buffer size/null pointer. */
size_t v3b_copy_owned(void *,const void *,size_t);
unsigned v3b_consume(struct v3b_reader *,const void *,size_t,v3b_record_fn,void *);
#endif
