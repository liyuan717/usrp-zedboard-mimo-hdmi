// SPDX-License-Identifier: GPL-2.0
/* Private, single-owner Detector DMA driver.  Never owns AD9361/IIO/ADI DMAC. */
#include <linux/atomic.h>
#include <linux/dma-mapping.h>
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/jiffies.h>
#include <linux/kernel.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/uaccess.h>
#include <linux/wait.h>

#include "uapi/zedboard_detector.h"

#define DRIVER_NAME "zedboard-detector"
#define DEVICE_NAME "zed-mimo-detector"
#define DET_CORE_ID 0x00
#define DET_CORE_VERSION 0x04
#define DET_CONTROL 0x08
#define DET_STATUS 0x0c
#define DET_ERROR_CODE 0x10
#define DET_BUILD_ID 0x3c
#define DET_EXPECTED_CORE_ID 0x4d484744
#define DET_EXPECTED_CORE_VERSION 0x00010000
#define DET_CTRL_START_IRQ 0x00000101
#define DET_CTRL_ABORT_IRQ 0x00000102
#define DET_CTRL_SOFT_RESET_IRQ 0x00000104
#define DET_STATUS_DONE 0x08
#define DET_STATUS_ERROR 0x10
#define DET_STATUS_STICKY 0x38

#define DMA_MM2S_DMACR 0x00
#define DMA_MM2S_DMASR 0x04
#define DMA_MM2S_SA 0x18
#define DMA_MM2S_LENGTH 0x28
#define DMA_S2MM_DMACR 0x30
#define DMA_S2MM_DMASR 0x34
#define DMA_S2MM_DA 0x48
#define DMA_S2MM_LENGTH 0x58
#define DMA_DMACR_RUNSTOP 0x00000001
#define DMA_DMACR_RESET 0x00000004
#define DMA_DMACR_IRQ_EN 0x00007000
#define DMA_DMASR_ERR 0x00000070
#define DMA_DMASR_IOC 0x00001000
#define DMA_DMASR_IRQ 0x00007000

struct zed_detector {
	struct device *dev;
	void __iomem *det;
	void __iomem *dma;
	int irq[3];
	bool irq_ready;
	struct miscdevice misc;
	struct mutex hw_lock; /* held only by the single hardware owner */
	spinlock_t event_lock;
	wait_queue_head_t event_waitq;
	wait_queue_head_t state_waitq;
	atomic_t opened;
	atomic_t active;
	atomic_t cancel_requested;
	bool removing;
	bool poisoned;
	bool poison_module_ref;
	bool result_valid;
	atomic_t event_seq;
	u32 detector_events;
	u32 mm2s_events;
	u32 s2mm_events;
	void *input_cpu;
	dma_addr_t input_dma;
	void *output_cpu;
	dma_addr_t output_dma;
	int last_error;
};

static inline u32 det_read(struct zed_detector *d, u32 offset) { return readl(d->det + offset); }
static inline void det_write(struct zed_detector *d, u32 offset, u32 value) { writel(value, d->det + offset); }
static inline u32 dma_read(struct zed_detector *d, u32 offset) { return readl(d->dma + offset); }
static inline void dma_write(struct zed_detector *d, u32 offset, u32 value) { writel(value, d->dma + offset); }

static void zed_clear_events(struct zed_detector *d)
{
	unsigned long flags;
	spin_lock_irqsave(&d->event_lock, flags);
	d->detector_events = 0;
	d->mm2s_events = 0;
	d->s2mm_events = 0;
	spin_unlock_irqrestore(&d->event_lock, flags);
}

static bool zed_detector_identity_ok(struct zed_detector *d)
{
	return det_read(d, DET_CORE_ID) == DET_EXPECTED_CORE_ID &&
	       det_read(d, DET_CORE_VERSION) == DET_EXPECTED_CORE_VERSION;
}

