/*
 * Analog Devices AXI HDMI DRM driver.
 *
 * Copyright 2012 Analog Devices Inc.
 *  Author: Lars-Peter Clausen <lars@metafoo.de>
 *
 * Licensed under the GPL-2.
 */

#include <linux/slab.h>
#include <linux/dmaengine.h>
#include <linux/dma/xilinx_dma.h>
#include <linux/io.h>
#include <linux/mutex.h>
#include <linux/workqueue.h>
#include <linux/moduleparam.h>
#include <linux/timekeeping.h>
#include "refill_probe.h"

#include <drm/drm_atomic.h>
#include <drm/drm_atomic_helper.h>
#include <drm/drm_crtc_helper.h>
#include <drm/drm_fourcc.h>
#include <drm/drm_framebuffer.h>
#include <drm/drm_gem_dma_helper.h>
#include <drm/drm_gem_framebuffer_helper.h>
#include <drm/drm_plane_helper.h>
#include <drm/drm_print.h>
#include <drm/drm_vblank.h>
#include <drm/adi_hdmi_present.h>

#include "axi_hdmi_drv.h"

enum axi_hdmi_presentation_state {
	AXI_HDMI_OFF,
	AXI_HDMI_PREPARED,
	AXI_HDMI_RUNNING,
	AXI_HDMI_FAULT,
	AXI_HDMI_STOPPING,
};

struct axi_hdmi_crtc {
	struct drm_crtc drm_crtc;
	struct drm_plane plane;

	struct dma_chan *dma;
	struct axi_hdmi_private *private;
	struct mutex submit_lock;
	struct mutex teardown_lock;
	struct work_struct completion_work;
	struct work_struct fault_work;
	struct drm_pending_vblank_event *latched_event;
	struct drm_framebuffer *last_good_fb;
	dma_addr_t last_good_src;
	u32 last_good_width;
	u32 last_good_height;
	u32 last_good_pitch;
	u32 last_good_serial;
	u64 last_good_token;
	struct dma_interleaved_template *dma_template[2];
	struct drm_framebuffer *job_fb[2];
	struct drm_pending_vblank_event *job_event[2];
	u64 job_token[2];
	u32 job_serial[2];
	dma_addr_t job_src[2];
	u32 job_width[2];
	u32 job_height[2];
	u32 job_pitch[2];
	bool job_dma_done[2];
	bool job_eof_seen[2];
	bool job_boundary_seen[2];
	bool job_complete[2];
	bool job_submitted[2];
	bool job_new[2];
	u32 job_generation[2];
	struct {
		struct axi_hdmi_crtc *axi;
		u32 generation;
		u8 slot;
	} callback[2];
	u8 active_job;
	u8 pending_job;
	u32 next_serial;
	enum axi_hdmi_presentation_state presentation_state;
	bool permanent_stop;
	bool vblank_ref;
	struct dma_async_tx_descriptor *job_desc[2];
	u32 last_irq_vsync;
	u64 probe_queued_ns;
	u64 probe_boundary_ns[2];
	u64 probe_retired_boundary_ns;
};

#define HPO_IRQ_STATUS 0x0c
#define HPO_IRQ_ENABLE 0x10
#define HPO_CONTROL 0x08
#define HPO_VSYNC_COUNT 0x14
#define HPO_LAST_EOF 0x18
#define HPO_LAST_BOUNDARY 0x24
#define HPO_QUEUE_SUBMIT 0x34
#define HPO_FIFO_COUNT 0x3c
#define HPO_FIFO_HEAD_SERIAL 0x40
#define HPO_FIFO_HEAD_BEATS 0x44
#define HPO_FIFO_EOF_VSYNC 0x48
#define HPO_FIFO_BOUNDARY_VSYNC 0x4c
#define HPO_FIFO_POP 0x50
#define HPO_IRQ_VSYNC BIT(0)
#define HPO_IRQ_DMA_EOF BIT(1)
#define HPO_IRQ_TX_BOUNDARY BIT(2)
#define HPO_IRQ_QUEUE_FULL BIT(3)
#define HPO_IRQ_PROTOCOL BIT(4)
#define HPO_IRQ_PRESENT BIT(5)
#define HPO_IRQ_ALL (HPO_IRQ_VSYNC | HPO_IRQ_DMA_EOF | HPO_IRQ_TX_BOUNDARY | HPO_IRQ_QUEUE_FULL | HPO_IRQ_PROTOCOL)
#define HPO_CAP_EVENT_FIFO BIT(5)
#define HPO_FIFO_DEPTH 4
/* AXIS payload is 64-bit: 1024*600 XRGB8888 bytes / 8 bytes per beat. */
#define HPO_EXPECTED_BEATS (1024 * 600 * 4 / 8)
#define HPO_NEW BIT(31)
#define HPO_SERIAL_MASK GENMASK(30, 0)
#define AXI_HDMI_NO_JOB 0xff

/* Measurements are optional, read-only, aggregate-only, and reset by reload.
 * No per-frame printk or DMA flag changes; this candidate raises completion work priority.
 * Timestamps identify software observation points, NOT physical VSYNC edges.
 */
static bool measure_refill;
module_param(measure_refill, bool, 0444);
MODULE_PARM_DESC(measure_refill, "Collect aggregate HDMI completion/refill timings");
static DEFINE_SPINLOCK(probe_stats_lock);
static struct refill_probe_stats probe_stats;

static void probe_record(unsigned int metric, u64 start, u64 end)
{
	unsigned long flags;
	spin_lock_irqsave(&probe_stats_lock, flags);
	refill_probe_record(&probe_stats.metric[metric], start, end);
	spin_unlock_irqrestore(&probe_stats_lock, flags);
}

