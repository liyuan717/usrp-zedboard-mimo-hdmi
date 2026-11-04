// SPDX-License-Identifier: GPL-2.0
/*
 * Private owner for the experimental IMBC batch-envelope core.
 *
 * This is deliberately not compatible with zedboard_detector.  It owns a
 * separate DT node, miscdevice, ioctl magic and coherent DMA allocation.  A
 * failed envelope never copies a prefix back to userspace: both AXI-DMA
 * channels are reset before the next owner may submit work.
 */
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
#include <linux/uaccess.h>
#include <linux/wait.h>

#include "uapi/zedboard_interleaved_candidate.h"

#define DRIVER_NAME "zedboard-interleaved-candidate"
#define DEVICE_NAME "zed-mimo-interleaved-candidate"

/* Candidate AXI-Lite map, matching mimo_mhgd_interleaved_candidate_axil.sv. */
#define CORE_ID                 0x00
#define CORE_VERSION            0x04
#define CORE_CAPABILITIES       0x08
#define CORE_MAX_TASKS          0x0c
#define CORE_CONTROL            0x10
#define CORE_BATCH_COUNT        0x14
#define CORE_STATUS             0x18
#define CORE_ERROR              0x1c
#define CORE_BUILD_ID           0x3c
#define CORE_EXPECTED_ID        0x494d4243 /* IMBC */
#define CORE_EXPECTED_VERSION   0x00010000
#define CORE_CAP_EXPECTED       0x00000007
#define CORE_CONTROL_START      0x00000001
#define CORE_CONTROL_CANCEL     0x00000002
#define CORE_STATUS_BUSY        0x00000001
#define CORE_STATUS_DONE        0x00000002
#define CORE_STATUS_ERROR       0x00000004

#define DMA_MM2S_DMACR 0x00
#define DMA_MM2S_DMASR 0x04
#define DMA_MM2S_SA    0x18
#define DMA_MM2S_LENGTH 0x28
#define DMA_S2MM_DMACR 0x30
#define DMA_S2MM_DMASR 0x34
#define DMA_S2MM_DA    0x48
#define DMA_S2MM_LENGTH 0x58
#define DMA_DMACR_RUNSTOP 0x00000001
#define DMA_DMACR_RESET   0x00000004
#define DMA_DMACR_IRQ_EN  0x00007000
#define DMA_DMASR_ERR     0x00000070
#define DMA_DMASR_IOC     0x00001000
#define DMA_DMASR_IRQ     0x00007000

#define INPUT_BYTES  ZED_INTERLEAVED_CANDIDATE_INPUT_BYTES
#define OUTPUT_BYTES ZED_INTERLEAVED_CANDIDATE_OUTPUT_BYTES
#define MAX_TASKS    ZED_INTERLEAVED_CANDIDATE_MAX_TASKS
#define INPUT_ALLOC_BYTES (INPUT_BYTES * MAX_TASKS)
#define OUTPUT_ALLOC_BYTES (OUTPUT_BYTES * MAX_TASKS)

struct zed_interleaved {
	struct device *dev;
	void __iomem *core;
	void __iomem *dma;
	int irq[2];
	bool irq_ready;
	struct miscdevice misc;
	struct mutex owner_lock;
	spinlock_t event_lock;
	wait_queue_head_t event_waitq;
	wait_queue_head_t state_waitq;
	atomic_t opened, active, cancel_requested, event_seq;
	bool removing, poisoned, poison_module_ref;
	/* Incremented only after old DMA IRQs are acknowledged and drained.  This
	 * is an internal epoch, not a userspace ABI field. */
	u32 event_epoch;
	u32 mm2s_events, s2mm_events;
	void *input_cpu, *output_cpu;
	dma_addr_t input_dma, output_dma;
	int last_error;
	u32 recovery_state; /* 0 clean; 1 recovered after an error; 2 unknown */
};

static inline u32 core_read(struct zed_interleaved *d, u32 o)
{ return readl(d->core + o); }
static inline void core_write(struct zed_interleaved *d, u32 o, u32 v)
{ writel(v, d->core + o); }
static inline u32 dma_read(struct zed_interleaved *d, u32 o)
{ return readl(d->dma + o); }
static inline void dma_write(struct zed_interleaved *d, u32 o, u32 v)
{ writel(v, d->dma + o); }

