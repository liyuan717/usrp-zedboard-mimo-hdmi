/* Finite file adapter for component/decoder tests, not the production runtime. */
#include "include/g0_video_au.h"
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>
#include <string.h>
static uint32_t u32(const uint8_t *p){return p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static uint64_t u64(const uint8_t *p){return u32(p)|(uint64_t)u32(p+4)<<32;}
static void put32(uint8_t *p,uint32_t n){for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(n>>(8*i));}
static void put64(uint8_t *p,uint64_t n){put32(p,(uint32_t)n);put32(p+4,(uint32_t)(n>>32));}
struct sink {FILE *f;unsigned error,drop;};
static int output(void *opaque,const struct v3au_info *a,const uint8_t *p)
{
 struct sink *s=opaque;uint8_t h[32]={0};memcpy(h,"V3AU",4);h[4]=1;h[6]=32;put64(h+8,((uint64_t)a->session<<32)|a->frame_id);put64(h+16,a->capture_time_us);put32(h+24,a->bytes);put32(h+28,a->damaged);
 if(s->drop){s->drop--;return 0;}
 if(fwrite(h,1,32,s->f)!=32||fwrite(p,1,a->bytes,s->f)!=a->bytes){s->error=1;return 0;}return 1;
}
int main(int argc,char **argv)
{
 if(argc!=3&&argc!=4)return 2;
 FILE *input=fopen(argv[1],"rb");struct sink sink={.f=fopen(argv[2],"wb")};if(!input||!sink.f)return 3;
 if(argc==4){char *end;unsigned long n=strtoul(argv[3],&end,10);if(*end||n>4096)return 2;sink.drop=(unsigned)n;}
 struct v3au *c=v3au_create();uint8_t *data=malloc(V3AU_MAX_BYTES),h[32];if(!c||!data)return 4;
 uint64_t local=0;uint32_t session=0;unsigned have=0,count=0;int error=0;size_t bytes;
 while((bytes=fread(h,1,32,input))){
  if(bytes!=32||count++>=8192){error=5;break;}
  struct v3vf_info n={.session=u32(h),.unit_id=u32(h+4),.frame_id=u32(h+8),.bytes=u32(h+12),.capture_time_us=u64(h+16),.duration_us=u32(h+24),.type=h[28],.flags=h[29]};
  if(!n.bytes||n.bytes>V3AU_MAX_BYTES||h[30]||h[31]||fread(data,1,n.bytes,input)!=n.bytes){error=6;break;}
  if(!have||session!=n.session){session=n.session;have=1;v3au_session(c,session);}
  local=local+10>n.capture_time_us?local+10:n.capture_time_us;
  (void)v3au_push(c,&n,data,local,output,&sink);
 }
 v3au_tick(c,local+V3AU_IDLE_US+1,output,&sink);
 struct v3au_stats s;v3au_get_stats(c,&s);
 printf("{\"units\":%u,\"context_bytes\":%zu",count,v3au_context_bytes());
#define FIELD(n) printf(",\"" #n "\":%"PRIu64,s.n)
 FIELD(submitted);FIELD(damaged);FIELD(no_vcl);FIELD(invalid);FIELD(stale);FIELD(overflow);FIELD(output_dropped);FIELD(session_dropped);FIELD(peak_bytes);printf("}\n");
 if(ferror(input)||sink.error)error=7;
 v3au_destroy(c);free(data);if(fclose(input)||fclose(sink.f))error=8;return error;
}