static int refill_stats_get(char *buf, const struct kernel_param *kp)
{
	struct refill_probe_stats snapshot;
	unsigned long flags;
	int n = 0, i;
	spin_lock_irqsave(&probe_stats_lock, flags);
	snapshot = probe_stats;
	spin_unlock_irqrestore(&probe_stats_lock, flags);
	n += scnprintf(buf + n, PAGE_SIZE - n,
		"{\"version\":1,\"enabled\":%s,\"queue_requests\":%llu,\"metrics\":[",
		measure_refill ? "true" : "false", snapshot.queue_requests);
	for (i = 0; i < REFILL_METRICS; i++) {
		struct refill_probe_metric *m = &snapshot.metric[i];
		n += scnprintf(buf + n, PAGE_SIZE - n,
			"%s{\"name\":\"%s\",\"valid\":%llu,\"missing\":%llu,\"reversed\":%llu,"
			"\"sum_ns\":%llu,\"max_ns\":%llu,\"sum_saturated\":%s,"
			"\"bins_lt_50us_250us_500us_1ms_2ms_else\":[%llu,%llu,%llu,%llu,%llu,%llu]}",
			i ? "," : "", refill_metric_names[i], m->valid, m->missing, m->reversed,
			m->sum_ns, m->max_ns, m->sum_saturated ? "true" : "false",
			m->bins[0], m->bins[1], m->bins[2], m->bins[3], m->bins[4], m->bins[5]);
	}
	return n + scnprintf(buf + n, PAGE_SIZE - n, "]}\n");
}
static const struct kernel_param_ops refill_stats_ops = { .get = refill_stats_get };
module_param_cb(refill_stats, &refill_stats_ops, NULL, 0444);

static void axi_hdmi_queue_completion(struct axi_hdmi_crtc *axi)
{
	unsigned long flags;
	if (measure_refill) {
		spin_lock_irqsave(&axi->private->presentation_lock, flags);
		refill_probe_queue(&axi->probe_queued_ns, ktime_get_mono_fast_ns());
		spin_unlock_irqrestore(&axi->private->presentation_lock, flags);
		spin_lock_irqsave(&probe_stats_lock, flags);
		probe_stats.queue_requests++;
		spin_unlock_irqrestore(&probe_stats_lock, flags);
	}
	queue_work(system_highpri_wq, &axi->completion_work);
}

static struct axi_hdmi_crtc *plane_to_axi_hdmi_crtc(struct drm_plane *plane)
{
	return container_of(plane, struct axi_hdmi_crtc, plane);
}

static struct axi_hdmi_crtc *to_axi_hdmi_crtc(struct drm_crtc *crtc)
{
	return container_of(crtc, struct axi_hdmi_crtc, drm_crtc);
}

/* Match drm_send_event_helper, but mark an aborted out-fence as an error. */
static void axi_hdmi_cancel_event(struct drm_device *dev,
				  struct drm_pending_vblank_event *event)
{
	unsigned long flags;

	if (!event)
		return;
	spin_lock_irqsave(&dev->event_lock, flags);
	if (event->base.completion) {
		complete_all(event->base.completion);
		event->base.completion_release(event->base.completion);
		event->base.completion = NULL;
	}
	if (event->base.fence) {
		dma_fence_set_error(event->base.fence, -EIO);
		dma_fence_signal(event->base.fence);
		dma_fence_put(event->base.fence);
		event->base.fence = NULL;
	}
	spin_unlock_irqrestore(&dev->event_lock, flags);
	drm_event_cancel_free(dev, &event->base);
}

static void axi_hdmi_dma_done(void *arg);
static void axi_hdmi_presentation_stop(struct axi_hdmi_private *p,
					       bool permanent, bool fault);

static struct dma_async_tx_descriptor *axi_hdmi_vdma_prep_interleaved_desc(
	struct axi_hdmi_crtc *axi_hdmi_crtc, unsigned int slot,
	struct drm_framebuffer *fb, dma_addr_t src, u32 width, u32 height)
{
	struct dma_interleaved_template *template = axi_hdmi_crtc->dma_template[slot];
	size_t hw_row_size;

#if IS_ENABLED(CONFIG_XILINX_DMA)
	struct xilinx_vdma_config vdma_config;

	if (!strncmp(axi_hdmi_crtc->dma->device->dev->driver->name, "xilinx-vdma", 11)) {
		memset(&vdma_config, 0, sizeof(vdma_config));
		vdma_config.park = 1;
		vdma_config.coalesc = 0xff;
		xilinx_vdma_channel_set_config(axi_hdmi_crtc->dma, &vdma_config);
	}
#endif

	/* Interleaved DMA is used that way:
	 * Each interleaved frame is a row (hsize) implemented in ONE
	 * chunk (sgl has len 1).
	 * The number of interleaved frames is the number of rows (vsize).
	 * The icg in used to pack data to the HW, so that the buffer len
	 * is fb->piches[0], but the actual size for the hw is somewhat less
	 */
	memset(template, 0, sizeof(*template) + sizeof(template->sgl[0]));
	template->dir = DMA_MEM_TO_DEV;
	template->src_start = src;
	/* sgl list have just one entry (each interleaved frame have 1 chunk) */
	template->frame_size = 1;
	/* the number of interleaved frame, each has the size specified in sgl */
	template->numf = height;
	template->src_sgl = 1;
	template->src_inc = 1;

	/* vdma IP does not provide any addr to the hdmi IP, so dst_inc
	 * and dst_sgl should make no any difference.
	 */
	template->dst_inc = 0;
	template->dst_sgl = 0;

	hw_row_size = width * 4;
	if (!width || !height || hw_row_size > fb->pitches[0])
		return NULL;
	template->sgl[0].size = hw_row_size;

	/* the vdma driver seems to look at icg, and not src_icg */
	template->sgl[0].icg =
		fb->pitches[0] - hw_row_size;

	return dmaengine_prep_interleaved_dma(axi_hdmi_crtc->dma,
				template, DMA_PREP_INTERRUPT);
}

/* submit_lock held: descriptor was prepared before enable but is not live yet */
static int axi_hdmi_start_slot(struct axi_hdmi_crtc *axi, unsigned int slot,
				       bool new_identity)
{
	struct axi_hdmi_private *p = axi->private;
	dma_cookie_t cookie;

	if (axi->presentation_state != AXI_HDMI_RUNNING || !axi->job_desc[slot])
		return -ESHUTDOWN;
	cookie = dmaengine_submit(axi->job_desc[slot]);
	if (dma_submit_error(cookie))
		return dma_submit_error(cookie);
	axi->job_submitted[slot] = true;
	writel(axi->job_serial[slot] | (new_identity ? HPO_NEW : 0),
	       p->observer_base + HPO_QUEUE_SUBMIT);
	dma_async_issue_pending(axi->dma);
	if (measure_refill && !new_identity) {
		probe_record(REFILL_BOUNDARY_TO_ISSUE, axi->probe_retired_boundary_ns,
			     ktime_get_mono_fast_ns());
		axi->probe_retired_boundary_ns = 0;
	}
	return 0;
}