static bool core_identity_ok(struct zed_interleaved *d)
{
	return core_read(d, CORE_ID) == CORE_EXPECTED_ID &&
		core_read(d, CORE_VERSION) == CORE_EXPECTED_VERSION &&
		(core_read(d, CORE_CAPABILITIES) & CORE_CAP_EXPECTED) == CORE_CAP_EXPECTED &&
		core_read(d, CORE_MAX_TASKS) == MAX_TASKS;
}

static irqreturn_t candidate_irq(int irq, void *opaque)
{
	struct zed_interleaved *d = opaque;
	u32 pending = 0;
	unsigned long flags;
	u32 status_reg;
	u32 *events;

	if (irq == d->irq[0]) {
		status_reg = DMA_MM2S_DMASR;
		events = &d->mm2s_events;
	} else if (irq == d->irq[1]) {
		status_reg = DMA_S2MM_DMASR;
		events = &d->s2mm_events;
	} else {
		return IRQ_NONE;
	}

	/* Keep the status sampling, W1C ACK, and event publication in the same
	 * event_lock critical section as the later kick.  Thus a handler that
	 * sampled an old envelope cannot publish it after a new event epoch, nor
	 * can its delayed ACK clear a new envelope's IOC bit.  MMIO read/write is
	 * non-sleeping and valid in this hard-IRQ spinlocked region. */
	spin_lock_irqsave(&d->event_lock, flags);
	pending = dma_read(d, status_reg) & DMA_DMASR_IRQ;
	if (pending) {
		dma_write(d, status_reg, pending);
		*events |= pending;
	}
	spin_unlock_irqrestore(&d->event_lock, flags);
	if (!pending)
		return IRQ_NONE;
	atomic_inc(&d->event_seq);
	wake_up_interruptible(&d->event_waitq);
	return IRQ_HANDLED;
}

static void acknowledge_dma_irqs(struct zed_interleaved *d)
{
	dma_write(d, DMA_MM2S_DMASR, DMA_DMASR_IRQ);
	dma_write(d, DMA_S2MM_DMASR, DMA_DMASR_IRQ);
}

static void synchronize_dma_irqs(struct zed_interleaved *d)
{
	/* synchronize_irq() may wait, so it must never run under event_lock. */
	if (!d->irq_ready)
		return;
	synchronize_irq(d->irq[0]);
	synchronize_irq(d->irq[1]);
}

/* Start a new envelope event epoch only after acknowledge_dma_irqs() and
 * synchronize_dma_irqs().  No old handler can then publish stale IOC/ERR
 * bits into this epoch. */
static void begin_event_epoch(struct zed_interleaved *d)
{
	unsigned long flags;
	spin_lock_irqsave(&d->event_lock, flags);
	d->mm2s_events = 0;
	d->s2mm_events = 0;
	d->event_epoch++;
	spin_unlock_irqrestore(&d->event_lock, flags);
}

static void drain_dma_irq_epoch(struct zed_interleaved *d)
{
	/* Required ordering: W1C stale device status, drain in-flight handlers,
	 * then clear software latches.  The following kick takes event_lock, so an
	 * IRQ belonging to the next envelope starts only after this boundary. */
	acknowledge_dma_irqs(d);
	synchronize_dma_irqs(d);
	begin_event_epoch(d);
}

/*
 * Cancellation is an owner-scoped sticky latch.  event_lock is also held by
 * the final DMA/core kick preflight, so an ABORT that acquires this lock first
 * cannot be followed by a new envelope start.  Do not wait, copy userspace
 * memory, drain DMA IRQs, or perform DMA recovery while holding it.
 */
static void latch_cancel(struct zed_interleaved *d)
{
	unsigned long flags;

	spin_lock_irqsave(&d->event_lock, flags);
	atomic_set(&d->cancel_requested, 1);
	spin_unlock_irqrestore(&d->event_lock, flags);
}