static void zed_snapshot_events(struct zed_detector *d, u32 *detector, u32 *mm2s, u32 *s2mm)
{
	unsigned long flags;
	spin_lock_irqsave(&d->event_lock, flags);
	*detector = d->detector_events;
	*mm2s = d->mm2s_events;
	*s2mm = d->s2mm_events;
	spin_unlock_irqrestore(&d->event_lock, flags);
}

static irqreturn_t zed_detector_irq(int irq, void *opaque)
{
	struct zed_detector *d = opaque;
	unsigned long flags;
	u32 pending;

	spin_lock_irqsave(&d->event_lock, flags);
	if (irq == d->irq[0]) {
		pending = det_read(d, DET_STATUS) & DET_STATUS_STICKY;
		if (pending) {
			d->detector_events |= pending;
			det_write(d, DET_STATUS, pending); /* deassert level IRQ */
		}
	} else if (irq == d->irq[1]) {
		pending = dma_read(d, DMA_MM2S_DMASR) & DMA_DMASR_IRQ;
		if (pending) {
			d->mm2s_events |= pending; /* preserve IOC/ERR before W1C */
			dma_write(d, DMA_MM2S_DMASR, pending);
		}
	} else if (irq == d->irq[2]) {
		pending = dma_read(d, DMA_S2MM_DMASR) & DMA_DMASR_IRQ;
		if (pending) {
			d->s2mm_events |= pending; /* preserve IOC/ERR before W1C */
			dma_write(d, DMA_S2MM_DMASR, pending);
		}
	} else {
		pending = 0;
	}
	spin_unlock_irqrestore(&d->event_lock, flags);
	if (!pending)
		return IRQ_NONE;
	/* Status shadows are sticky for this transaction.  A separate sequence is
	 * consumed by waiters, preventing a first MM2S event from busy-spinning. */
	atomic_inc(&d->event_seq);
	wake_up_interruptible(&d->event_waitq);
	return IRQ_HANDLED;
}

static void zed_mark_poisoned_locked(struct zed_detector *d)
{
	if (!d->poisoned) {
		d->poisoned = true;
		/* Pins the module until an explicit successful RESET proves DMA stopped. */
		d->poison_module_ref = try_module_get(THIS_MODULE);
	}
}

static void zed_clear_poisoned_locked(struct zed_detector *d)
{
	if (d->poisoned && d->poison_module_ref) {
		d->poison_module_ref = false;
		module_put(THIS_MODULE);
	}
	d->poisoned = false;
}

static int zed_reset_dma_locked(struct zed_detector *d, unsigned long deadline)
{
	dma_write(d, DMA_MM2S_DMACR, DMA_DMACR_RESET);
	dma_write(d, DMA_S2MM_DMACR, DMA_DMACR_RESET);
	do {
		if (!(dma_read(d, DMA_MM2S_DMACR) & DMA_DMACR_RESET) &&
		    !(dma_read(d, DMA_S2MM_DMACR) & DMA_DMACR_RESET)) {
			dma_write(d, DMA_MM2S_DMASR, DMA_DMASR_IRQ);
			dma_write(d, DMA_S2MM_DMASR, DMA_DMASR_IRQ);
			zed_clear_events(d);
			return 0;
		}
		usleep_range(100, 250);
	} while (time_before(jiffies, deadline));
	return -ETIMEDOUT;
}

static int zed_recover_locked(struct zed_detector *d)
{
	unsigned long recovery_deadline = jiffies + msecs_to_jiffies(1000);
	int ret;

	det_write(d, DET_CONTROL, DET_CTRL_ABORT_IRQ);
	ret = zed_reset_dma_locked(d, recovery_deadline);
	if (ret) {
		zed_mark_poisoned_locked(d);
		return ret;
	}
	det_write(d, DET_STATUS, DET_STATUS_STICKY);
	if (!zed_detector_identity_ok(d) || !(det_read(d, DET_STATUS) & 0x01)) {
		zed_mark_poisoned_locked(d);
		return -EIO;
	}
	return 0;
}