/* submit_lock held; never call this while presentation_lock is held. */
static void axi_hdmi_remember_last_good(struct axi_hdmi_crtc *axi,
					struct drm_framebuffer *fb, dma_addr_t src,
					u32 width, u32 height, u32 pitch, u32 serial,
					u64 token)
{
	drm_framebuffer_assign(&axi->last_good_fb, fb);
	axi->last_good_src = src;
	axi->last_good_width = width;
	axi->last_good_height = height;
	axi->last_good_pitch = pitch;
	axi->last_good_serial = serial;
	axi->last_good_token = token;
}

static void axi_hdmi_complete_work_impl(struct work_struct *work)
{
	struct axi_hdmi_crtc *axi = container_of(work, struct axi_hdmi_crtc,
						     completion_work);
	struct axi_hdmi_private *p = axi->private;
	unsigned long flags;

	/* Retire only active->pending; never scan array order or skip a frame. */
	for (;;) {
		struct drm_pending_vblank_event *event = NULL;
		struct drm_framebuffer *fb = NULL;
		dma_addr_t src = 0;
		u32 width = 0, height = 0, pitch = 0, serial = 0;
		u64 token = 0;
		bool new_frame = false;
		u8 i;

		spin_lock_irqsave(&p->presentation_lock, flags);
		if (axi->presentation_state != AXI_HDMI_RUNNING) {
			spin_unlock_irqrestore(&p->presentation_lock, flags);
			return;
		}
		i = axi->active_job;
		if (i != AXI_HDMI_NO_JOB && axi->job_complete[i]) {
			if (measure_refill)
				axi->probe_retired_boundary_ns = axi->probe_boundary_ns[i];
			axi->job_complete[i] = false;
			axi->active_job = axi->pending_job;
			axi->pending_job = AXI_HDMI_NO_JOB;
			event = axi->job_event[i];
			axi->job_event[i] = NULL;
			fb = axi->job_fb[i];
			axi->job_fb[i] = NULL;
			src = axi->job_src[i];
			width = axi->job_width[i];
			height = axi->job_height[i];
			pitch = axi->job_pitch[i];
			serial = axi->job_serial[i];
			token = axi->job_token[i];
			new_frame = axi->job_new[i];
			axi->job_new[i] = false;
			axi->job_serial[i] = 0;
			axi->job_desc[i] = NULL;
			axi->job_submitted[i] = false;
		}
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		if (!fb)
			break;
		if (event) {
			spin_lock_irq(&axi->drm_crtc.dev->event_lock);
			drm_crtc_send_vblank_event(&axi->drm_crtc, event);
			spin_unlock_irq(&axi->drm_crtc.dev->event_lock);
		}
		if (new_frame) {
			mutex_lock(&axi->submit_lock);
			axi_hdmi_remember_last_good(axi, fb, src, width, height,
						     pitch, serial, token);
			mutex_unlock(&axi->submit_lock);
		}
		drm_framebuffer_put(fb);
	}
	/* A repeat has NEW=0 and reuses the last serial; it owns no user event. */
	mutex_lock(&axi->submit_lock);
	if (axi->presentation_state == AXI_HDMI_RUNNING &&
	    axi->active_job == AXI_HDMI_NO_JOB && axi->last_good_fb &&
	    axi->last_good_serial) {
		struct dma_async_tx_descriptor *desc;
		struct drm_framebuffer *fb = axi->last_good_fb;
		u32 slot = axi->job_serial[0] ? 1 : 0;
		int ret;

		drm_framebuffer_get(fb);
		desc = axi_hdmi_vdma_prep_interleaved_desc(axi, slot, fb,
			axi->last_good_src, axi->last_good_width,
			axi->last_good_height);
		if (!desc) {
			drm_framebuffer_put(fb);
			spin_lock_irqsave(&p->presentation_lock, flags);
			p->presentation_error = true;
			spin_unlock_irqrestore(&p->presentation_lock, flags);
			goto out_unlock;
		}
		axi->callback[slot].axi = axi;
		axi->callback[slot].slot = slot;
		axi->callback[slot].generation = ++axi->job_generation[slot];
		desc->callback = axi_hdmi_dma_done;
		desc->callback_param = &axi->callback[slot];
		spin_lock_irqsave(&p->presentation_lock, flags);
		axi->job_serial[slot] = axi->last_good_serial;
		axi->job_token[slot] = axi->last_good_token;
		axi->job_fb[slot] = fb;
		axi->job_desc[slot] = desc;
		axi->job_dma_done[slot] = false;
		axi->job_eof_seen[slot] = false;
		axi->job_boundary_seen[slot] = false;
		axi->probe_boundary_ns[slot] = 0;
		axi->job_complete[slot] = false;
		axi->job_submitted[slot] = false;
		axi->job_new[slot] = false;
		axi->active_job = slot;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		ret = axi_hdmi_start_slot(axi, slot, false);
		if (ret) {
			spin_lock_irqsave(&p->presentation_lock, flags);
			axi->active_job = AXI_HDMI_NO_JOB;
			axi->job_fb[slot] = NULL;
			axi->job_desc[slot] = NULL;
			axi->job_serial[slot] = 0;
			axi->job_submitted[slot] = false;
			p->presentation_error = true;
			spin_unlock_irqrestore(&p->presentation_lock, flags);
			drm_framebuffer_put(fb);
		}
	}
out_unlock:
	mutex_unlock(&axi->submit_lock);
}

static void axi_hdmi_complete_work(struct work_struct *work)
{
	struct axi_hdmi_crtc *axi = container_of(work, struct axi_hdmi_crtc,
						     completion_work);
	u64 start, queued;
	unsigned long flags;
	if (!measure_refill) {
		axi_hdmi_complete_work_impl(work);
		return;
	}
	spin_lock_irqsave(&axi->private->presentation_lock, flags);
	start = ktime_get_mono_fast_ns();
	queued = refill_probe_take(&axi->probe_queued_ns);
	spin_unlock_irqrestore(&axi->private->presentation_lock, flags);
	probe_record(REFILL_QUEUE_TO_WORK, queued, start);
	axi_hdmi_complete_work_impl(work);
	probe_record(REFILL_WORK_DURATION, start, ktime_get_mono_fast_ns());
}