static void clear_cancel(struct zed_interleaved *d)
{
	unsigned long flags;

	spin_lock_irqsave(&d->event_lock, flags);
	atomic_set(&d->cancel_requested, 0);
	spin_unlock_irqrestore(&d->event_lock, flags);
}

static void read_events(struct zed_interleaved *d, u32 *mm2s, u32 *s2mm)
{
	unsigned long flags;
	spin_lock_irqsave(&d->event_lock, flags);
	*mm2s = d->mm2s_events;
	*s2mm = d->s2mm_events;
	spin_unlock_irqrestore(&d->event_lock, flags);
}

static void mark_poisoned(struct zed_interleaved *d)
{
	if (!d->poisoned) {
		d->poisoned = true;
		d->recovery_state = 2;
		d->poison_module_ref = try_module_get(THIS_MODULE);
	}
}

static void clear_poisoned(struct zed_interleaved *d)
{
	if (d->poisoned && d->poison_module_ref) {
		d->poison_module_ref = false;
		module_put(THIS_MODULE);
	}
	d->poisoned = false;
	d->recovery_state = 0;
}

static int reset_dma_locked(struct zed_interleaved *d, unsigned long deadline)
{
	dma_write(d, DMA_MM2S_DMACR, DMA_DMACR_RESET);
	dma_write(d, DMA_S2MM_DMACR, DMA_DMACR_RESET);
	do {
		if (!(dma_read(d, DMA_MM2S_DMACR) & DMA_DMACR_RESET) &&
		    !(dma_read(d, DMA_S2MM_DMACR) & DMA_DMACR_RESET)) {
			drain_dma_irq_epoch(d);
			return 0;
		}
		usleep_range(100, 250);
	} while (time_before(jiffies, deadline));
	return -ETIMEDOUT;
}

/* Must be called for every failed or cancelled envelope while owner_lock held. */
static int recover_locked(struct zed_interleaved *d)
{
	int ret;
	core_write(d, CORE_CONTROL, CORE_CONTROL_CANCEL);
	ret = reset_dma_locked(d, jiffies + msecs_to_jiffies(1000));
	if (ret || !core_identity_ok(d) || (core_read(d, CORE_STATUS) & CORE_STATUS_BUSY)) {
		mark_poisoned(d);
		return ret ? ret : -EIO;
	}
	d->recovery_state = 1;
	return 0;
}

static int wait_complete_locked(struct zed_interleaved *d, unsigned long deadline)
{
	int seq = atomic_read(&d->event_seq);
	long remain, waitret;
	for (;;) {
		u32 mm2s_event, s2mm_event;
		u32 mm2s, s2mm;
		u32 status = core_read(d, CORE_STATUS);
		read_events(d, &mm2s_event, &s2mm_event);
		mm2s = dma_read(d, DMA_MM2S_DMASR) | mm2s_event;
		s2mm = dma_read(d, DMA_S2MM_DMASR) | s2mm_event;
		if (atomic_read(&d->cancel_requested))
			return -ECANCELED;
		if ((mm2s | s2mm) & DMA_DMASR_ERR || status & CORE_STATUS_ERROR)
			return -EIO;
		if ((mm2s & DMA_DMASR_IOC) && (s2mm & DMA_DMASR_IOC) &&
		    (status & CORE_STATUS_DONE) && !(status & CORE_STATUS_BUSY))
			return 0;
		if (!time_before(jiffies, deadline))
			return -ETIMEDOUT;
		remain = deadline - jiffies;
		if (!d->irq_ready) {
			usleep_range(200, 500);
			continue;
		}
		waitret = wait_event_interruptible_timeout(d->event_waitq,
			atomic_read(&d->cancel_requested) || atomic_read(&d->event_seq) != seq,
			min_t(long, remain, max_t(long, 1, msecs_to_jiffies(10))));
		if (waitret < 0)
			return (int)waitret;
		seq = atomic_read(&d->event_seq);
	}
}

static int validate_batch(const struct zed_interleaved_candidate_batch *b)
{
	if (!b->count || b->count > MAX_TASKS || !b->timeout_ms ||
	    b->timeout_ms > ZED_INTERLEAVED_CANDIDATE_TIMEOUT_MAX_MS || b->flags || b->reserved ||
	    !b->input_user_ptr || !b->output_user_ptr)
		return -EINVAL;
	return 0;
}