static int zed_wait_complete_locked(struct zed_detector *d, unsigned long deadline)
{
	u32 mm2s, s2mm, status, det_irq, mm2s_irq, s2mm_irq;
	long remaining, waited;
	int event_seq = atomic_read(&d->event_seq);

	for (;;) {
		if (atomic_read(&d->cancel_requested))
			return -ECANCELED;
		mm2s = dma_read(d, DMA_MM2S_DMASR);
		s2mm = dma_read(d, DMA_S2MM_DMASR);
		status = det_read(d, DET_STATUS);
		zed_snapshot_events(d, &det_irq, &mm2s_irq, &s2mm_irq);
		if ((mm2s | s2mm | mm2s_irq | s2mm_irq) & DMA_DMASR_ERR)
			return -EIO;
		if (((mm2s | mm2s_irq) & DMA_DMASR_IOC) &&
		    ((s2mm | s2mm_irq) & DMA_DMASR_IOC) &&
		    ((status | det_irq) & (DET_STATUS_DONE | DET_STATUS_ERROR)))
			return 0;
		if (!time_before(jiffies, deadline))
			return -ETIMEDOUT;
		remaining = deadline - jiffies;
		if (d->irq_ready) {
			waited = wait_event_interruptible_timeout(d->event_waitq,
				atomic_read(&d->cancel_requested) ||
				atomic_read(&d->event_seq) != event_seq,
				min_t(long, remaining, max_t(long, 1, msecs_to_jiffies(10))));
			if (waited < 0)
				return (int)waited;
			event_seq = atomic_read(&d->event_seq);
		}
		else
			usleep_range(200, 500);
	}
}

static int zed_validate_submit(const struct zed_detector_submit *submit)
{
	if (submit->input_bytes != ZED_DETECTOR_INPUT_BYTES ||
	    submit->output_bytes != ZED_DETECTOR_OUTPUT_BYTES || !submit->timeout_ms ||
	    submit->timeout_ms > 60000 || submit->flags)
		return -EINVAL;
	return 0;
}

static int zed_submit_locked(struct zed_detector *d,
			     const struct zed_detector_submit *submit,
			     unsigned long deadline)
{
	u32 mm2s, s2mm;
	int ret, recovery;

	ret = zed_validate_submit(submit);
	if (ret)
		return ret;
	if (upper_32_bits(d->input_dma) || upper_32_bits(d->output_dma) ||
	    (d->input_dma & 0x7) || (d->output_dma & 0x7))
		return -ERANGE;
	memcpy(d->input_cpu, submit->input, ZED_DETECTOR_INPUT_BYTES);
	memset(d->output_cpu, 0, ZED_DETECTOR_OUTPUT_BYTES);
	WRITE_ONCE(d->result_valid, false);
	det_write(d, DET_STATUS, DET_STATUS_STICKY);
	dma_write(d, DMA_MM2S_DMASR, DMA_DMASR_IRQ);
	dma_write(d, DMA_S2MM_DMASR, DMA_DMASR_IRQ);
	zed_clear_events(d);

	/* S2MM first; exactly 64 bytes, then arm Detector and send 120 bytes. */
	dma_write(d, DMA_S2MM_DMACR, DMA_DMACR_RUNSTOP | DMA_DMACR_IRQ_EN);
	dma_write(d, DMA_S2MM_DA, lower_32_bits(d->output_dma));
	dma_write(d, DMA_S2MM_LENGTH, ZED_DETECTOR_OUTPUT_BYTES);
	det_write(d, DET_CONTROL, DET_CTRL_START_IRQ);
	dma_write(d, DMA_MM2S_DMACR, DMA_DMACR_RUNSTOP | DMA_DMACR_IRQ_EN);
	dma_write(d, DMA_MM2S_SA, lower_32_bits(d->input_dma));
	dma_write(d, DMA_MM2S_LENGTH, ZED_DETECTOR_INPUT_BYTES);
	ret = zed_wait_complete_locked(d, deadline);
	mm2s = dma_read(d, DMA_MM2S_DMASR);
	s2mm = dma_read(d, DMA_S2MM_DMASR);
	if (!ret && ((mm2s | s2mm) & DMA_DMASR_ERR))
		ret = -EIO;
	if (ret) {
		recovery = zed_recover_locked(d); /* independent deadline, never stale */
		if (recovery)
			dev_err(d->dev, "DMA recovery failed after submit error %d: %d\n", ret, recovery);
		return ret;
	}
	/* A Detector ERROR can deliberately accompany a complete 64-byte
	 * STREAM_ERROR reply.  Both DMA IOCs prove that the framed reply is safe
	 * for userspace to decode; a missing S2MM IOC still times out/recoveries. */
	WRITE_ONCE(d->result_valid, true);
	det_write(d, DET_STATUS, DET_STATUS_STICKY);
	return 0;
}