static void axi_hdmi_dma_done(void *arg)
{
	typeof(((struct axi_hdmi_crtc *)0)->callback[0]) *cb = arg;
	struct axi_hdmi_crtc *axi = cb->axi;
	struct axi_hdmi_private *p = axi->private;
	unsigned long flags;
	u8 slot = cb->slot;

	/* DMA callback is a memory-read completion only; it never ACKs a flip. */
	spin_lock_irqsave(&p->presentation_lock, flags);
	if (slot < ARRAY_SIZE(axi->job_dma_done) &&
	    axi->job_generation[slot] == cb->generation &&
	    (axi->active_job == slot || axi->pending_job == slot)) {
		axi->job_dma_done[slot] = true;
		if (axi->job_eof_seen[slot] && axi->job_boundary_seen[slot])
			axi->job_complete[slot] = true;
	}
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	axi_hdmi_queue_completion(axi);
}

static void axi_hdmi_plane_atomic_update(struct drm_plane *plane,
	struct drm_atomic_state *old_state)
{
	struct axi_hdmi_crtc *axi = plane_to_axi_hdmi_crtc(plane);
	struct axi_hdmi_private *p = axi->private;
	struct drm_plane_state *ps = plane->state;
	struct drm_gem_dma_object *obj;
	struct dma_async_tx_descriptor *desc;
	struct drm_pending_vblank_event *event = NULL;
	unsigned long flags;
	dma_addr_t src;
	u32 serial;
	unsigned int slot;
	int ret = 0;

	if (!ps->crtc || !ps->fb)
		return;
	if (ps->fb->format->format != DRM_FORMAT_XRGB8888 ||
	    ps->crtc_x || ps->crtc_y || ps->crtc_w != 1024 ||
	    ps->crtc_h != 600 || ps->src_x || ps->src_y ||
	    ps->src_w != (1024 << 16) || ps->src_h != (600 << 16)) {
		spin_lock_irqsave(&p->presentation_lock, flags);
		p->presentation_error = true;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		return;
	}
	obj = drm_fb_dma_get_gem_obj(ps->fb, 0);
	if (!obj)
		return;

	src = obj->dma_addr + (ps->src_x >> 16) * 4 +
		(ps->src_y >> 16) * ps->fb->pitches[0];
	mutex_lock(&axi->submit_lock);
	spin_lock_irqsave(&p->presentation_lock, flags);
	if (axi->presentation_state == AXI_HDMI_STOPPING ||
	    axi->presentation_state == AXI_HDMI_FAULT ||
	    (!p->presentation_ready && axi->presentation_state == AXI_HDMI_RUNNING)) {
		event = axi->latched_event;
		axi->latched_event = NULL;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		mutex_unlock(&axi->submit_lock);
		spin_lock_irqsave(&p->presentation_lock, flags);
		p->presentation_error = true;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		axi_hdmi_cancel_event(p->drm_dev, event);
		return;
	}
	if ((axi->active_job != AXI_HDMI_NO_JOB &&
	     axi->pending_job != AXI_HDMI_NO_JOB) ||
	    (axi->presentation_state == AXI_HDMI_PREPARED &&
	     axi->active_job != AXI_HDMI_NO_JOB)) {
		event = axi->latched_event;
		axi->latched_event = NULL;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		mutex_unlock(&axi->submit_lock);
		spin_lock_irqsave(&p->presentation_lock, flags);
		p->presentation_error = true;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		axi_hdmi_cancel_event(p->drm_dev, event);
		return;
	}
	slot = axi->active_job == AXI_HDMI_NO_JOB ? 0 : 1;
	if (axi->job_serial[slot])
		slot ^= 1;
	spin_unlock_irqrestore(&p->presentation_lock, flags);

	/* Preparation must succeed before the event or either job slot is published. */
	drm_framebuffer_get(ps->fb);
	desc = axi_hdmi_vdma_prep_interleaved_desc(axi, slot, ps->fb, src,
						   ps->crtc_w, ps->crtc_h);
	if (!desc) {
		struct drm_pending_vblank_event *cancel;

		drm_framebuffer_put(ps->fb);
		spin_lock_irqsave(&p->presentation_lock, flags);
		cancel = axi->latched_event;
		axi->latched_event = NULL;
		p->presentation_error = true;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		axi_hdmi_cancel_event(p->drm_dev, cancel);
		goto out_unlock;
	}
	axi->callback[slot].axi = axi;
	axi->callback[slot].slot = slot;
	axi->callback[slot].generation = ++axi->job_generation[slot];
	desc->callback = axi_hdmi_dma_done;
	desc->callback_param = &axi->callback[slot];
	spin_lock_irqsave(&p->presentation_lock, flags);
	event = axi->latched_event;
	axi->latched_event = NULL;
	serial = ++axi->next_serial & HPO_SERIAL_MASK;
	if (!serial)
		serial = ++axi->next_serial & HPO_SERIAL_MASK;
	/* Keep it PREPARED across commit_tail's planes-before-enables ordering. */
	axi->job_fb[slot] = ps->fb;
	axi->job_event[slot] = event;
	axi->job_desc[slot] = desc;
	axi->job_serial[slot] = serial;
	axi->job_token[slot] = event ? event->event.vbl.user_data : 0;
	axi->job_src[slot] = src;
	axi->job_width[slot] = ps->crtc_w;
	axi->job_height[slot] = ps->crtc_h;
	axi->job_pitch[slot] = ps->fb->pitches[0];
	axi->job_dma_done[slot] = false;
	axi->job_eof_seen[slot] = false;
	axi->job_boundary_seen[slot] = false;
	axi->probe_boundary_ns[slot] = 0;
	axi->job_complete[slot] = false;
	axi->job_submitted[slot] = false;
	axi->job_new[slot] = true;
	if (axi->active_job == AXI_HDMI_NO_JOB)
		axi->active_job = slot;
	else
		axi->pending_job = slot;
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	if (axi->presentation_state != AXI_HDMI_RUNNING) {
		axi->presentation_state = AXI_HDMI_PREPARED;
		goto out_unlock;
	}
	ret = axi_hdmi_start_slot(axi, slot, true);
	if (ret) {
		struct drm_framebuffer *fb;
		struct drm_pending_vblank_event *cancel;

		spin_lock_irqsave(&p->presentation_lock, flags);
		if (axi->active_job == slot)
			axi->active_job = AXI_HDMI_NO_JOB;
		else if (axi->pending_job == slot)
			axi->pending_job = AXI_HDMI_NO_JOB;
		fb = axi->job_fb[slot];
		cancel = axi->job_event[slot];
		axi->job_serial[slot] = 0;
		axi->job_event[slot] = NULL;
		axi->job_fb[slot] = NULL;
		axi->job_desc[slot] = NULL;
		axi->job_submitted[slot] = false;
		p->presentation_error = true;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		axi_hdmi_cancel_event(p->drm_dev, cancel);
		drm_framebuffer_put(fb);
		goto out_unlock;
	}
out_unlock:
	mutex_unlock(&axi->submit_lock);
}