static int submit_locked(struct zed_interleaved *d, const struct zed_interleaved_candidate_batch *b)
{
	unsigned long deadline = jiffies + max_t(unsigned long, 1, msecs_to_jiffies(b->timeout_ms));
	unsigned long flags;
	u32 in_bytes = b->count * INPUT_BYTES, out_bytes = b->count * OUTPUT_BYTES;
	int ret, recovery;
	bool kicked = false;

	if (upper_32_bits(d->input_dma) || upper_32_bits(d->output_dma) ||
	    (d->input_dma & 7) || (d->output_dma & 7))
		return -ERANGE;
	if (copy_from_user(d->input_cpu, u64_to_user_ptr(b->input_user_ptr), in_bytes))
		return -EFAULT;
	memset(d->output_cpu, 0, out_bytes);
	/* Both allocations are coherent, but retain the ordering barrier so this
	 * code remains correct on DMA-coherent architectures with posted writes. */
	dma_wmb();
	if (!time_before(jiffies, deadline))
		return -ETIMEDOUT;
	drain_dma_irq_epoch(d);

	/*
	 * ABORT and this preflight serialize on event_lock.  Holding the lock over
	 * all three kick domains is intentional: if ABORT acquired it first, no
	 * S2MM/core/MM2S start write can occur; if this acquired it first, ABORT
	 * cancels an already-started envelope and wait_complete_locked() recovers it.
	 * The MMIO writes do not sleep.  drain_dma_irq_epoch() is deliberately
	 * above this region because synchronize_irq() may sleep and its final
	 * event-latch clear takes event_lock itself.
	 */
	spin_lock_irqsave(&d->event_lock, flags);
	if (atomic_read(&d->cancel_requested)) {
		ret = -ECANCELED;
	} else if (!time_before(jiffies, deadline)) {
		ret = -ETIMEDOUT;
	} else {
		/* One S2MM, core start, then one MM2S envelope. TLAST is supplied
		 * only by the candidate AXIS wrapper at each envelope tail. */
		dma_write(d, DMA_S2MM_DMACR, DMA_DMACR_RUNSTOP |
			  (d->irq_ready ? DMA_DMACR_IRQ_EN : 0));
		dma_write(d, DMA_S2MM_DA, lower_32_bits(d->output_dma));
		dma_write(d, DMA_S2MM_LENGTH, out_bytes);
		core_write(d, CORE_BATCH_COUNT, b->count);
		core_write(d, CORE_CONTROL, CORE_CONTROL_START);
		dma_write(d, DMA_MM2S_DMACR, DMA_DMACR_RUNSTOP |
			  (d->irq_ready ? DMA_DMACR_IRQ_EN : 0));
		dma_write(d, DMA_MM2S_SA, lower_32_bits(d->input_dma));
		dma_write(d, DMA_MM2S_LENGTH, in_bytes);
		kicked = true;
		ret = 0;
	}
	spin_unlock_irqrestore(&d->event_lock, flags);
	if (ret)
		return ret;
	ret = wait_complete_locked(d, deadline);
	if (ret) {
		if (kicked) {
			recovery = recover_locked(d);
			if (recovery)
				dev_err(d->dev, "candidate recovery failed after %d: %d\n", ret, recovery);
		}
		return ret;
	}
	dma_rmb();
	return 0;
}

static int begin_active(struct zed_interleaved *d)
{
	if (!mutex_trylock(&d->owner_lock))
		return -EBUSY;
	if (d->removing) {
		mutex_unlock(&d->owner_lock);
		return -ENODEV;
	}
	if (d->poisoned) {
		mutex_unlock(&d->owner_lock);
		return -EIO;
	}
	atomic_set(&d->active, 1);
	return 0;
}

static void end_active(struct zed_interleaved *d)
{
	atomic_set(&d->active, 0);
	wake_up_all(&d->state_waitq);
	mutex_unlock(&d->owner_lock);
}

