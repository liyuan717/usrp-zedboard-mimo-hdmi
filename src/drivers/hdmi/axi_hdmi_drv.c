/*
 * Analog Devices AXI HDMI DRM driver.
 *
 * Copyright 2012 Analog Devices Inc.
 *  Author: Lars-Peter Clausen <lars@metafoo.de>
 *
 * Licensed under the GPL-2.
 */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/of.h>
#include <linux/i2c.h>
#include <linux/of_address.h>
#include <linux/of_dma.h>
#include <linux/of_graph.h>
#include <linux/of_irq.h>
#include <linux/clk.h>
#include <linux/interrupt.h>
#include <linux/io.h>

#include <drm/drm.h>
#include <drm/drm_atomic_helper.h>
#include <drm/drm_crtc_helper.h>
#include <drm/drm_drv.h>
#include <drm/drm_fbdev_ttm.h>
#include <drm/drm_fb_helper.h>
#include <drm/drm_gem_dma_helper.h>
#include <drm/drm_gem_framebuffer_helper.h>
#include <drm/drm_probe_helper.h>
#include <drm/drm_ioctl.h>
#include <drm/drm_of.h>
#include <drm/drm_panel.h>
#include <drm/drm_bridge.h>
#include <drm/drm_vblank.h>
#include <drm/adi_hdmi_present.h>

#include "axi_hdmi_drv.h"

#define DRIVER_NAME	"axi_hdmi_drm"
#define DRIVER_DESC	"AXI HDMI DRM"
#define DRIVER_DATE	"20120930"
#define DRIVER_MAJOR	1
#define DRIVER_MINOR	0

static int axi_hdmi_atomic_check(struct drm_device *dev,
				 struct drm_atomic_state *state)
{
	int ret = drm_atomic_helper_check(dev, state);

	if (ret)
		return ret;
	return axi_hdmi_presentation_atomic_check(dev, state);
}

static struct drm_mode_config_funcs axi_hdmi_mode_config_funcs = {
	.fb_create = drm_gem_fb_create,
	.atomic_check = axi_hdmi_atomic_check,
	.atomic_commit = drm_atomic_helper_commit,
};

#define HPO_ID 0x00
#define HPO_VERSION 0x04
#define HPO_CONTROL 0x08
#define HPO_IRQ_STATUS 0x0c
#define HPO_IRQ_ENABLE 0x10
#define HPO_IRQ_ALL GENMASK(4, 0)
#define HPO_IRQ_PRESENT BIT(5)
#define HPO_ID_VALUE 0x48504f31
#define HPO_CAPS_REQUIRED 0x7
#define HPO_CAP_EVENT_FIFO BIT(5)

static irqreturn_t axi_hdmi_observer_irq(int irq, void *arg)
{
	struct axi_hdmi_private *p = arg;

	return axi_hdmi_presentation_irq(p);
}

static int axi_hdmi_observer_init(struct platform_device *pdev,
				  struct axi_hdmi_private *p)
{
	struct device_node *node;
	struct resource res;
	u32 version;
	int ret;

	node = of_parse_phandle(pdev->dev.of_node, "adi,presentation-observer", 0);
	if (!node)
		return -ENODEV;
	ret = of_address_to_resource(node, 0, &res);
	if (ret)
		goto out_put;
	p->observer_base = devm_ioremap_resource(&pdev->dev, &res);
	if (IS_ERR(p->observer_base)) {
		ret = PTR_ERR(p->observer_base);
		goto out_put;
	}
	p->observer_irq = of_irq_get(node, 0);
	if (p->observer_irq < 0) {
		ret = p->observer_irq;
		goto out_put;
	}
	if (readl(p->observer_base + HPO_ID) != HPO_ID_VALUE) {
		ret = -ENODEV;
		goto out_put;
	}
	version = readl(p->observer_base + HPO_VERSION);
	if ((version >> 16) != DRM_ADI_HDMI_PRESENT_ABI_MAJOR ||
	    (version & HPO_CAPS_REQUIRED) != HPO_CAPS_REQUIRED) {
		ret = -EPROTONOSUPPORT;
		goto out_put;
	}
	p->observer_version = version;
	p->observer_caps = version & 0xffff;
	if (!(p->observer_caps & HPO_CAP_EVENT_FIFO)) {
		ret = -EPROTONOSUPPORT;
		goto out_put;
	}
	writel(HPO_IRQ_ALL, p->observer_base + HPO_IRQ_STATUS);
	writel(0, p->observer_base + HPO_IRQ_ENABLE);
	writel(0x2, p->observer_base + HPO_CONTROL); /* flush stale identities */
	ret = devm_request_irq(&pdev->dev, p->observer_irq, axi_hdmi_observer_irq,
			       IRQF_SHARED, "axi-hdmi-hpo1", p);
	if (ret)
		goto out_put;
	ret = 0;
out_put:
	of_node_put(node);
	return ret;
}