static void axi_hdmi_crtc_enable(struct drm_crtc *crtc,
				 struct drm_atomic_state *old_state)
{
	struct axi_hdmi_crtc *axi = to_axi_hdmi_crtc(crtc);
	struct axi_hdmi_private *p = axi->private;
	struct drm_framebuffer *fb = NULL;
	struct drm_pending_vblank_event *cancel = NULL;
	unsigned long flags;
	u32 vsync = 0;
	int ret = 0;

	mutex_lock(&axi->submit_lock);
	/* commit_tail enables after plane update: flush before, never after, submit */
	if (axi->presentation_state == AXI_HDMI_STOPPING ||
	    axi->presentation_state == AXI_HDMI_FAULT)
	{
		ret = -EIO;
		goto out;
	}
	if (axi->presentation_state != AXI_HDMI_PREPARED ||
	    axi->active_job == AXI_HDMI_NO_JOB) {
		spin_lock_irqsave(&p->presentation_lock, flags);
		axi->presentation_state = AXI_HDMI_FAULT;
		p->presentation_ready = false;
		p->presentation_error = true;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		ret = -EIO;
		goto out;
	}
	drm_crtc_vblank_on(crtc);
	ret = drm_crtc_vblank_get(crtc);
	if (ret)
		goto out;
	axi->vblank_ref = true;
	if (p->observer_base) {
		writel(0, p->observer_base + HPO_IRQ_ENABLE);
		writel(0x2, p->observer_base + HPO_CONTROL);
		writel(HPO_IRQ_ALL, p->observer_base + HPO_IRQ_STATUS);
		writel(0x1, p->observer_base + HPO_CONTROL);
		vsync = readl(p->observer_base + HPO_VSYNC_COUNT);
	}
	axi->presentation_state = AXI_HDMI_RUNNING;
	if (axi->active_job != AXI_HDMI_NO_JOB)
		ret = axi_hdmi_start_slot(axi, axi->active_job, true);
	if (ret) {
		u8 slot = axi->active_job;

		spin_lock_irqsave(&p->presentation_lock, flags);
		fb = axi->job_fb[slot];
		cancel = axi->job_event[slot];
		axi->job_fb[slot] = NULL;
		axi->job_event[slot] = NULL;
		axi->job_desc[slot] = NULL;
		axi->job_serial[slot] = 0;
		axi->job_submitted[slot] = false;
		axi->active_job = AXI_HDMI_NO_JOB;
		axi->presentation_state = AXI_HDMI_FAULT;
		p->presentation_ready = false;
		p->presentation_error = true;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		if (p->observer_base)
			writel(0, p->observer_base + HPO_IRQ_ENABLE);
		axi_hdmi_cancel_event(p->drm_dev, cancel);
		drm_framebuffer_put(fb);
		goto out;
	}
	spin_lock_irqsave(&p->presentation_lock, flags);
	p->presentation_ready = true;
	p->presentation_error = false;
	p->presentation_session++;
	p->vsync_count = vsync;
	p->last_dma_eof_serial = 0;
	p->last_tx_boundary_serial = 0;
	p->last_dma_eof_token = 0;
	p->last_tx_boundary_token = 0;
	axi->last_irq_vsync = vsync;
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	if (p->observer_base)
		writel(HPO_IRQ_ALL | HPO_IRQ_PRESENT, p->observer_base + HPO_IRQ_ENABLE);
out:
	mutex_unlock(&axi->submit_lock);
	if (ret && axi->vblank_ref) {
		drm_crtc_vblank_put(crtc);
		drm_crtc_vblank_off(crtc);
		axi->vblank_ref = false;
	} else if (ret) {
		drm_crtc_vblank_off(crtc);
	}
}

static void axi_hdmi_crtc_disable(struct drm_crtc *crtc,
				  struct drm_atomic_state *old_state)
{
	struct axi_hdmi_crtc *axi_hdmi_crtc = to_axi_hdmi_crtc(crtc);

	/* Normal flips never stop the channel.  Shutdown is the sole stop path. */
	axi_hdmi_presentation_shutdown(axi_hdmi_crtc->private);
}

static void axi_hdmi_crtc_atomic_begin(struct drm_crtc *crtc,
	struct drm_atomic_state *state)
{
	struct axi_hdmi_crtc *axi = to_axi_hdmi_crtc(crtc);
	struct drm_pending_vblank_event *stale;
	unsigned long flags;

	/* Hold the standard page-flip event until HPO1 reports its boundary. */
	spin_lock_irqsave(&axi->private->presentation_lock, flags);
	stale = axi->latched_event;
	axi->latched_event = crtc->state->event;
	crtc->state->event = NULL;
	spin_unlock_irqrestore(&axi->private->presentation_lock, flags);
	axi_hdmi_cancel_event(axi->private->drm_dev, stale);
}

static int axi_hdmi_crtc_enable_vblank(struct drm_crtc *crtc)
{
	struct axi_hdmi_crtc *axi = to_axi_hdmi_crtc(crtc);

	return axi->private->observer_base ? 0 : -ENODEV;
}

static void axi_hdmi_crtc_disable_vblank(struct drm_crtc *crtc)
{
}

/* Runs after plane updates; consumes an event from a disabling/no-plane commit. */
static void axi_hdmi_crtc_atomic_flush(struct drm_crtc *crtc,
	struct drm_atomic_state *state)
{
	struct axi_hdmi_crtc *axi = to_axi_hdmi_crtc(crtc);
	struct drm_pending_vblank_event *event;
	unsigned long flags;

	spin_lock_irqsave(&axi->private->presentation_lock, flags);
	event = axi->latched_event;
	axi->latched_event = NULL;
	spin_unlock_irqrestore(&axi->private->presentation_lock, flags);
	/* A normal plane update has already moved this to its per-job storage. */
	axi_hdmi_cancel_event(axi->private->drm_dev, event);
}

static const struct drm_crtc_helper_funcs axi_hdmi_crtc_helper_funcs = {
	.atomic_enable = axi_hdmi_crtc_enable,
	.atomic_disable = axi_hdmi_crtc_disable,
	.atomic_begin = axi_hdmi_crtc_atomic_begin,
	.atomic_flush = axi_hdmi_crtc_atomic_flush,
};