static void candidate_status(struct zed_interleaved *d, struct zed_interleaved_candidate_status *s)
{
	s->core_id = core_read(d, CORE_ID);
	s->core_version = core_read(d, CORE_VERSION);
	s->build_id = core_read(d, CORE_BUILD_ID);
	s->candidate_status = core_read(d, CORE_STATUS);
	s->candidate_error = core_read(d, CORE_ERROR);
	s->mm2s_dmasr = dma_read(d, DMA_MM2S_DMASR);
	s->s2mm_dmasr = dma_read(d, DMA_S2MM_DMASR);
	s->last_error = READ_ONCE(d->last_error);
	s->recovery_state = READ_ONCE(d->recovery_state);
}

/* A new owner may forget a historical operation error only when the actual
 * fabric endpoints are presently idle and error-free.  Do not mutate
 * recovery_state here: a value of 1 remains useful evidence that this
 * instance had to recover before the new owner opened it. */
static bool candidate_healthy_idle(struct zed_interleaved *d)
{
	u32 status;
	u32 dma_status;

	if (READ_ONCE(d->poisoned) || atomic_read(&d->active))
		return false;
	if (!core_identity_ok(d))
		return false;
	status = core_read(d, CORE_STATUS);
	dma_status = dma_read(d, DMA_MM2S_DMASR) | dma_read(d, DMA_S2MM_DMASR);
	return !(status & CORE_STATUS_BUSY) && !(dma_status & DMA_DMASR_ERR);
}

static int candidate_open(struct inode *inode, struct file *file)
{
	struct miscdevice *misc = file->private_data;
	struct zed_interleaved *d = container_of(misc, struct zed_interleaved, misc);
	if (READ_ONCE(d->removing))
		return -ENODEV;
	if (atomic_cmpxchg(&d->opened, 0, 1))
		return -EBUSY;
	if (!get_device(d->dev)) {
		atomic_set(&d->opened, 0);
		return -ENODEV;
	}
	/* opened transitioned from zero only after the prior release observed
	 * !active, so this is the only implicit owner-boundary cancel clear. */
	clear_cancel(d);
	if (candidate_healthy_idle(d))
		WRITE_ONCE(d->last_error, 0);
	file->private_data = d;
	return 0;
}

static int candidate_release(struct inode *inode, struct file *file)
{
	struct zed_interleaved *d = file->private_data;
	wait_event(d->state_waitq, !atomic_read(&d->active));
	atomic_set(&d->opened, 0);
	wake_up_all(&d->state_waitq);
	put_device(d->dev);
	return 0;
}

static int cancel_active(struct zed_interleaved *d)
{
	/* Latch even while idle: a later SUBMIT by this open owner must observe
	 * -ECANCELED at its event_lock preflight rather than start hardware. */
	latch_cancel(d);
	wake_up_interruptible(&d->event_waitq);
	if (!atomic_read(&d->active))
		return 0;
	if (!wait_event_timeout(d->state_waitq, !atomic_read(&d->active), msecs_to_jiffies(1200)))
		return -ETIMEDOUT;
	return d->poisoned ? -EIO : 0;
}

