#include "include/g0_video_fragment.h"
#include <stdlib.h>
#include <string.h>
struct unit {
 struct v3vf_info info;
 uint8_t *storage,*bitmap;
 size_t allocation;
 uint64_t first_seen;
 uint32_t count,received;
};
struct v3vf {
 struct unit units[V3VF_MAX_UNITS];
 struct v3vf_stats stats;
 uint32_t session,latest,history[V3VF_REORDER_UNITS];
 unsigned session_valid,latest_valid,history_count,history_next;
 uint32_t next_id;
 uint64_t order_start;
 unsigned ordered,order_have,order_started;
};
static uint16_t get16(const uint8_t *p){return (uint16_t)p[0]|(uint16_t)p[1]<<8;}
static uint32_t get32(const uint8_t *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static uint64_t get64(const uint8_t *p){return (uint64_t)get32(p)|(uint64_t)get32(p+4)<<32;}
static void release_unit(struct v3vf *c,struct unit *u)
{
 if(!u->storage)return;
 c->stats.current_bytes-=u->allocation;c->stats.current_units--;
 free(u->storage);memset(u,0,sizeof(*u));
}
struct v3vf *v3vf_create(void){return calloc(1,sizeof(struct v3vf));}
void v3vf_destroy(struct v3vf *c)
{
 if(!c)return;
 for(unsigned i=0;i<V3VF_MAX_UNITS;i++)release_unit(c,&c->units[i]);
 free(c);
}
size_t v3vf_context_bytes(void){return sizeof(struct v3vf);}
void v3vf_session(struct v3vf *c,uint32_t session)
{
 if(c->session_valid&&c->session==session)return;
 for(unsigned i=0;i<V3VF_MAX_UNITS;i++)release_unit(c,&c->units[i]);
 c->session=session;c->session_valid=1;c->latest_valid=0;c->history_count=0;c->history_next=0;c->order_have=0;c->order_started=0;c->stats.session_changes++;
}
int v3vf_set_ordered(struct v3vf *c,int ordered)
{
 if(c->stats.current_units)return 0;
 c->ordered=ordered!=0;c->order_have=0;c->order_started=0;return 1;
}
static int source_old(uint64_t capture,uint64_t now,int valid)
{return valid&&now>=capture&&now-capture>V3VF_SOURCE_AGE_US;}
void v3vf_expire(struct v3vf *c,uint64_t local,uint64_t source,int valid)
{
 for(unsigned i=0;i<V3VF_MAX_UNITS;i++){
  struct unit *u=&c->units[i];
  uint64_t ttl=V3VF_LOCAL_TTL_US+((c->ordered&&u->received==u->count)?V3VF_REORDER_WAIT_US:0);
  if(u->storage&&((local>=u->first_seen&&local-u->first_seen>=ttl)||source_old(u->info.capture_time_us,source,valid))){
   c->stats.expired_units++;release_unit(c,u);
  }
 }
}
static void publish(struct v3vf *c,struct unit *u,v3vf_deliver deliver,void *opaque)
{
 if(deliver&&deliver(opaque,&u->info,u->storage)>0){c->stats.units_delivered++;c->stats.payload_bytes_delivered+=u->info.bytes;}
 else c->stats.output_dropped++;
 c->history[c->history_next]=u->info.unit_id;c->history_next=(c->history_next+1U)%V3VF_REORDER_UNITS;
 if(c->history_count<V3VF_REORDER_UNITS)c->history_count++;
 release_unit(c,u);
}
void v3vf_drain(struct v3vf *c,uint64_t local,uint64_t source,int valid,v3vf_deliver deliver,void *opaque)
{
 v3vf_expire(c,local,source,valid);
 if(!c->ordered||!c->order_have)return;
 if(!c->order_started&&(local<c->order_start||local-c->order_start<V3VF_REORDER_WAIT_US)){c->stats.ordered_waits++;return;}
 c->order_started=1;
 for(unsigned turn=0;turn<V3VF_MAX_UNITS;turn++){
  struct unit *nearest=NULL;uint32_t distance=0x80000000U;
  for(unsigned i=0;i<V3VF_MAX_UNITS;i++){
   struct unit *u=&c->units[i];uint32_t d=u->info.unit_id-c->next_id;
   if(u->storage&&d<distance){distance=d;nearest=u;}
  }
  if(!nearest)return;
  if(distance){
   if(local<nearest->first_seen||local-nearest->first_seen<V3VF_REORDER_WAIT_US){c->stats.ordered_waits++;return;}
   c->stats.ordered_skipped_units+=distance;c->next_id=nearest->info.unit_id;
  }
  /* A partial NAL must not hold useful later complete NALs for the whole
   * cache TTL. Both have had a bounded reorder opportunity before skipping.
   * With no later complete content, retain the ordinary500ms idle TTL. */
  if(nearest->received!=nearest->count){
   int later_due=0;
   if(local>=nearest->first_seen&&local-nearest->first_seen>=V3VF_REORDER_WAIT_US)
    for(unsigned i=0;i<V3VF_MAX_UNITS;i++){
     struct unit *u=&c->units[i];uint32_t d=u->info.unit_id-nearest->info.unit_id;
     if(u->storage&&d&&d<0x80000000U&&u->received==u->count&&
        local>=u->first_seen&&local-u->first_seen>=V3VF_REORDER_WAIT_US){later_due=1;break;}
    }
   if(!later_due){c->stats.ordered_waits++;return;}
   c->stats.expired_units++;c->stats.ordered_skipped_units++;c->next_id++;
   release_unit(c,nearest);continue;
  }
  c->order_started=1;c->next_id++;
  publish(c,nearest,deliver,opaque);
 }
}
static struct unit *oldest(struct v3vf *c)
{
 struct unit *result=NULL;
 for(unsigned i=0;i<V3VF_MAX_UNITS;i++){
  struct unit *u=&c->units[i];
  if(u->storage&&(!result||u->first_seen<result->first_seen))result=u;
 }
 return result;
}
static int invalid(struct v3vf *c){c->stats.invalid_fragments++;return -1;}
int v3vf_push(struct v3vf *c,uint32_t session,const uint8_t *p,size_t bytes,
 uint64_t local,uint64_t source,int valid,v3vf_deliver deliver,void *opaque)
{
 v3vf_expire(c,local,source,valid);
 if(!c->session_valid||session!=c->session){c->stats.stale_fragments++;return 0;}
 if(bytes!=V3VF_SYMBOL_BYTES||!p)return invalid(c);
 uint32_t frame=get32(p),id=get32(p+12),total=get32(p+16),offset=get32(p+20),duration=get32(p+32);
 uint64_t capture=get64(p+4);
 unsigned length=get16(p+24),index=get16(p+26),count=get16(p+28),type=p[30],flags=p[31];
 if(type==0){
  for(size_t i=0;i<bytes;i++)if(p[i])return invalid(c);
  c->stats.padding_symbols++;return 0;
 }
 if((type!=1&&type!=5&&type!=6&&type!=7&&type!=8&&type!=9)||!total||total>V3VF_UNIT_MAX_BYTES||duration!=50000||get32(p+36)||
    (flags&~7U)||((flags&4U)!=((type==5)?4U:0U))||!count||index>=count||count!=(total/472U+(total%472U!=0))||
    offset!=(uint32_t)index*472U||offset>total||length>472U||length!=((total-offset>472U)?472U:total-offset)||
    ((flags&1U)&&index!=0)||((flags&2U)&&index+1U!=count))return invalid(c);
 if(source_old(capture,source,valid)){c->stats.stale_fragments++;return 0;}
 if(c->ordered&&c->order_started&&id-c->next_id>=0x80000000U){c->stats.stale_fragments++;return 0;}
 for(unsigned i=0;i<c->history_count;i++)if(c->history[i]==id){c->stats.duplicate_fragments++;return 0;}
 if(c->latest_valid){
  uint32_t behind=c->latest-id;
  if(behind>=V3VF_REORDER_UNITS&&behind<0x80000000U){c->stats.stale_fragments++;return 0;}
 }
 struct unit *u=NULL;
 for(unsigned i=0;i<V3VF_MAX_UNITS;i++)if(c->units[i].storage&&c->units[i].info.unit_id==id){u=&c->units[i];break;}
 if(u){
  if(u->info.frame_id!=frame||u->info.capture_time_us!=capture||u->info.bytes!=total||u->info.type!=type||u->count!=count)return invalid(c);
 }else{
  size_t allocation=total+(count+7U)/8U;
  while(c->stats.current_units==V3VF_MAX_UNITS||c->stats.current_bytes+allocation>V3VF_CACHE_MAX_BYTES){
   struct unit *victim=oldest(c);
   if(!victim)return invalid(c);
   c->stats.evicted_units++;release_unit(c,victim);
  }
  for(unsigned i=0;i<V3VF_MAX_UNITS;i++)if(!c->units[i].storage){u=&c->units[i];break;}
  if(!u)return invalid(c);
  u->storage=malloc(allocation);
  if(!u->storage){c->stats.alloc_failures++;return 0;}
  u->bitmap=u->storage+total;memset(u->bitmap,0,(count+7U)/8U);u->allocation=allocation;u->first_seen=local;u->count=count;u->received=0;
  u->info=(struct v3vf_info){.session=session,.unit_id=id,.frame_id=frame,.bytes=total,.capture_time_us=capture,.duration_us=duration,.type=(uint8_t)type,.flags=0};
  c->stats.current_bytes+=allocation;c->stats.current_units++;
  if(c->stats.current_bytes>c->stats.peak_bytes)c->stats.peak_bytes=c->stats.current_bytes;
  if(c->stats.current_units>c->stats.peak_units)c->stats.peak_units=c->stats.current_units;
 }
 if(!c->latest_valid||(id-c->latest!=0&&id-c->latest<0x80000000U)){c->latest=id;c->latest_valid=1;}
 if(c->ordered){
  if(!c->order_have){c->order_have=1;c->next_id=id;c->order_start=local;}
  else if(!c->order_started&&c->next_id-id<V3VF_REORDER_UNITS)c->next_id=id;
 }
 if(u->bitmap[index/8U]&(1U<<(index%8U))){c->stats.duplicate_fragments++;return 0;}
 memcpy(u->storage+offset,p+40,length);u->bitmap[index/8U]|=(uint8_t)(1U<<(index%8U));u->received++;u->info.flags|=(uint8_t)flags;c->stats.accepted_fragments++;
 if(u->received==u->count){
  c->stats.units_completed++;
  if(!c->ordered)publish(c,u,deliver,opaque);
  else v3vf_drain(c,local,source,valid,deliver,opaque);
  return 1;
 }
 if(c->ordered)v3vf_drain(c,local,source,valid,deliver,opaque);
 return 0;
}
void v3vf_get_stats(const struct v3vf *c,struct v3vf_stats *s){*s=c->stats;}