static void axi_hdmi_crtc_destroy(struct drm_crtc *crtc)
{
	struct axi_hdmi_crtc *axi_hdmi_crtc = to_axi_hdmi_crtc(crtc);

	cancel_work_sync(&axi_hdmi_crtc->fault_work);
	drm_crtc_cleanup(crtc);
	axi_hdmi_presentation_shutdown(axi_hdmi_crtc->private);
	kfree(axi_hdmi_crtc->dma_template[0]);
	kfree(axi_hdmi_crtc->dma_template[1]);
	kfree(axi_hdmi_crtc);
}

static const struct drm_crtc_funcs axi_hdmi_crtc_funcs = {
	.destroy = axi_hdmi_crtc_destroy,
	.set_config = drm_atomic_helper_set_config,
	.page_flip = drm_atomic_helper_page_flip,
	.reset = drm_atomic_helper_crtc_reset,
	.atomic_duplicate_state = drm_atomic_helper_crtc_duplicate_state,
	.atomic_destroy_state = drm_atomic_helper_crtc_destroy_state,
	.enable_vblank = axi_hdmi_crtc_enable_vblank,
	.disable_vblank = axi_hdmi_crtc_disable_vblank,
};

static const struct drm_plane_helper_funcs axi_hdmi_plane_helper_funcs = {
	.atomic_update = axi_hdmi_plane_atomic_update,
};

static void axi_hdmi_plane_destroy(struct drm_plane *plane)
{
	drm_plane_cleanup(plane);
}

static const struct drm_plane_funcs axi_hdmi_plane_funcs = {
	.update_plane = drm_atomic_helper_update_plane,
	.disable_plane = drm_atomic_helper_disable_plane,
	.destroy = axi_hdmi_plane_destroy,
	.reset = drm_atomic_helper_plane_reset,
	.atomic_duplicate_state = drm_atomic_helper_plane_duplicate_state,
	.atomic_destroy_state = drm_atomic_helper_plane_destroy_state,
};

static const u32 axi_hdmi_supported_formats[] = {
	DRM_FORMAT_XRGB8888,
};

irqreturn_t axi_hdmi_presentation_irq(struct axi_hdmi_private *p)
{
	struct axi_hdmi_crtc *axi;
	unsigned long flags;
	u32 status, count, serial_word, serial, beats, eof_vsync, boundary_vsync;
	u32 vsync = 0, delta = 0;
	unsigned int i, slot, vblank_count = 0;

	if (!p->observer_base)
		return IRQ_NONE;
	status = readl(p->observer_base + HPO_IRQ_STATUS);
	if (!status)
		return IRQ_NONE;
	spin_lock_irqsave(&p->presentation_lock, flags);
	if (!p->presentation_ready || !p->crtc) {
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		return IRQ_NONE;
	}
	axi = to_axi_hdmi_crtc(p->crtc);
	if (axi->presentation_state != AXI_HDMI_RUNNING) {
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		return IRQ_NONE;
	}
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	vsync = readl(p->observer_base + HPO_VSYNC_COUNT);
	/* FIFO head stays stable until the full-width POP write below. */
	count = readl(p->observer_base + HPO_FIFO_COUNT);
	spin_lock_irqsave(&p->presentation_lock, flags);
	p->vsync_count = vsync;
	if (status & HPO_IRQ_VSYNC) {
		delta = vsync - axi->last_irq_vsync;
		if (!delta || delta > HPO_FIFO_DEPTH) {
			p->presentation_error = true;
		} else {
			axi->last_irq_vsync = vsync;
			vblank_count = delta;
		}
	}
	if (count > HPO_FIFO_DEPTH) {
		p->presentation_error = true;
		count = HPO_FIFO_DEPTH;
	}
	if (status & (HPO_IRQ_QUEUE_FULL | HPO_IRQ_PROTOCOL))
		p->presentation_error = true;
	for (i = 0; i < count; i++) {
		serial_word = readl(p->observer_base + HPO_FIFO_HEAD_SERIAL);
		serial = serial_word & HPO_SERIAL_MASK;
		beats = readl(p->observer_base + HPO_FIFO_HEAD_BEATS);
		eof_vsync = readl(p->observer_base + HPO_FIFO_EOF_VSYNC);
		boundary_vsync = readl(p->observer_base + HPO_FIFO_BOUNDARY_VSYNC);
		slot = AXI_HDMI_NO_JOB;
		if (axi->active_job != AXI_HDMI_NO_JOB &&
		    axi->job_serial[axi->active_job] == serial)
			slot = axi->active_job;
		else if (axi->pending_job != AXI_HDMI_NO_JOB &&
			 axi->job_serial[axi->pending_job] == serial)
			slot = axi->pending_job;
		if (!serial || slot == AXI_HDMI_NO_JOB ||
		    !!(serial_word & HPO_NEW) != axi->job_new[slot] ||
		    beats != HPO_EXPECTED_BEATS ||
		    boundary_vsync < eof_vsync + 1) {
			p->presentation_error = true;
		} else {
			if (measure_refill && !axi->job_boundary_seen[slot])
				axi->probe_boundary_ns[slot] = ktime_get_mono_fast_ns();
			axi->job_eof_seen[slot] = true;
			axi->job_boundary_seen[slot] = true;
			p->last_dma_eof_serial = serial;
			p->last_dma_eof_token = axi->job_token[slot];
			p->last_tx_boundary_serial = serial;
			p->last_tx_boundary_token = axi->job_token[slot];
			if (axi->job_dma_done[slot])
				axi->job_complete[slot] = true;
		}
		writel(1, p->observer_base + HPO_FIFO_POP);
	}
	if (p->presentation_error) {
		axi->presentation_state = AXI_HDMI_FAULT;
		p->presentation_ready = false;
	}
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	if (READ_ONCE(axi->presentation_state) == AXI_HDMI_FAULT)
		writel(0, p->observer_base + HPO_IRQ_ENABLE);
	if (READ_ONCE(axi->presentation_state) == AXI_HDMI_FAULT)
		schedule_work(&axi->fault_work);
	while (vblank_count--)
		drm_crtc_handle_vblank(p->crtc);
	/* W1C old edge/fault causes only; PRESENT remains level while nonempty. */
	writel(status & HPO_IRQ_ALL, p->observer_base + HPO_IRQ_STATUS);
	axi_hdmi_queue_completion(axi);
	return IRQ_HANDLED;
}