static int zed_validate_batch(const struct zed_detector_batch *batch)
{
	u32 i;
	int ret;

	if (!batch->count || batch->count > ZED_DETECTOR_BATCH_MAX_TASKS ||
	    !batch->total_timeout_ms ||
	    batch->total_timeout_ms > ZED_DETECTOR_BATCH_TIMEOUT_MAX_MS ||
	    batch->reserved[0] || batch->reserved[1])
		return -EINVAL;
	for (i = 0; i < ZED_DETECTOR_BATCH_MAX_TASKS; i++) {
		/* Result storage is output-only.  Requiring zero removes stale-input
		 * ambiguity and prevents a failed batch from looking partially valid. */
		if (memchr_inv(&batch->result[i], 0, sizeof(batch->result[i])))
			return -EINVAL;
		if (i < batch->count) {
			ret = zed_validate_submit(&batch->submit[i]);
			if (ret)
				return ret;
		} else if (memchr_inv(&batch->submit[i], 0,
					 sizeof(batch->submit[i]))) {
			return -EINVAL;
		}
	}
	return 0;
}

static int zed_submit_batch_locked(struct zed_detector *d,
				   struct zed_detector_batch *batch)
{
	unsigned long batch_deadline, task_deadline;
	u32 i;
	int ret, recovery;

	batch_deadline = jiffies +
		max_t(unsigned long, 1, msecs_to_jiffies(batch->total_timeout_ms));
	WRITE_ONCE(d->result_valid, false);
	for (i = 0; i < batch->count; i++) {
		/* Do not start another independent 120B/64B transaction once an
		 * ABORT won the race or the one total batch budget has expired. */
		if (atomic_read(&d->cancel_requested)) {
			ret = -ECANCELED;
			goto recover;
		}
		if (!time_before(jiffies, batch_deadline)) {
			ret = -ETIMEDOUT;
			goto recover;
		}
		task_deadline = jiffies + max_t(unsigned long, 1,
			msecs_to_jiffies(batch->submit[i].timeout_ms));
		if (time_after(task_deadline, batch_deadline))
			task_deadline = batch_deadline;
		ret = zed_submit_locked(d, &batch->submit[i], task_deadline);
		if (ret)
			goto failed;
		memcpy(batch->result[i].output, d->output_cpu,
		       ZED_DETECTOR_OUTPUT_BYTES);
	}
	/* Batch replies are delivered only by the IOWR copy below.  Do not let
	 * legacy GET_RESULT expose the final member as a misleading partial API. */
	WRITE_ONCE(d->result_valid, false);
	return 0;

recover:
	/* Expiry/cancel between items has no active transfer, but an explicit
	 * abort+DMA reset proves the next owner cannot inherit stale state. */
	recovery = zed_recover_locked(d);
	if (recovery)
		dev_err(d->dev, "DMA recovery failed after batch error %d: %d\n", ret, recovery);
failed:
	WRITE_ONCE(d->result_valid, false);
	return ret;
}