static int axi_hdmi_get_presentation_info_ioctl(struct drm_device *dev,
						 void *data,
						 struct drm_file *file)
{
	return axi_hdmi_presentation_get_info(dev->dev_private, data);
}

static const struct drm_ioctl_desc axi_hdmi_ioctls[] = {
	DRM_IOCTL_DEF_DRV(ADI_HDMI_GET_PRESENTATION_INFO,
			  axi_hdmi_get_presentation_info_ioctl, DRM_RENDER_ALLOW),
};

static void axi_hdmi_mode_config_init(struct drm_device *dev)
{
	drm_mode_config_init(dev);

	dev->mode_config.min_width = 0;
	dev->mode_config.min_height = 0;

	dev->mode_config.max_width = 4096;
	dev->mode_config.max_height = 4096;

	dev->mode_config.funcs = &axi_hdmi_mode_config_funcs;
}

static int axi_hdmi_init(struct drm_driver *ddrv, struct device *dev)
{
	struct axi_hdmi_private *private = dev_get_drvdata(dev);
	struct drm_device *ddev;
	struct drm_encoder *encoder;
	int ret;

	ddev = drm_dev_alloc(ddrv, dev);
	if (IS_ERR(ddev))
		return PTR_ERR(ddev);

	private->drm_dev = ddev;

	ddev->dev_private = private;

	axi_hdmi_mode_config_init(ddev);

	private->crtc = axi_hdmi_crtc_create(ddev);
	if (IS_ERR(private->crtc)) {
		ret = PTR_ERR(private->crtc);
		goto err_crtc;
	}
	ret = drm_vblank_init(ddev, 1);
	if (ret)
		goto err_crtc;

	encoder = axi_hdmi_encoder_create(ddev);
	if (IS_ERR(encoder)) {
	    ret = PTR_ERR(encoder);
	    goto err_crtc;
	}

	drm_mode_config_reset(ddev);

	/* init kms poll for handling hpd */
	drm_kms_helper_poll_init(ddev);
	/* HPO1 stays flushed and IRQ-masked until atomic_enable starts a CRTC. */

	ret = drm_dev_register(ddev, 0);
	if (ret)
		goto err_poll;

	drm_fbdev_ttm_setup(ddev, 32);

	return 0;
err_poll:
	drm_kms_helper_poll_fini(ddev);
err_crtc:
	drm_mode_config_cleanup(ddev);
	drm_dev_put(ddev);
	return ret;
}

static void axi_hdmi_unload(struct drm_device *dev)
{
	struct axi_hdmi_private *private = dev->dev_private;

	/* Called after unregister has blocked new ioctls, while KMS objects exist. */
	drm_atomic_helper_shutdown(dev);
	axi_hdmi_presentation_remove(private);
	drm_kms_helper_poll_fini(dev);
	drm_mode_config_cleanup(dev);
}

static const struct file_operations axi_hdmi_driver_fops = {
	.fop_flags	= FOP_UNSIGNED_OFFSET,
	.owner		= THIS_MODULE,
	.open		= drm_open,
	.mmap		= drm_gem_mmap,
	.poll		= drm_poll,
	.read		= drm_read,
	.unlocked_ioctl	= drm_ioctl,
	.release	= drm_release,
};

static struct drm_driver axi_hdmi_driver = {
	.driver_features	= DRIVER_MODESET | DRIVER_GEM | DRIVER_ATOMIC,
	.ioctls			= axi_hdmi_ioctls,
	.num_ioctls		= ARRAY_SIZE(axi_hdmi_ioctls),
	.unload			= axi_hdmi_unload,
	.gem_prime_import	= drm_gem_prime_import,
	.gem_prime_import_sg_table = drm_gem_dma_prime_import_sg_table,
	.dumb_create		= drm_gem_dma_dumb_create,
	.fops			= &axi_hdmi_driver_fops,
	.name			= DRIVER_NAME,
	.desc			= DRIVER_DESC,
	.date			= DRIVER_DATE,
	.major			= DRIVER_MAJOR,
	.minor			= DRIVER_MINOR,
};