static void axi_hdmi_presentation_stop(struct axi_hdmi_private *p,
					       bool permanent, bool fault)
{
	struct axi_hdmi_crtc *axi;
	struct drm_pending_vblank_event *event[2] = { NULL, NULL };
	struct drm_pending_vblank_event *latched = NULL;
	struct drm_framebuffer *fb[2] = { NULL, NULL };
	struct drm_framebuffer *last_good = NULL;
	unsigned long flags;
	unsigned int i;
	bool stale_fault = false;

	if (!p || !p->crtc)
		return;
	axi = to_axi_hdmi_crtc(p->crtc);
	mutex_lock(&axi->teardown_lock);
	/* A queued fault work item must not overwrite a completed disable/remove. */
	if (fault) {
		spin_lock_irqsave(&p->presentation_lock, flags);
		stale_fault = axi->presentation_state != AXI_HDMI_FAULT ||
			axi->permanent_stop;
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		if (stale_fault) {
			mutex_unlock(&axi->teardown_lock);
			return;
		}
	}

	/* Block every future submit before waiting for the current one to leave. */
	mutex_lock(&axi->submit_lock);
	spin_lock_irqsave(&p->presentation_lock, flags);
	if (permanent)
		axi->permanent_stop = true;
	axi->presentation_state = AXI_HDMI_STOPPING;
	p->presentation_ready = false;
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	mutex_unlock(&axi->submit_lock);

	if (p->observer_base)
		writel(0, p->observer_base + HPO_IRQ_ENABLE);
	if (p->observer_irq > 0)
		synchronize_irq(p->observer_irq);
	if (axi->vblank_ref) {
		drm_crtc_vblank_put(&axi->drm_crtc);
		drm_crtc_vblank_off(&axi->drm_crtc);
		axi->vblank_ref = false;
	}
	dmaengine_terminate_sync(axi->dma);
	dmaengine_synchronize(axi->dma);
	cancel_work_sync(&axi->completion_work);

	mutex_lock(&axi->submit_lock);
	spin_lock_irqsave(&p->presentation_lock, flags);
	for (i = 0; i < ARRAY_SIZE(axi->job_fb); i++) {
		event[i] = axi->job_event[i];
		axi->job_event[i] = NULL;
		fb[i] = axi->job_fb[i];
		axi->job_fb[i] = NULL;
		axi->job_serial[i] = 0;
		axi->job_complete[i] = false;
		axi->job_dma_done[i] = false;
		axi->job_eof_seen[i] = false;
		axi->job_boundary_seen[i] = false;
		axi->probe_boundary_ns[i] = 0;
		axi->job_submitted[i] = false;
		axi->job_new[i] = false;
		axi->job_generation[i]++;
		axi->job_desc[i] = NULL;
	}
	latched = axi->latched_event;
	axi->latched_event = NULL;
	axi->probe_queued_ns = 0;
	axi->probe_retired_boundary_ns = 0;
	last_good = axi->last_good_fb;
	axi->last_good_fb = NULL;
	axi->last_good_serial = 0;
	axi->active_job = AXI_HDMI_NO_JOB;
	axi->pending_job = AXI_HDMI_NO_JOB;
	p->presentation_session++;
	p->presentation_error = fault;
	p->last_dma_eof_serial = 0;
	p->last_tx_boundary_serial = 0;
	p->last_dma_eof_token = 0;
	p->last_tx_boundary_token = 0;
	/* STOPPING is terminal only for device removal; normal disable is reusable. */
	axi->presentation_state = axi->permanent_stop ? AXI_HDMI_STOPPING :
		(fault ? AXI_HDMI_FAULT : AXI_HDMI_OFF);
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	mutex_unlock(&axi->submit_lock);

	for (i = 0; i < ARRAY_SIZE(fb); i++) {
		axi_hdmi_cancel_event(p->drm_dev, event[i]);
		if (fb[i])
			drm_framebuffer_put(fb[i]);
	}
	axi_hdmi_cancel_event(p->drm_dev, latched);
	if (last_good)
		drm_framebuffer_put(last_good);
	mutex_unlock(&axi->teardown_lock);
}

static void axi_hdmi_fault_work(struct work_struct *work)
{
	struct axi_hdmi_crtc *axi = container_of(work, struct axi_hdmi_crtc,
						     fault_work);

	axi_hdmi_presentation_stop(axi->private, false, true);
}

void axi_hdmi_presentation_shutdown(struct axi_hdmi_private *p)
{
	if (p && p->crtc)
		cancel_work_sync(&to_axi_hdmi_crtc(p->crtc)->fault_work);
	axi_hdmi_presentation_stop(p, false, false);
}

void axi_hdmi_presentation_remove(struct axi_hdmi_private *p)
{
	if (p && p->crtc)
		cancel_work_sync(&to_axi_hdmi_crtc(p->crtc)->fault_work);
	axi_hdmi_presentation_stop(p, true, false);
}

void axi_hdmi_presentation_fault(struct axi_hdmi_private *p)
{
	struct axi_hdmi_crtc *axi;
	unsigned long flags;

	if (!p || !p->crtc)
		return;
	axi = to_axi_hdmi_crtc(p->crtc);
	spin_lock_irqsave(&p->presentation_lock, flags);
	axi->presentation_state = AXI_HDMI_FAULT;
	p->presentation_ready = false;
	p->presentation_error = true;
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	if (p->observer_base)
		writel(0, p->observer_base + HPO_IRQ_ENABLE);
	schedule_work(&axi->fault_work);
}

int axi_hdmi_presentation_atomic_check(struct drm_device *dev,
				       struct drm_atomic_state *state)
{
	struct axi_hdmi_private *p = dev->dev_private;
	struct axi_hdmi_crtc *axi;
	struct drm_crtc_state *cs;
	struct drm_plane_state *ps;
	unsigned long flags;
	bool full;

