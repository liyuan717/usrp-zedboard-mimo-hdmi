/* Finite G0 harness, not a production runtime. 544B test events are not an RF wire format. */
#define _POSIX_C_SOURCE 200809L
#include "include/g0_video_fragment.h"
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
struct sink {FILE *file;int error;uint32_t drop_count;};
static uint32_t get32(const uint8_t *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static uint64_t get64(const uint8_t *p){return get32(p)|(uint64_t)get32(p+4)<<32;}
static void put32(uint8_t *p,uint32_t n){for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(n>>(8*i));}
static void put64(uint8_t *p,uint64_t n){put32(p,(uint32_t)n);put32(p+4,(uint32_t)(n>>32));}
static int output(void *opaque,const struct v3vf_info *info,const uint8_t *data)
{
 struct sink *s=opaque;uint8_t h[32]={0};
 if(s->drop_count){s->drop_count--;return 0;}
 put32(h,info->session);put32(h+4,info->unit_id);put32(h+8,info->frame_id);put32(h+12,info->bytes);put64(h+16,info->capture_time_us);put32(h+24,info->duration_us);h[28]=info->type;h[29]=info->flags;
 if(fwrite(h,1,32,s->file)!=32||fwrite(data,1,info->bytes,s->file)!=info->bytes){s->error=1;return 0;}
 return 1;
}
static double now(clockid_t clock){struct timespec t;if(clock_gettime(clock,&t))return 0;return t.tv_sec+t.tv_nsec/1e9;}
int main(int argc,char **argv)
{
 if(argc!=3){fprintf(stderr,"usage: g0-video-fragment-probe EVENTS UNITS\n");return 2;}
 FILE *input=fopen(argv[1],"rb");if(!input)return 3;
 struct sink sink={.file=fopen(argv[2],"wb")};if(!sink.file){fclose(input);return 3;}
 struct v3vf *ctx=v3vf_create();if(!ctx){fclose(input);fclose(sink.file);return 4;}
 uint8_t event[544];size_t count;uint64_t events=0,last_local=0,last_source=0;int result=0,valid=0;
 double wall=now(CLOCK_MONOTONIC),cpu=now(CLOCK_PROCESS_CPUTIME_ID);
 while((count=fread(event,1,sizeof(event),input))!=0){
  if(count!=sizeof(event)||events++>=60000){result=5;break;}
  last_local=get64(event);last_source=get64(event+8);uint32_t session=get32(event+16),command=get32(event+20);valid=get32(event+24)!=0;
  if(get32(event+28)){result=6;break;}
  if(command==0)(void)v3vf_push(ctx,session,event+32,512,last_local,last_source,valid,output,&sink);
  else if(command==1)v3vf_drain(ctx,last_local,last_source,valid,output,&sink);
  else if(command==2)v3vf_session(ctx,session);
  else if(command==3)sink.drop_count=get32(event+32);
  else if(command==4){if(!v3vf_set_ordered(ctx,1)){result=10;break;}}
  else{result=7;break;}
 }
 v3vf_drain(ctx,last_local+V3VF_LOCAL_TTL_US+1,last_source,valid,output,&sink);
 v3vf_expire(ctx,last_local+V3VF_LOCAL_TTL_US+V3VF_REORDER_WAIT_US+1,last_source+V3VF_SOURCE_AGE_US+1,valid);
 struct v3vf_stats s;v3vf_get_stats(ctx,&s);
 wall=now(CLOCK_MONOTONIC)-wall;cpu=now(CLOCK_PROCESS_CPUTIME_ID)-cpu;
 printf("{\"events\":%"PRIu64",\"wall_seconds\":%.9f,\"cpu_seconds\":%.9f,\"context_bytes\":%zu",events,wall,cpu,v3vf_context_bytes());
#define FIELD(n) printf(",\"" #n "\":%"PRIu64,s.n)
 FIELD(accepted_fragments);FIELD(units_completed);FIELD(units_delivered);FIELD(payload_bytes_delivered);FIELD(invalid_fragments);FIELD(duplicate_fragments);FIELD(stale_fragments);FIELD(expired_units);FIELD(evicted_units);FIELD(output_dropped);FIELD(alloc_failures);FIELD(padding_symbols);FIELD(session_changes);FIELD(current_bytes);FIELD(peak_bytes);FIELD(current_units);FIELD(peak_units);
 FIELD(ordered_skipped_units);FIELD(ordered_waits);
 printf("}\n");
 if(ferror(input)||sink.error)result=8;
 v3vf_destroy(ctx);if(fclose(input)||fclose(sink.file))result=9;
 return result;
}