static long candidate_ioctl(struct file *file, unsigned int command, unsigned long arg)
{
	struct zed_interleaved *d = file->private_data;
	struct zed_interleaved_candidate_info info;
	struct zed_interleaved_candidate_batch batch;
	struct zed_interleaved_candidate_status status;
	int ret;
	if (_IOC_TYPE(command) != ZED_INTERLEAVED_CANDIDATE_IOC_MAGIC || d->removing)
		return -ENODEV;
	switch (command) {
	case ZED_INTERLEAVED_CANDIDATE_IOC_GET_INFO:
		info.abi_version = ZED_INTERLEAVED_CANDIDATE_ABI_VERSION;
		info.input_bytes = INPUT_BYTES;
		info.output_bytes = OUTPUT_BYTES;
		info.max_tasks = MAX_TASKS;
		info.build_id = core_read(d, CORE_BUILD_ID);
		info.flags = ZED_INTERLEAVED_CANDIDATE_INFO_BATCH_ENVELOPE |
			ZED_INTERLEAVED_CANDIDATE_INFO_TAG_REORDER_REQUIRED |
			ZED_INTERLEAVED_CANDIDATE_INFO_POLL_FALLBACK |
			ZED_INTERLEAVED_CANDIDATE_INFO_STICKY_IDLE_ABORT;
		return copy_to_user((void __user *)arg, &info, sizeof(info)) ? -EFAULT : 0;
	case ZED_INTERLEAVED_CANDIDATE_IOC_SUBMIT_BATCH:
		if (copy_from_user(&batch, (void __user *)arg, sizeof(batch)))
			return -EFAULT;
		ret = validate_batch(&batch);
		if (ret)
			return ret;
		ret = begin_active(d);
		if (ret)
			return ret;
		ret = submit_locked(d, &batch);
		if (!ret && copy_to_user(u64_to_user_ptr(batch.output_user_ptr), d->output_cpu,
					  batch.count * OUTPUT_BYTES))
			ret = -EFAULT;
		WRITE_ONCE(d->last_error, ret);
		/* Keep the exclusive owner lock until after the full successful copy;
		 * otherwise the next envelope could overwrite output_cpu mid-copy. */
		end_active(d);
		return ret;
	case ZED_INTERLEAVED_CANDIDATE_IOC_ABORT:
		ret = cancel_active(d);
		WRITE_ONCE(d->last_error, ret);
		return ret;
	case ZED_INTERLEAVED_CANDIDATE_IOC_RESET:
		ret = cancel_active(d);
		if (ret && ret != -EIO)
			return ret;
		if (!mutex_trylock(&d->owner_lock))
			return -EBUSY;
		core_write(d, CORE_CONTROL, CORE_CONTROL_CANCEL);
		ret = reset_dma_locked(d, jiffies + msecs_to_jiffies(1000));
		if (!ret && core_identity_ok(d) && !(core_read(d, CORE_STATUS) & CORE_STATUS_BUSY)) {
			clear_poisoned(d);
			/* RESET is an explicit, successful owner-scoped cancel clear.  An
			 * ABORT racing it serializes through event_lock and may re-latch it. */
			clear_cancel(d);
		} else {
			if (!ret)
				ret = -EIO;
			mark_poisoned(d);
		}
		WRITE_ONCE(d->last_error, ret);
		mutex_unlock(&d->owner_lock);
		return ret;
	case ZED_INTERLEAVED_CANDIDATE_IOC_GET_STATUS:
		candidate_status(d, &status);
		return copy_to_user((void __user *)arg, &status, sizeof(status)) ? -EFAULT : 0;
	default:
		return -ENOTTY;
	}
}

static const struct file_operations candidate_fops = {
	.owner = THIS_MODULE, .open = candidate_open, .release = candidate_release,
	.unlocked_ioctl = candidate_ioctl, .compat_ioctl = candidate_ioctl, .llseek = NULL,
};

static int request_irqs(struct platform_device *pdev, struct zed_interleaved *d)
{
	int ret;
	d->irq[0] = platform_get_irq_byname_optional(pdev, "mm2s");
	d->irq[1] = platform_get_irq_byname_optional(pdev, "s2mm");
	if (d->irq[0] == -EPROBE_DEFER || d->irq[1] == -EPROBE_DEFER)
		return -EPROBE_DEFER;
	if (d->irq[0] == -ENXIO && d->irq[1] == -ENXIO)
		return 0; /* bounded polling is a declared ABI capability */
	/* A half-wired or otherwise malformed interrupt description must not be
	 * silently relabelled as polling: it would hide a real BD/DT mismatch. */
	if (d->irq[0] < 0)
		return d->irq[0];
	if (d->irq[1] < 0)
		return d->irq[1];
	ret = devm_request_irq(&pdev->dev, d->irq[0], candidate_irq, 0, DRIVER_NAME, d);
	if (ret)
		return ret;
	ret = devm_request_irq(&pdev->dev, d->irq[1], candidate_irq, 0, DRIVER_NAME, d);
	if (ret)
		return ret;
	d->irq_ready = true;
	return 0;
}