static void zed_status(struct zed_detector *d, struct zed_detector_status *s)
{
	s->core_id = det_read(d, DET_CORE_ID);
	s->core_version = det_read(d, DET_CORE_VERSION);
	s->build_id = det_read(d, DET_BUILD_ID);
	s->detector_status = det_read(d, DET_STATUS);
	s->error_code = det_read(d, DET_ERROR_CODE);
	s->mm2s_dmasr = dma_read(d, DMA_MM2S_DMASR);
	s->s2mm_dmasr = dma_read(d, DMA_S2MM_DMASR);
	s->last_error = READ_ONCE(d->last_error);
}

static int zed_open(struct inode *inode, struct file *file)
{
	struct miscdevice *misc = file->private_data;
	struct zed_detector *d = container_of(misc, struct zed_detector, misc);

	if (READ_ONCE(d->removing))
		return -ENODEV;
	if (atomic_cmpxchg(&d->opened, 0, 1))
		return -EBUSY;
	if (!get_device(d->dev)) {
		atomic_set(&d->opened, 0);
		return -ENODEV;
	}
	file->private_data = d;
	return 0;
}

static int zed_release(struct inode *inode, struct file *file)
{
	struct zed_detector *d = file->private_data;
	/* A close from another userspace thread cannot expose a live owner. */
	wait_event(d->state_waitq, !atomic_read(&d->active));
	atomic_set(&d->opened, 0);
	wake_up_all(&d->state_waitq);
	put_device(d->dev);
	return 0;
}

static int zed_cancel_active(struct zed_detector *d)
{
	if (!atomic_read(&d->active))
		return 0;
	atomic_set(&d->cancel_requested, 1);
	wake_up_interruptible(&d->event_waitq);
	if (!wait_event_timeout(d->state_waitq, !atomic_read(&d->active), msecs_to_jiffies(1200)))
		return -ETIMEDOUT;
	return READ_ONCE(d->poisoned) ? -EIO : 0;
}

/* The owner lock serializes starting state with itself.  Never wait here: a
 * SUBMIT which cannot own the hardware immediately keeps the legacy -EBUSY
 * contract and its total deadline starts only after this succeeds.  Clear
 * cancellation before publishing active so ABORT cannot set a request which a
 * later start then erases.  ABORT intentionally does not take hw_lock. */
static int zed_begin_active_locked(struct zed_detector *d)
{
	if (!mutex_trylock(&d->hw_lock))
		return -EBUSY;
	/* The initial ioctl checks are only fast paths.  A preceding transaction
	 * can poison the driver or removal can begin while this caller waits for
	 * userspace/copy work, so the owner-lock check is authoritative. */
	if (READ_ONCE(d->removing)) {
		mutex_unlock(&d->hw_lock);
		return -ENODEV;
	}
	if (READ_ONCE(d->poisoned)) {
		mutex_unlock(&d->hw_lock);
		return -EIO;
	}
	if (atomic_read(&d->active)) {
		mutex_unlock(&d->hw_lock);
		return -EBUSY;
	}
	atomic_set(&d->cancel_requested, 0);
	atomic_set(&d->active, 1);
	return 0;
}

static void zed_end_active_locked(struct zed_detector *d)
{
	atomic_set(&d->active, 0);
	wake_up_all(&d->state_waitq);
	mutex_unlock(&d->hw_lock);
}

