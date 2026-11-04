#define _POSIX_C_SOURCE 200809L
#include "crc32_slice8_candidate.h"
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <zlib.h>

#define IMAGE_BYTES 1228800U
#define BUFFER_BYTES (4194304U+32U)
#define ITERATIONS 32U
static volatile uint32_t sink;
static uint64_t now_ns(clockid_t clock) {
    struct timespec ts;
    if (clock_gettime(clock, &ts)) { perror("clock_gettime"); exit(2); }
    return (uint64_t)ts.tv_sec*UINT64_C(1000000000)+(uint64_t)ts.tv_nsec;
}
static int verify(const struct crc32_slice8_context *ctx, const uint8_t *data) {
    const size_t sizes[] = {0,1,2,7,8,9,15,16,28,31,32,255,256,4096,IMAGE_BYTES,4194304};
    unsigned off, n;
    if (crc32_slice8(ctx, NULL, 0) != 0 ||
        crc32_slice8(ctx, (const uint8_t *)"123456789", 9) != UINT32_C(0xcbf43926)) return 1;
    for (off=0; off<32; ++off)
        for (n=0; n<258; ++n)
            if (crc32_slice8(ctx,data+off,n) != (uint32_t)crc32(0L,data+off,n)) return 2;
    for (off=0; off<8; ++off)
        for (n=0; n<sizeof(sizes)/sizeof(sizes[0]); ++n)
            if (crc32_slice8(ctx,data+off,sizes[n]) != (uint32_t)crc32(0L,data+off,(uInt)sizes[n])) return 3;
    return 0;
}
static void sample(const struct crc32_slice8_context *ctx, const uint8_t *data,
                   unsigned round, int candidate) {
    uint64_t wall, cpu, wall_end, cpu_end;
    uint32_t checksum=0;
    unsigned i;
    wall=now_ns(CLOCK_MONOTONIC); cpu=now_ns(CLOCK_PROCESS_CPUTIME_ID);
    for (i=0; i<ITERATIONS; ++i) {
        /* Prevent loop-invariant hoisting; never alter the measured payload. */
        __asm__ __volatile__("" : : : "memory");
        checksum += candidate ? crc32_slice8(ctx,data+(i&7U),IMAGE_BYTES) :
                                (uint32_t)crc32(0L,data+(i&7U),IMAGE_BYTES);
    }
    cpu_end=now_ns(CLOCK_PROCESS_CPUTIME_ID); wall_end=now_ns(CLOCK_MONOTONIC);
    sink=checksum;
    printf("{\"round\":%u,\"implementation\":\"%s\",\"images\":%u,\"bytes_per_image\":%u,"
           "\"wall_ns\":%" PRIu64 ",\"cpu_ns\":%" PRIu64 ",\"checksum\":%u}\n",
           round,candidate?"slice8":"zlib",ITERATIONS,IMAGE_BYTES,wall_end-wall,cpu_end-cpu,checksum);
}
int main(int argc, char **argv) {
    struct crc32_slice8_context ctx;
    uint8_t *data=malloc(BUFFER_BYTES);
    uint32_t random=UINT32_C(0x19ab82d7);
    unsigned i, round;
    int rc;
    if (!data) return 2;
    for (i=0;i<BUFFER_BYTES;++i) {
        random ^= random<<13; random ^= random>>17; random ^= random<<5;
        data[i]=(uint8_t)random;
    }
    crc32_slice8_init(&ctx);
    rc=verify(&ctx,data);
    if (rc) {fprintf(stderr,"CRC_CANDIDATE_VERIFY_FAILED %d\n",rc);free(data);return 1;}
    puts("CRC_CANDIDATE_EQUIVALENCE_PASS offsets=32 small_lengths=258 large_lengths=16 known_vectors=2");
    if (argc==2 && !strcmp(argv[1],"--verify-only")) {free(data);return 0;}
    if (argc!=1) {free(data);return 2;}
    for (round=0;round<4;++round) {
        sample(&ctx,data,round,(int)(round&1U));
        sample(&ctx,data,round,(int)!(round&1U));
    }
    free(data);
    return 0;
}