static int candidate_probe(struct platform_device *pdev)
{
	struct zed_interleaved *d;
	struct resource *r;
	int ret;
	d = devm_kzalloc(&pdev->dev, sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	d->dev = &pdev->dev;
	mutex_init(&d->owner_lock);
	spin_lock_init(&d->event_lock);
	init_waitqueue_head(&d->event_waitq);
	init_waitqueue_head(&d->state_waitq);
	atomic_set(&d->opened, 0); atomic_set(&d->active, 0);
	atomic_set(&d->cancel_requested, 0); atomic_set(&d->event_seq, 0);
	r = platform_get_resource_byname(pdev, IORESOURCE_MEM, "candidate-core");
	if (!r) return -EINVAL;
	d->core = devm_ioremap_resource(&pdev->dev, r);
	if (IS_ERR(d->core)) return PTR_ERR(d->core);
	r = platform_get_resource_byname(pdev, IORESOURCE_MEM, "candidate-dma");
	if (!r) return -EINVAL;
	d->dma = devm_ioremap_resource(&pdev->dev, r);
	if (IS_ERR(d->dma)) return PTR_ERR(d->dma);
	if (!core_identity_ok(d))
		return -ENODEV;
	ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(32));
	if (ret) return ret;
	ret = reset_dma_locked(d, jiffies + msecs_to_jiffies(1000));
	if (ret) return ret;
	d->input_cpu = dma_alloc_coherent(&pdev->dev, INPUT_ALLOC_BYTES, &d->input_dma, GFP_KERNEL);
	d->output_cpu = dma_alloc_coherent(&pdev->dev, OUTPUT_ALLOC_BYTES, &d->output_dma, GFP_KERNEL);
	if (!d->input_cpu || !d->output_cpu) { ret = -ENOMEM; goto free_dma; }
	ret = request_irqs(pdev, d);
	if (ret) goto free_dma;
	d->misc.minor = MISC_DYNAMIC_MINOR; d->misc.name = DEVICE_NAME;
	d->misc.fops = &candidate_fops; d->misc.parent = &pdev->dev;
	ret = misc_register(&d->misc);
	if (ret) goto free_dma;
	platform_set_drvdata(pdev, d);
	dev_info(&pdev->dev, "IMBC batch candidate owner ready, build=0x%08x\n", core_read(d, CORE_BUILD_ID));
	return 0;
free_dma:
	if (d->output_cpu) dma_free_coherent(&pdev->dev, OUTPUT_ALLOC_BYTES, d->output_cpu, d->output_dma);
	if (d->input_cpu) dma_free_coherent(&pdev->dev, INPUT_ALLOC_BYTES, d->input_cpu, d->input_dma);
	return ret;
}

static void candidate_remove(struct platform_device *pdev)
{
	struct zed_interleaved *d = platform_get_drvdata(pdev);
	int ret;
	d->removing = true; misc_deregister(&d->misc);
	latch_cancel(d); wake_up_interruptible(&d->event_waitq);
	wait_event(d->state_waitq, !atomic_read(&d->active));
	wait_event(d->state_waitq, !atomic_read(&d->opened));
	mutex_lock(&d->owner_lock);
	ret = recover_locked(d);
	if (ret) {
		dev_crit(&pdev->dev, "candidate DMA halt failed; coherent pages quarantined\n");
		mutex_unlock(&d->owner_lock);
		return;
	}
	mutex_unlock(&d->owner_lock);
	dma_free_coherent(&pdev->dev, OUTPUT_ALLOC_BYTES, d->output_cpu, d->output_dma);
	dma_free_coherent(&pdev->dev, INPUT_ALLOC_BYTES, d->input_cpu, d->input_dma);
}

static const struct of_device_id candidate_of_match[] = {
	{ .compatible = "seu,mimo-interleaved-candidate-1.0" }, { }
};
MODULE_DEVICE_TABLE(of, candidate_of_match);
static struct platform_driver candidate_driver = {
	.probe = candidate_probe, .remove = candidate_remove,
	.driver = { .name = DRIVER_NAME, .of_match_table = candidate_of_match, .suppress_bind_attrs = true },
};
module_platform_driver(candidate_driver);
MODULE_DESCRIPTION("ZedBoard experimental interleaved MHGD batch DMA owner");
MODULE_LICENSE("GPL");