static long zed_ioctl(struct file *file, unsigned int command, unsigned long arg)
{
	struct zed_detector *d = file->private_data;
	struct zed_detector_info info;
	struct zed_detector_submit submit;
	struct zed_detector_result result;
	struct zed_detector_status status;
	struct zed_detector_batch *batch;
	int ret;

	if (_IOC_TYPE(command) != ZED_DETECTOR_IOC_MAGIC || READ_ONCE(d->removing))
		return -ENODEV;
	switch (command) {
	case ZED_DETECTOR_IOC_GET_INFO:
		info.abi_version = ZED_DETECTOR_ABI_VERSION;
		info.input_bytes = ZED_DETECTOR_INPUT_BYTES;
		info.output_bytes = ZED_DETECTOR_OUTPUT_BYTES;
		info.build_id = det_read(d, DET_BUILD_ID);
		info.flags = (d->irq_ready ? ZED_DETECTOR_INFO_IRQ_AVAILABLE :
			      ZED_DETECTOR_INFO_POLL_FALLBACK) |
			ZED_DETECTOR_INFO_BATCH_SUBMIT;
		return copy_to_user((void __user *)arg, &info, sizeof(info)) ? -EFAULT : 0;
	case ZED_DETECTOR_IOC_SUBMIT:
		if (copy_from_user(&submit, (void __user *)arg, sizeof(submit)))
			return -EFAULT;
		ret = zed_validate_submit(&submit);
		if (ret)
			return ret;
		ret = zed_begin_active_locked(d);
		if (ret)
			return ret;
		ret = READ_ONCE(d->removing) ? -ENODEV : zed_submit_locked(d, &submit,
			jiffies + max_t(unsigned long, 1, msecs_to_jiffies(submit.timeout_ms)));
		d->last_error = ret;
		zed_end_active_locked(d);
		return ret;
	case ZED_DETECTOR_IOC_SUBMIT_BATCH:
		batch = kzalloc(sizeof(*batch), GFP_KERNEL);
		if (!batch)
			return -ENOMEM;
		if (copy_from_user(batch, (void __user *)arg, sizeof(*batch))) {
			ret = -EFAULT;
			goto batch_free;
		}
		ret = zed_validate_batch(batch);
		if (ret)
			goto batch_free;
		ret = zed_begin_active_locked(d);
		if (ret)
			goto batch_free;
		ret = READ_ONCE(d->removing) ? -ENODEV : zed_submit_batch_locked(d, batch);
		d->last_error = ret;
		zed_end_active_locked(d);
		if (ret)
			goto batch_free;
		if (copy_to_user((void __user *)arg, batch, sizeof(*batch)))
			ret = -EFAULT;
batch_free:
		kfree(batch);
		return ret;
	case ZED_DETECTOR_IOC_ABORT:
		ret = zed_cancel_active(d);
		WRITE_ONCE(d->last_error, ret);
		if (ret)
			return ret;
		if (atomic_read(&d->active))
			return -EBUSY;
		return 0;
	case ZED_DETECTOR_IOC_RESET:
		ret = zed_cancel_active(d);
		if (ret && ret != -EIO)
			return ret;
		/* zed_cancel_active() can release the old owner before RESET obtains
		 * hw_lock.  Do not wait for a newly-started owner in that gap: it was
		 * not the transaction this RESET cancelled and might run for 60 s. */
		if (!mutex_trylock(&d->hw_lock)) {
			WRITE_ONCE(d->last_error, -EBUSY);
			return -EBUSY; /* userspace may explicitly retry; never auto-retry */
		}
		det_write(d, DET_CONTROL, DET_CTRL_SOFT_RESET_IRQ);
		ret = zed_reset_dma_locked(d, jiffies + msecs_to_jiffies(1000));
		if (!ret) {
			det_write(d, DET_STATUS, DET_STATUS_STICKY);
			/* A reset is proof of recovery only if the expected core remains
			 * present and it reports IDLE after both DMA reset bits cleared. */
			if (!zed_detector_identity_ok(d) ||
			    !(det_read(d, DET_STATUS) & 0x01))
				ret = -EIO;
			else {
				WRITE_ONCE(d->result_valid, false);
				zed_clear_poisoned_locked(d);
			}
		} else {
			zed_mark_poisoned_locked(d);
		}
		if (ret)
			zed_mark_poisoned_locked(d);
		WRITE_ONCE(d->last_error, ret);
		mutex_unlock(&d->hw_lock);
		return ret;
	case ZED_DETECTOR_IOC_GET_RESULT:
		/* One FD may be shared by threads; never copy while another thread
		 * has started a new transfer on the private coherent page. */
		mutex_lock(&d->hw_lock);
		if (atomic_read(&d->active))
			ret = -EBUSY;
		else if (!READ_ONCE(d->result_valid))
			ret = -ENODATA;
		else {
			memcpy(result.output, d->output_cpu, ZED_DETECTOR_OUTPUT_BYTES);
			ret = 0;
		}
		mutex_unlock(&d->hw_lock);
		if (ret)
			return ret;
		return copy_to_user((void __user *)arg, &result, sizeof(result)) ? -EFAULT : 0;
	case ZED_DETECTOR_IOC_GET_STATUS:
		zed_status(d, &status);
		return copy_to_user((void __user *)arg, &status, sizeof(status)) ? -EFAULT : 0;
	default:
		return -ENOTTY;
	}
}

