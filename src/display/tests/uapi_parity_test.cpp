#include "hdmi_presenter/drm_uapi_min.h"

extern "C" {
#include <drm/drm.h>
#include <drm/drm_mode.h>
#include <drm/adi_hdmi_present.h>
}

#include <cstddef>

namespace hp = hdmi_presenter::drm_uapi;

#define HP_SAME_SIZE(local, kernel) static_assert(sizeof(local) == sizeof(kernel), #local " size")
#define HP_SAME_MEMBER(local, kernel, member) \
  static_assert(offsetof(local, member) == offsetof(kernel, member), #local "." #member " offset")

HP_SAME_SIZE(hp::ModeInfo, drm_mode_modeinfo);
HP_SAME_MEMBER(hp::ModeInfo, drm_mode_modeinfo, clock);
HP_SAME_MEMBER(hp::ModeInfo, drm_mode_modeinfo, hdisplay);
HP_SAME_MEMBER(hp::ModeInfo, drm_mode_modeinfo, vdisplay);
HP_SAME_MEMBER(hp::ModeInfo, drm_mode_modeinfo, flags);
HP_SAME_MEMBER(hp::ModeInfo, drm_mode_modeinfo, name);
HP_SAME_SIZE(hp::CardRes, drm_mode_card_res);
HP_SAME_MEMBER(hp::CardRes, drm_mode_card_res, connector_id_ptr);
HP_SAME_MEMBER(hp::CardRes, drm_mode_card_res, count_connectors);
HP_SAME_SIZE(hp::Connector, drm_mode_get_connector);
HP_SAME_MEMBER(hp::Connector, drm_mode_get_connector, modes_ptr);
HP_SAME_MEMBER(hp::Connector, drm_mode_get_connector, connection);
HP_SAME_SIZE(hp::Encoder, drm_mode_get_encoder);
HP_SAME_SIZE(hp::Crtc, drm_mode_crtc);
HP_SAME_MEMBER(hp::Crtc, drm_mode_crtc, set_connectors_ptr);
HP_SAME_MEMBER(hp::Crtc, drm_mode_crtc, mode);
HP_SAME_SIZE(hp::CreateDumb, drm_mode_create_dumb);
HP_SAME_MEMBER(hp::CreateDumb, drm_mode_create_dumb, pitch);
HP_SAME_SIZE(hp::MapDumb, drm_mode_map_dumb);
HP_SAME_SIZE(hp::DestroyDumb, drm_mode_destroy_dumb);
HP_SAME_SIZE(hp::FbCmd2, drm_mode_fb_cmd2);
HP_SAME_MEMBER(hp::FbCmd2, drm_mode_fb_cmd2, modifier);
HP_SAME_SIZE(hp::PageFlip, drm_mode_crtc_page_flip);
HP_SAME_MEMBER(hp::PageFlip, drm_mode_crtc_page_flip, user_data);
HP_SAME_SIZE(hp::Event, drm_event);
HP_SAME_SIZE(hp::EventVBlank, drm_event_vblank);
HP_SAME_MEMBER(hp::EventVBlank, drm_event_vblank, user_data);
HP_SAME_SIZE(hp::PresentationInfo, drm_adi_hdmi_presentation_info);
HP_SAME_MEMBER(hp::PresentationInfo, drm_adi_hdmi_presentation_info, session);
HP_SAME_MEMBER(hp::PresentationInfo, drm_adi_hdmi_presentation_info, reserved);

static_assert(hp::kGetResources == DRM_IOCTL_MODE_GETRESOURCES, "GETRESOURCES ioctl");
static_assert(hp::kGetCrtc == DRM_IOCTL_MODE_GETCRTC, "GETCRTC ioctl");
static_assert(hp::kSetCrtc == DRM_IOCTL_MODE_SETCRTC, "SETCRTC ioctl");
static_assert(hp::kGetEncoder == DRM_IOCTL_MODE_GETENCODER, "GETENCODER ioctl");
static_assert(hp::kGetConnector == DRM_IOCTL_MODE_GETCONNECTOR, "GETCONNECTOR ioctl");
static_assert(hp::kPageFlip == DRM_IOCTL_MODE_PAGE_FLIP, "PAGE_FLIP must be IOWR");
static_assert(hp::kRmFb == DRM_IOCTL_MODE_RMFB, "RMFB ioctl");
static_assert(hp::kAddFb2 == DRM_IOCTL_MODE_ADDFB2, "ADDFB2 ioctl");
static_assert(hp::kCreateDumb == DRM_IOCTL_MODE_CREATE_DUMB, "CREATE_DUMB ioctl");
static_assert(hp::kMapDumb == DRM_IOCTL_MODE_MAP_DUMB, "MAP_DUMB ioctl");
static_assert(hp::kDestroyDumb == DRM_IOCTL_MODE_DESTROY_DUMB, "DESTROY_DUMB ioctl");
static_assert(hp::kSetMaster == DRM_IOCTL_SET_MASTER, "SET_MASTER ioctl");
static_assert(hp::kDropMaster == DRM_IOCTL_DROP_MASTER, "DROP_MASTER ioctl");
static_assert(hp::kGetPresentationInfo == DRM_IOCTL_ADI_HDMI_GET_PRESENTATION_INFO,
              "ADI presentation ioctl");

int main() { return 0; }
