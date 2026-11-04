/*
 * Analog Devices AXI HDMI DRM driver.
 *
 * Copyright 2012 Analog Devices Inc.
 *  Author: Lars-Peter Clausen <lars@metafoo.de>
 *
 * Licensed under the GPL-2.
 */

#ifndef _AXI_HDMI_DRV_H_
#define _AXI_HDMI_DRV_H_

#include <drm/drm.h>
#include <drm/drm_fb_dma_helper.h>
#include <linux/of.h>
#include <linux/clk.h>
#include <linux/interrupt.h>
#include <linux/spinlock.h>

struct xlnx_pcm_dma_params {
	struct device_node *of_node;
	int chan_id;
};

struct axi_hdmi_encoder;

struct axi_hdmi_private {
	struct drm_device *drm_dev;
	struct drm_crtc *crtc;
	struct axi_hdmi_encoder *encoder;
	struct i2c_client *encoder_slave;

	void __iomem *base;

	struct clk *hdmi_clock;
	bool clk_enabled;

	struct dma_chan *dma;
	/* HPO1 is a passive monitor; it never owns AXIS back pressure. */
	void __iomem *observer_base;
	int observer_irq;
	spinlock_t presentation_lock;
	bool presentation_ready;
	bool presentation_error;
	u32 observer_version;
	u32 observer_caps;
	u32 vsync_count;
	u32 last_dma_eof_serial;
	u32 last_tx_boundary_serial;
	u64 last_dma_eof_token;
	u64 last_tx_boundary_token;
	u64 presentation_session;

	bool is_rgb;
};

struct drm_crtc* axi_hdmi_crtc_create(struct drm_device *dev);
struct drm_encoder *axi_hdmi_encoder_create(struct drm_device *dev);
int axi_hdmi_presentation_atomic_check(struct drm_device *dev,
				       struct drm_atomic_state *state);
irqreturn_t axi_hdmi_presentation_irq(struct axi_hdmi_private *p);
void axi_hdmi_presentation_shutdown(struct axi_hdmi_private *p);
void axi_hdmi_presentation_remove(struct axi_hdmi_private *p);
void axi_hdmi_presentation_fault(struct axi_hdmi_private *p);
int axi_hdmi_presentation_get_info(struct axi_hdmi_private *p, void *data);

#endif