	if (!p || !p->crtc || !p->crtc->primary)
		return -ENODEV;
	if (!p->observer_base || !(p->observer_caps & HPO_CAP_EVENT_FIFO))
		return -ENODEV;
	ps = drm_atomic_get_new_plane_state(state, p->crtc->primary);
	if (!ps || !ps->crtc || !ps->fb)
		return 0;
	if (ps->fb->format->format != DRM_FORMAT_XRGB8888 ||
	    ps->crtc_x || ps->crtc_y || ps->crtc_w != 1024 ||
	    ps->crtc_h != 600 || ps->src_x || ps->src_y ||
	    ps->src_w != (1024 << 16) || ps->src_h != (600 << 16))
		return -EINVAL;
	axi = to_axi_hdmi_crtc(p->crtc);
	cs = drm_atomic_get_new_crtc_state(state, p->crtc);
	if (!cs || (cs->active &&
	    (cs->mode.hdisplay != 1024 || cs->mode.vdisplay != 600 ||
	     (cs->mode.flags & DRM_MODE_FLAG_INTERLACE) ||
	     cs->adjusted_mode.hdisplay != 1024 ||
	     cs->adjusted_mode.vdisplay != 600 ||
	     (cs->adjusted_mode.flags & DRM_MODE_FLAG_INTERLACE))))
		return -EINVAL;
	spin_lock_irqsave(&p->presentation_lock, flags);
	if (axi->presentation_state == AXI_HDMI_STOPPING) {
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		return -ESHUTDOWN;
	}
	if (axi->presentation_state == AXI_HDMI_FAULT) {
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		return -EIO;
	}
	/* OFF is legal only for the active modeset that will call atomic_enable. */
	if (axi->presentation_state == AXI_HDMI_OFF && (!cs || !cs->active)) {
		spin_unlock_irqrestore(&p->presentation_lock, flags);
		return -EINVAL;
	}
	full = (axi->active_job != AXI_HDMI_NO_JOB &&
		axi->pending_job != AXI_HDMI_NO_JOB) ||
		(axi->presentation_state == AXI_HDMI_PREPARED &&
		axi->active_job != AXI_HDMI_NO_JOB);
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	return full ? -EBUSY : 0;
}

int axi_hdmi_presentation_get_info(struct axi_hdmi_private *p, void *data)
{
	struct drm_adi_hdmi_presentation_info *info = data;
	unsigned long flags;

	if (info->size < sizeof(*info))
		return -EINVAL;
	memset(info, 0, sizeof(*info));
	info->size = sizeof(*info);
	info->abi_major = DRM_ADI_HDMI_PRESENT_ABI_MAJOR;
	info->abi_minor = DRM_ADI_HDMI_PRESENT_ABI_MINOR;
	info->driver_magic = DRM_ADI_HDMI_PRESENT_DRIVER_MAGIC;
	info->capabilities = DRM_ADI_HDMI_PRESENT_CAP_REAL_VSYNC_EOF |
		DRM_ADI_HDMI_PRESENT_CAP_TOKEN_EVENT |
		DRM_ADI_HDMI_PRESENT_CAP_REPEAT_FLAG;
	info->observer_id = 0x48504f31;
	info->observer_version = p->observer_version;
	spin_lock_irqsave(&p->presentation_lock, flags);
	info->status = p->presentation_ready ? DRM_ADI_HDMI_PRESENT_STATUS_READY : 0;
	if (p->presentation_error)
		info->status |= DRM_ADI_HDMI_PRESENT_STATUS_ERROR;
	info->status |= DRM_ADI_HDMI_PRESENT_STATUS_VISUAL_UNKNOWN;
	info->session = p->presentation_session;
	info->last_dma_eof_token = p->last_dma_eof_token;
	info->last_tx_boundary_token = p->last_tx_boundary_token;
	info->last_dma_eof_serial = p->last_dma_eof_serial;
	info->last_tx_boundary_serial = p->last_tx_boundary_serial;
	info->vsync_count = p->vsync_count;
	spin_unlock_irqrestore(&p->presentation_lock, flags);
	return 0;
}

struct drm_crtc *axi_hdmi_crtc_create(struct drm_device *dev)
{
	struct axi_hdmi_private *p = dev->dev_private;
	struct axi_hdmi_crtc *axi_hdmi_crtc;
	struct drm_crtc *crtc;
	struct drm_plane *plane;
	int ret;

	if (!dma_has_cap(DMA_INTERLEAVE, p->dma->device->cap_mask)) {
		DRM_ERROR("DMA needs to support interleaved transfers\n");
		return ERR_PTR(-EINVAL);
	}

	axi_hdmi_crtc = kzalloc(sizeof(*axi_hdmi_crtc), GFP_KERNEL);
	if (!axi_hdmi_crtc)
		return ERR_PTR(-ENOMEM);

	crtc = &axi_hdmi_crtc->drm_crtc;
	plane = &axi_hdmi_crtc->plane;

	/* Two descriptors have separate immutable storage; never mutate in flight. */
	axi_hdmi_crtc->dma_template[0] = kzalloc(
		sizeof(struct dma_interleaved_template) +
		sizeof(struct data_chunk), GFP_KERNEL);
	axi_hdmi_crtc->dma_template[1] = kzalloc(
		sizeof(struct dma_interleaved_template) +
		sizeof(struct data_chunk), GFP_KERNEL);
	if (!axi_hdmi_crtc->dma_template[0] || !axi_hdmi_crtc->dma_template[1]) {
		ret = -ENOMEM;
		goto err_free_dma_template;
	}

	ret = drm_universal_plane_init(dev, plane, 0xff, &axi_hdmi_plane_funcs,
		axi_hdmi_supported_formats,
		ARRAY_SIZE(axi_hdmi_supported_formats), NULL,
		DRM_PLANE_TYPE_PRIMARY, NULL);
	if (ret)
		goto err_free_dma_template;

	drm_plane_helper_add(plane, &axi_hdmi_plane_helper_funcs);

	axi_hdmi_crtc->dma = p->dma;
	axi_hdmi_crtc->private = p;
	mutex_init(&axi_hdmi_crtc->submit_lock);
	mutex_init(&axi_hdmi_crtc->teardown_lock);
	INIT_WORK(&axi_hdmi_crtc->completion_work, axi_hdmi_complete_work);
	INIT_WORK(&axi_hdmi_crtc->fault_work, axi_hdmi_fault_work);
	spin_lock_irq(&p->presentation_lock);
	axi_hdmi_crtc->active_job = AXI_HDMI_NO_JOB;
	axi_hdmi_crtc->pending_job = AXI_HDMI_NO_JOB;
	spin_unlock_irq(&p->presentation_lock);

	ret = drm_crtc_init_with_planes(dev, crtc, plane, NULL,
		&axi_hdmi_crtc_funcs, NULL);
	if (ret)
		goto err_plane_destroy;
	drm_crtc_helper_add(crtc, &axi_hdmi_crtc_helper_funcs);

	return crtc;

err_plane_destroy:
	axi_hdmi_plane_destroy(plane);
err_free_dma_template:
	kfree(axi_hdmi_crtc->dma_template[1]);
	kfree(axi_hdmi_crtc->dma_template[0]);
	kfree(axi_hdmi_crtc);

	return ERR_PTR(ret);
}