static const struct file_operations zed_fops = {
	.owner = THIS_MODULE,
	.open = zed_open,
	.release = zed_release,
	.unlocked_ioctl = zed_ioctl,
	.compat_ioctl = zed_ioctl,
	/* Linux 6.12 removed no_llseek; NULL preserves the non-seekable ABI. */
	.llseek = NULL,
};

static int zed_request_irqs(struct platform_device *pdev, struct zed_detector *d)
{
	static const char * const names[] = { "detector", "mm2s", "s2mm" };
	int i, ret;
	for (i = 0; i < ARRAY_SIZE(names); i++) {
		d->irq[i] = platform_get_irq_byname(pdev, names[i]);
		if (d->irq[i] == -EPROBE_DEFER)
			return -EPROBE_DEFER;
		if (d->irq[i] < 0)
			goto polling;
		ret = devm_request_irq(&pdev->dev, d->irq[i], zed_detector_irq, 0, DRIVER_NAME, d);
		if (ret)
			goto polling;
	}
	d->irq_ready = true;
	return 0;
polling:
	d->irq_ready = false;
	dev_warn(&pdev->dev, "Detector IRQ unavailable; using bounded status polling\n");
	return 0;
}

static int zed_probe(struct platform_device *pdev)
{
	struct zed_detector *d;
	struct resource *r;
	int ret;
	d = devm_kzalloc(&pdev->dev, sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	d->dev = &pdev->dev;
	mutex_init(&d->hw_lock);
	spin_lock_init(&d->event_lock);
	init_waitqueue_head(&d->event_waitq);
	init_waitqueue_head(&d->state_waitq);
	atomic_set(&d->opened, 0);
	atomic_set(&d->active, 0);
	atomic_set(&d->cancel_requested, 0);
	atomic_set(&d->event_seq, 0);
	r = platform_get_resource_byname(pdev, IORESOURCE_MEM, "detector");
	if (!r)
		return -EINVAL;
	d->det = devm_ioremap_resource(&pdev->dev, r);
	if (IS_ERR(d->det))
		return PTR_ERR(d->det);
	r = platform_get_resource_byname(pdev, IORESOURCE_MEM, "dma");
	if (!r)
		return -EINVAL;
	d->dma = devm_ioremap_resource(&pdev->dev, r);
	if (IS_ERR(d->dma))
		return PTR_ERR(d->dma);
	/* Do not bind a misc device to an arbitrary AXI address.  A wrong bitstream
	 * or a stale DT node must fail before it can program DMA registers. */
	if (!zed_detector_identity_ok(d)) {
		dev_err(&pdev->dev, "Detector identity mismatch: id=0x%08x version=0x%08x\n",
			det_read(d, DET_CORE_ID), det_read(d, DET_CORE_VERSION));
		return -ENODEV;
	}
	ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(32));
	if (ret)
		return ret;
	/* A readable core ID is not enough: require both simple-DMA reset bits to
	 * self-clear before allocating/exporting any userspace endpoint. */
	ret = zed_reset_dma_locked(d, jiffies + msecs_to_jiffies(1000));
	if (ret) {
		dev_err(&pdev->dev, "Detector DMA reset did not complete during probe\n");
		return ret;
	}
	d->input_cpu = dma_alloc_coherent(&pdev->dev, PAGE_SIZE, &d->input_dma, GFP_KERNEL);
	d->output_cpu = dma_alloc_coherent(&pdev->dev, PAGE_SIZE, &d->output_dma, GFP_KERNEL);
	if (!d->input_cpu || !d->output_cpu) {
		ret = -ENOMEM;
		goto free_buffers;
	}
	ret = zed_request_irqs(pdev, d);
	if (ret)
		goto free_buffers;
	d->misc.minor = MISC_DYNAMIC_MINOR;
	d->misc.name = DEVICE_NAME;
	d->misc.fops = &zed_fops;
	d->misc.parent = &pdev->dev;
	ret = misc_register(&d->misc);
	if (ret)
		goto free_buffers;
	platform_set_drvdata(pdev, d);
	dev_info(&pdev->dev, "Detector owner ready: core=0x%08x build=0x%08x IRQ=%s\n",
		det_read(d, DET_CORE_ID), det_read(d, DET_BUILD_ID), d->irq_ready ? "on" : "poll");
	return 0;
free_buffers:
	if (d->output_cpu)
		dma_free_coherent(&pdev->dev, PAGE_SIZE, d->output_cpu, d->output_dma);
	if (d->input_cpu)
		dma_free_coherent(&pdev->dev, PAGE_SIZE, d->input_cpu, d->input_dma);
	return ret;
}