static const struct of_device_id adv7511_encoder_of_match[] = {
	{
		.compatible = "adi,axi-hdmi-tx-1.00.a",
	},
	{},
};
MODULE_DEVICE_TABLE(of, adv7511_encoder_of_match);

static int axi_hdmi_platform_probe(struct platform_device *pdev)
{
	struct device_node *np = pdev->dev.of_node;
	struct axi_hdmi_private *private;
	struct device_node *slave_node, *ep_node;
	struct of_endpoint ep;
	struct resource *res;
	struct drm_panel *panel;
	struct drm_bridge *bridge;
	int ret;

	private = devm_kzalloc(&pdev->dev, sizeof(*private), GFP_KERNEL);
	if (!private)
		return -ENOMEM;
	spin_lock_init(&private->presentation_lock);
	private->presentation_session = 1;

	res = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	private->base = devm_ioremap_resource(&pdev->dev, res);
	if (IS_ERR(private->base))
		return PTR_ERR(private->base);

	private->hdmi_clock = devm_clk_get(&pdev->dev, NULL);
	if (IS_ERR(private->hdmi_clock)) {
		return -EPROBE_DEFER;
	}

	ep_node = of_graph_get_next_endpoint(np, NULL);
	if (ep_node) {
		ret = of_graph_parse_endpoint(ep_node, &ep);
		if (ret) {
			of_node_put(ep_node);
			return ret;
		}
		if (ep.port != 0 && ep.id != 0) {
			of_node_put(ep_node);
			return -EINVAL;
		}
		slave_node = of_graph_get_remote_port_parent(ep_node);
		of_node_put(ep_node);
	} else {
		slave_node = of_parse_phandle(np, "encoder-slave", 0);
	}

	if (!slave_node)
		return -EINVAL;
	ret = drm_of_find_panel_or_bridge(np, 0, 0, &panel, &bridge);
	if (ret) {
		of_node_put(slave_node);
		return ret;
	}
	if (panel || !bridge) {
		of_node_put(slave_node);
		return -ENODEV;
	}

	private->is_rgb = of_property_read_bool(np, "adi,is-rgb");

	private->encoder_slave = of_find_i2c_device_by_node(slave_node);
	of_node_put(slave_node);

	if (!private->encoder_slave)
		return -EPROBE_DEFER;

	private->dma = dma_request_slave_channel(&pdev->dev, "video");
	if (private->dma == NULL) {
		put_device(&private->encoder_slave->dev);
		return -EPROBE_DEFER;
	}

	platform_set_drvdata(pdev, private);

	ret = axi_hdmi_observer_init(pdev, private);
	if (ret) {
		dma_release_channel(private->dma);
		put_device(&private->encoder_slave->dev);
		return ret;
	}
	ret = axi_hdmi_init(&axi_hdmi_driver, &pdev->dev);
	if (ret) {
		dma_release_channel(private->dma);
		put_device(&private->encoder_slave->dev);
	}
	return ret;
}

static void axi_hdmi_platform_remove(struct platform_device *pdev)
{
	struct axi_hdmi_private *private = platform_get_drvdata(pdev);

	/* Unregister first: unload owns KMS/DMA teardown before objects disappear. */
	drm_dev_unregister(private->drm_dev);
	/* drm_put_dev() unregisters again. This owner already did so above. */
	drm_dev_put(private->drm_dev);
	dma_release_channel(private->dma);
	put_device(&private->encoder_slave->dev);
}

static struct platform_driver adv7511_encoder_driver = {
	.driver = {
		.name = "axi-hdmi",
		.owner = THIS_MODULE,
		.of_match_table = adv7511_encoder_of_match,
	},
	.probe = axi_hdmi_platform_probe,
	.remove = axi_hdmi_platform_remove,
};
module_platform_driver(adv7511_encoder_driver);

MODULE_LICENSE("GPL v2");
MODULE_AUTHOR("Lars-Peter Clausen <lars@metafoo.de>");
MODULE_DESCRIPTION("");
