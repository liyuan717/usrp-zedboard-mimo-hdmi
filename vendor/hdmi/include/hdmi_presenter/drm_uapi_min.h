#pragma once
/* Exact subset of stable Linux include/uapi/drm/{drm.h,drm_mode.h}; this avoids
 * a libdrm dependency on the minimal target image. */
#include <cstdint>
#include <sys/ioctl.h>
namespace hdmi_presenter::drm_uapi {
constexpr unsigned char kIoctlBase = 'd'; constexpr uint32_t kCommandBase=0x40;
#define HP_DRM_IOWR(nr, type) _IOWR(::hdmi_presenter::drm_uapi::kIoctlBase, nr, type)
#define HP_DRM_IOW(nr, type) _IOW(::hdmi_presenter::drm_uapi::kIoctlBase, nr, type)
#define HP_DRM_IO(nr) _IO(::hdmi_presenter::drm_uapi::kIoctlBase, nr)
struct ModeInfo { uint32_t clock; uint16_t hdisplay,hsync_start,hsync_end,htotal,hskew; uint16_t vdisplay,vsync_start,vsync_end,vtotal,vscan; uint32_t vrefresh,flags,type; char name[32]; };
struct CardRes { uint64_t fb_id_ptr,crtc_id_ptr,connector_id_ptr,encoder_id_ptr; uint32_t count_fbs,count_crtcs,count_connectors,count_encoders,min_width,max_width,min_height,max_height; };
struct Connector { uint64_t encoders_ptr,modes_ptr,props_ptr,prop_values_ptr; uint32_t count_modes,count_props,count_encoders,encoder_id,connector_id,connector_type,connector_type_id,connection,mm_width,mm_height,subpixel,pad; };
struct Encoder { uint32_t encoder_id,encoder_type,crtc_id,possible_crtcs,possible_clones; };
struct Crtc { uint64_t set_connectors_ptr; uint32_t count_connectors,crtc_id,fb_id,x,y,gamma_size,mode_valid; ModeInfo mode; };
struct CreateDumb { uint32_t height,width,bpp,flags,handle,pitch; uint64_t size; };
struct MapDumb { uint32_t handle,pad; uint64_t offset; }; struct DestroyDumb { uint32_t handle; };
struct FbCmd2 { uint32_t fb_id,width,height,pixel_format,flags,handles[4],pitches[4],offsets[4]; uint64_t modifier[4]; };
struct PageFlip { uint32_t crtc_id,fb_id,flags,reserved; uint64_t user_data; };
struct Event { uint32_t type,length; }; struct EventVBlank { uint32_t type,length; uint64_t user_data; uint32_t tv_sec,tv_usec,sequence,crtc_id; };
struct GetCap { uint64_t capability,value; };
constexpr uint64_t kTimestampMonotonic=6;
constexpr unsigned long kGetCap=HP_DRM_IOWR(0x0c,GetCap);
struct PresentationInfo { uint32_t size; uint16_t abi_major,abi_minor; uint32_t driver_magic,capabilities,status,observer_id,observer_version; uint64_t session,last_dma_eof_token,last_tx_boundary_token; uint32_t last_dma_eof_serial,last_tx_boundary_serial,vsync_count,reserved[5]; };
constexpr uint32_t kConnected=1, kModePreferred=1U<<3, kModeInterlace=1U<<4,
                   kFlipEvent=1, kEventFlipComplete=2, kXrgb8888=0x34325258U;
constexpr uint32_t kAbiMajor=1,kMagic=0x41485031U,kObserver=0x48504f31U,kCapRealVsyncEof=1,kCapTokenEvent=2,kCapRepeatFlag=4,kReady=1,kError=2;
/* A DRM page-flip event has no presentation-session generation field.  The
 * user token therefore becomes trustworthy only while an immediately sampled
 * observer reports the same READY session that armed the presenter. */
inline bool running_session_unchanged(const PresentationInfo& info, uint64_t expected_session) {
  return expected_session != 0 && info.session == expected_session &&
         (info.status & kReady) != 0 && (info.status & kError) == 0;
}
constexpr unsigned long kGetResources=HP_DRM_IOWR(0xA0, CardRes); constexpr unsigned long kGetCrtc=HP_DRM_IOWR(0xA1,Crtc); constexpr unsigned long kSetCrtc=HP_DRM_IOWR(0xA2,Crtc); constexpr unsigned long kGetEncoder=HP_DRM_IOWR(0xA6,Encoder); constexpr unsigned long kGetConnector=HP_DRM_IOWR(0xA7,Connector); constexpr unsigned long kPageFlip=HP_DRM_IOWR(0xB0,PageFlip); constexpr unsigned long kRmFb=HP_DRM_IOWR(0xAF,uint32_t); constexpr unsigned long kAddFb2=HP_DRM_IOWR(0xB8,FbCmd2); constexpr unsigned long kCreateDumb=HP_DRM_IOWR(0xB2,CreateDumb); constexpr unsigned long kMapDumb=HP_DRM_IOWR(0xB3,MapDumb); constexpr unsigned long kDestroyDumb=HP_DRM_IOWR(0xB4,DestroyDumb); constexpr unsigned long kSetMaster=HP_DRM_IO(0x1e); constexpr unsigned long kDropMaster=HP_DRM_IO(0x1f); constexpr unsigned long kGetPresentationInfo=HP_DRM_IOWR(kCommandBase,PresentationInfo);
static_assert(sizeof(ModeInfo)==68 && sizeof(CardRes)==64 && sizeof(Connector)==80 &&
              sizeof(Encoder)==20 && sizeof(Crtc)==104 && sizeof(CreateDumb)==32 &&
              sizeof(MapDumb)==16 && sizeof(DestroyDumb)==4 && sizeof(FbCmd2)==104 &&
              sizeof(PageFlip)==24 && sizeof(Event)==8 && sizeof(EventVBlank)==32 &&
              sizeof(PresentationInfo)==88, "DRM UAPI layout drift");
}  // namespace hdmi_presenter::drm_uapi
#undef HP_DRM_IOWR
#undef HP_DRM_IOW
#undef HP_DRM_IO