static void zed_remove(struct platform_device *pdev)
{
	struct zed_detector *d = platform_get_drvdata(pdev);
	int ret;

	WRITE_ONCE(d->removing, true);
	misc_deregister(&d->misc);
	atomic_set(&d->cancel_requested, 1);
	wake_up_interruptible(&d->event_waitq);
	/* No mmap ABI exists.  Wait for active ioctl and exclusive fd to leave. */
	wait_event(d->state_waitq, !atomic_read(&d->active));
	wait_event(d->state_waitq, !atomic_read(&d->opened));
	/* platform remove cannot reliably veto forced unbind.  It therefore owns a
	 * last, independent-deadline halt attempt even after an otherwise normal
	 * transfer (RunStop may still be asserted).  If this cannot prove the AXI
	 * master stopped, coherent pages are intentionally quarantined rather than
	 * freed underneath a possible DMA write. */
	mutex_lock(&d->hw_lock);
	det_write(d, DET_CONTROL, DET_CTRL_ABORT_IRQ);
	ret = zed_reset_dma_locked(d, jiffies + msecs_to_jiffies(1000));
	if (!ret && (!zed_detector_identity_ok(d) ||
		     !(det_read(d, DET_STATUS) & 0x01)))
		ret = -EIO;
	if (ret) {
		zed_mark_poisoned_locked(d);
		dev_crit(&pdev->dev,
			"DMA halt failed during remove (%d); coherent pages quarantined; reboot before rebind\n",
			ret);
		mutex_unlock(&d->hw_lock);
		return;
	}
	zed_clear_poisoned_locked(d);
	mutex_unlock(&d->hw_lock);
	dma_free_coherent(&pdev->dev, PAGE_SIZE, d->output_cpu, d->output_dma);
	dma_free_coherent(&pdev->dev, PAGE_SIZE, d->input_cpu, d->input_dma);
}

static const struct of_device_id zed_of_match[] = {
	{ .compatible = "seu,mimo-detector-1.0" }, { }
};
MODULE_DEVICE_TABLE(of, zed_of_match);
static struct platform_driver zed_driver = {
	.probe = zed_probe,
	.remove = zed_remove,
	.driver = {
		.name = DRIVER_NAME,
		.of_match_table = zed_of_match,
		.suppress_bind_attrs = true,
	},
};
module_platform_driver(zed_driver);
MODULE_DESCRIPTION("ZedBoard private Detector AXI DMA owner");
MODULE_LICENSE("GPL");
