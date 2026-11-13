#include "include/g0_video_au.h"
#include <stdlib.h>
#include <string.h>
struct v3au {
 uint8_t data[V3AU_MAX_BYTES+64];
 struct v3au_info info;struct v3au_stats stats;
 uint64_t first_local,last_local;
 uint32_t session,last_unit,last_frame;
 unsigned session_valid,unit_valid,frame_valid,have,vcl,flags;
};
struct v3au *v3au_create(void){return calloc(1,sizeof(struct v3au));}
void v3au_destroy(struct v3au *c){free(c);}
size_t v3au_context_bytes(void){return sizeof(struct v3au);}
void v3au_session(struct v3au *c,uint32_t session)
{
 if(c->session_valid&&c->session==session)return;
 if(c->have)c->stats.session_dropped++;
 c->session=session;c->session_valid=1;c->unit_valid=0;c->frame_valid=0;c->have=0;
}
static void flush(struct v3au *c,v3au_deliver deliver,void *opaque)
{
 if(!c->have)return;
 if(c->vcl){
  c->info.damaged=c->info.missing_units!=0||!(c->flags&1U)||!(c->flags&2U);
  memset(c->data+c->info.bytes,0,64);
  c->stats.submitted++;c->stats.damaged+=c->info.damaged;
  if(!deliver||deliver(opaque,&c->info,c->data)<=0)c->stats.output_dropped++;
  c->last_frame=c->info.frame_id;c->frame_valid=1;
 }else c->stats.no_vcl++;
 c->have=0;
}
void v3au_tick(struct v3au *c,uint64_t local,v3au_deliver deliver,void *opaque)
{
 if(c->have&&((local>=c->last_local&&local-c->last_local>=V3AU_IDLE_US)||
             (local>=c->first_local&&local-c->first_local>=V3AU_MAX_HOLD_US)))
  flush(c,deliver,opaque);
}
int v3au_push(struct v3au *c,const struct v3vf_info *n,const uint8_t *data,uint64_t local,v3au_deliver deliver,void *opaque)
{
 if(!n||!data||!n->bytes||n->bytes>V3AU_MAX_BYTES){c->stats.invalid++;return 0;}
 if(!c->session_valid||n->session!=c->session){c->stats.stale++;return 0;}
 unsigned start=n->bytes>=4&&data[0]==0&&data[1]==0&&data[2]==1?3:
  n->bytes>=5&&data[0]==0&&data[1]==0&&data[2]==0&&data[3]==1?4:0;
 if(!start||(data[start]&0x80)||(data[start]&31U)!=n->type||n->duration_us!=50000){c->stats.invalid++;return 0;}
 if(c->unit_valid&&(n->unit_id-c->last_unit==0||n->unit_id-c->last_unit>=0x80000000U)){c->stats.stale++;return 0;}
 if(c->frame_valid&&(n->frame_id-c->last_frame==0||n->frame_id-c->last_frame>=0x80000000U)){c->stats.stale++;return 0;}
 uint32_t missing=c->unit_valid?n->unit_id-c->last_unit-1U:0;
 if(c->have&&n->frame_id!=c->info.frame_id&&n->frame_id-c->info.frame_id>=0x80000000U){c->stats.stale++;return 0;}
 if(c->have&&n->frame_id==c->info.frame_id&&n->capture_time_us!=c->info.capture_time_us){c->stats.invalid++;return 0;}
 if(c->have&&n->frame_id!=c->info.frame_id)flush(c,deliver,opaque);
 v3au_tick(c,local,deliver,opaque);
 if(c->frame_valid&&n->frame_id==c->last_frame){c->stats.stale++;return 0;}
 if(!c->have){c->have=1;c->vcl=0;c->flags=0;c->first_local=local;c->info=(struct v3au_info){.session=n->session,.frame_id=n->frame_id,.capture_time_us=n->capture_time_us};}
 c->last_unit=n->unit_id;c->unit_valid=1;c->info.missing_units+=missing;
 if(n->bytes>V3AU_MAX_BYTES-c->info.bytes){
  c->stats.overflow++;c->have=0;c->last_frame=n->frame_id;c->frame_valid=1;return 0;
 }
 memcpy(c->data+c->info.bytes,data,n->bytes);c->info.bytes+=n->bytes;
 c->last_local=local;
 if(c->info.bytes>c->stats.peak_bytes)c->stats.peak_bytes=c->info.bytes;
 c->vcl|=n->type==1||n->type==5;c->flags|=n->flags;
 if(n->flags&2U)flush(c,deliver,opaque);
 return 1;
}
void v3au_get_stats(const struct v3au *c,struct v3au_stats *s){*s=c->stats;}
