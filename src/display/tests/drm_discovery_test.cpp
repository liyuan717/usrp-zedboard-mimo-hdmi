#include "hdmi_presenter/presenter.h"
#include "hdmi_presenter/drm_uapi_min.h"
#include <cassert>
#include <cerrno>
#include <cstdarg>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sys/mman.h>
namespace d=hdmi_presenter::drm_uapi;
static unsigned modesets=0, flips=0; static bool active=false; static unsigned scenario=0; static uint32_t serial=0;
extern "C" int __wrap_open(const char*,int,...){return 123;}
extern "C" int __wrap_close(int){return 0;}
extern "C" void* __wrap_mmap(void*,size_t n,int,int,int,off_t){return std::calloc(1,n);}
extern "C" int __wrap_munmap(void* p,size_t){std::free(p);return 0;}
static bool copy_id(uint64_t p,uint32_t n,uint32_t value){if(n&&!p){errno=EFAULT;return false;}if(n)*reinterpret_cast<uint32_t*>(p)=value;return true;}
extern "C" int __wrap_ioctl(int,unsigned long cmd,...){
 va_list ap;va_start(ap,cmd);void* arg=va_arg(ap,void*);va_end(ap);
 if(cmd==d::kSetMaster||cmd==d::kDropMaster||cmd==d::kRmFb||cmd==d::kDestroyDumb)return 0;
 if(cmd==d::kGetPresentationInfo){auto*p=static_cast<d::PresentationInfo*>(arg);*p={};p->size=sizeof(*p);p->abi_major=1;p->driver_magic=d::kMagic;p->observer_id=d::kObserver;p->capabilities=7;p->status=scenario==3?d::kError:active?d::kReady:0;p->session=active?9:0;return 0;}
 if(cmd==d::kGetResources){auto*p=static_cast<d::CardRes*>(arg);
  if(!copy_id(p->fb_id_ptr,p->count_fbs,11)||!copy_id(p->crtc_id_ptr,p->count_crtcs,22)||!copy_id(p->connector_id_ptr,p->count_connectors,33)||!copy_id(p->encoder_id_ptr,p->count_encoders,44))return -1;
  p->count_fbs=p->count_crtcs=p->count_connectors=p->count_encoders=1;return 0;}
 if(cmd==d::kGetConnector){auto*p=static_cast<d::Connector*>(arg);
  if((p->count_modes&&!p->modes_ptr)||(p->count_props&&(!p->props_ptr||!p->prop_values_ptr))||(p->count_encoders&&!p->encoders_ptr)){errno=EFAULT;return -1;}
  if(p->count_modes){auto*m=reinterpret_cast<d::ModeInfo*>(p->modes_ptr);*m={};m->clock=48000;m->hdisplay=scenario==1?800:1024;m->vdisplay=600;m->htotal=1280;m->vtotal=625;m->flags=scenario==2?d::kModeInterlace:0;if(scenario==4){m->clock=50250;m->htotal=1344;m->vtotal=625;}if(scenario==5)m->clock=46400;}
  if(p->count_encoders)*reinterpret_cast<uint32_t*>(p->encoders_ptr)=44;
  if(p->count_props){*reinterpret_cast<uint32_t*>(p->props_ptr)=55;*reinterpret_cast<uint64_t*>(p->prop_values_ptr)=66;}
  p->connection=d::kConnected;p->encoder_id=44;p->count_modes=p->count_props=p->count_encoders=1;return 0;}
 if(cmd==d::kGetEncoder){auto*p=static_cast<d::Encoder*>(arg);p->crtc_id=22;p->possible_crtcs=1;return 0;}
 if(cmd==d::kGetCrtc){auto*p=static_cast<d::Crtc*>(arg);p->mode_valid=0;return 0;}
 if(cmd==d::kPageFlip){++flips;return active?0:-1;}
 if(cmd==d::kSetCrtc){++modesets;active=static_cast<d::Crtc*>(arg)->mode_valid!=0;return 0;}
 if(cmd==d::kCreateDumb){auto*p=static_cast<d::CreateDumb*>(arg);p->handle=++serial;p->pitch=4096;p->size=1024*600*4;return 0;}
 if(cmd==d::kAddFb2){static_cast<d::FbCmd2*>(arg)->fb_id=++serial;return 0;}
 if(cmd==d::kMapDumb){static_cast<d::MapDumb*>(arg)->offset=0;return 0;}
 errno=EINVAL;return -1;
}
int main(){
 for(scenario=0;scenario<6;++scenario){active=false;auto p=hdmi_presenter::make_drm_backend("mock-card");
  if((scenario==0||scenario==4)&&!p->supports_physical_presentation()){std::cerr<<p->unavailable_reason()<<'\n';return 1;}
  if(scenario>0&&scenario!=4)assert(!p->supports_physical_presentation());
  if(scenario==4)assert(p->mode_refresh_millihz()==59821);
  if(scenario==0)assert(p->mode_refresh_millihz()==60000);
 }
 scenario=0;active=false;modesets=flips=0;
 {auto p=hdmi_presenter::make_drm_backend("mock-card");assert(p->supports_physical_presentation());
  assert(!active&&modesets==0&&flips==0);
 }
 assert(!active&&modesets==0&&flips==0); // No-frame failure/close never enables or restores CRTC.
 {auto p=hdmi_presenter::make_drm_backend("mock-card");assert(p->supports_physical_presentation());
  std::string error;
  hdmi_presenter::SlotView frame{};frame.slot_index=0;
  assert(p->submit(frame,0,&error)==hdmi_presenter::BackendSubmitResult::kFailure);
  assert(modesets==0); // Invalid frames cannot trigger scanout.
  assert(p->submit(frame,7,&error)==hdmi_presenter::BackendSubmitResult::kAccepted);
  assert(active&&modesets==1&&flips==1);
 }
 assert(!active&&modesets==2); // Restore only a genuinely activated CRTC.
 std::cout<<"PASS discovery, cold zero-frame close, deferred activation, and preserved observer gates\n";
}
