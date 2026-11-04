/* SPDX-License-Identifier: MIT */
#ifndef __UAPI_DRM_ADI_HDMI_PRESENT_H__
#define __UAPI_DRM_ADI_HDMI_PRESENT_H__
#include "drm.h"
#define DRM_ADI_HDMI_PRESENT_ABI_MAJOR 1U
#define DRM_ADI_HDMI_PRESENT_ABI_MINOR 0U
#define DRM_ADI_HDMI_PRESENT_DRIVER_MAGIC 0x41485031U
#define DRM_ADI_HDMI_PRESENT_CAP_REAL_VSYNC_EOF (1U << 0)
#define DRM_ADI_HDMI_PRESENT_CAP_TOKEN_EVENT (1U << 1)
#define DRM_ADI_HDMI_PRESENT_CAP_REPEAT_FLAG (1U << 2)
#define DRM_ADI_HDMI_PRESENT_STATUS_READY (1U << 0)
#define DRM_ADI_HDMI_PRESENT_STATUS_ERROR (1U << 1)
#define DRM_ADI_HDMI_PRESENT_STATUS_VISUAL_UNKNOWN (1U << 2)
struct drm_adi_hdmi_presentation_info {
	__u32 size;
	__u16 abi_major;
	__u16 abi_minor;
	__u32 driver_magic;
	__u32 capabilities;
	__u32 status;
	__u32 observer_id;
	__u32 observer_version;
	__u64 session;
	__u64 last_dma_eof_token;
	__u64 last_tx_boundary_token;
	__u32 last_dma_eof_serial;
	__u32 last_tx_boundary_serial;
	__u32 vsync_count;
	__u32 reserved[5];
};
#define DRM_ADI_HDMI_GET_PRESENTATION_INFO 0x00
#define DRM_IOCTL_ADI_HDMI_GET_PRESENTATION_INFO \
	DRM_IOWR(DRM_COMMAND_BASE + DRM_ADI_HDMI_GET_PRESENTATION_INFO, \
		struct drm_adi_hdmi_presentation_info)
#endif
