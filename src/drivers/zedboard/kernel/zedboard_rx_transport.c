// SPDX-License-Identifier: GPL-2.0
/* Dedicated RXTH AXI-DMAC consumer.  Does not own the normal IIO DMAC. */
#include <linux/dma-mapping.h>
#include <linux/dmaengine.h>
#include <linux/fs.h>
#include <linux/kref.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/sysfs.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include <linux/workqueue.h>
#include "uapi/zedboard_rx_transport.h"
/* 64 * 1024 scans covers 16.384 ms of scheduling jitter at 4 MSPS.
 * Bounded buffering is not a throughput guarantee; hardware RXTH continuity
 * and the exported halt counter remain the acceptance authority.
 */
#define N 64
#define DEV "zed-rxth"
enum st { FREE, INFLIGHT, READY, COPYING };
struct d; struct slot { struct d *d; void *cpu; dma_addr_t dma; u32 gen; u8 n, st; };
struct d { struct device *dev, *dma_dev; struct dma_chan *chan; struct miscdevice misc; struct kref ref;
 struct mutex lock, read_lock; spinlock_t qlock; wait_queue_head_t wait; struct work_struct refill;
 struct slot s[N]; u8 q[N],head,tail,nready; u32 gen,to; bool removing,streaming,halted,poisoned,stopping,dma_unsafe,dma_pinned,cancel_latched;
 atomic_t open; atomic64_t submitted,completed,aborted,timedout,delivery,halts; int err; };
static void destroy(struct kref *r)
{
	struct d *d = container_of(r, struct d, ref);
	int i;

	/* Unknown DMA ownership: retain callback state AND its device references.
	 * submit() pins this module until termination is confirmed. Retaining only
	 * the buffers would still allow callbacks into freed state or module text.
	 * This quarantined device requires recovery/reboot; it is not reusable.
	 */
	if (d->dma_unsafe)
		return;
	for (i = 0; i < N; i++)
		if (d->s[i].cpu)
			dma_free_coherent(d->dma_dev, ZED_RXTH_BLOCK_BYTES,
					  d->s[i].cpu, d->s[i].dma);
	if (d->chan)
		dma_release_channel(d->chan);
	if (d->dma_dev)
		put_device(d->dma_dev);
	put_device(d->dev);
	kfree(d);
}
static void done(void *p)
{
	struct slot *s = p;
	struct d *d = s->d;
	unsigned long flags;
	bool accepted = false;

	spin_lock_irqsave(&d->qlock, flags);
	if (!READ_ONCE(d->stopping) && s->gen == READ_ONCE(d->gen) &&
	    s->st == INFLIGHT) {
		s->st = READY;
		d->q[d->tail] = s->n;
		d->tail = (d->tail + 1) % N;
		d->nready++;
		atomic64_inc(&d->completed);
		accepted = true;
		if (d->nready == N) {
			WRITE_ONCE(d->halted, true);
			/* Remain logically streaming; read recycles a slot and resumes.
			 * In-band RXTH metadata, not this ring, proves continuity.
			 */
			atomic64_inc(&d->halts);
		}
	}
	spin_unlock_irqrestore(&d->qlock, flags);
	if (accepted) {
		wake_up_interruptible(&d->wait);
		schedule_work(&d->refill);
	}
}
static int submit(struct d *d, struct slot *s)
{
	struct dma_async_tx_descriptor *descriptor;
	dma_cookie_t cookie;

	descriptor = dmaengine_prep_slave_single(d->chan, s->dma,
		ZED_RXTH_BLOCK_BYTES, DMA_DEV_TO_MEM, DMA_PREP_INTERRUPT | DMA_CTRL_ACK);
	if (!descriptor)
		return -EIO;
	if (!d->dma_pinned) {
		__module_get(THIS_MODULE);
		d->dma_pinned = true;
	}
	s->gen = d->gen;
	s->st = INFLIGHT;
	descriptor->callback = done;
	descriptor->callback_param = s;
	cookie = dmaengine_submit(descriptor);
	if (dma_submit_error(cookie)) {
		s->st = FREE;
		return dma_submit_error(cookie);
	}
	atomic64_inc(&d->submitted);
	return 0;
}
static void stop(struct d *d, int error);

static int fill(struct d *d)
{
	int i, result = 0;

	/* All callers hold d->lock.  This is a second gate for workqueue refills:
	 * callback/work activity after ABORT must not submit a new descriptor. */
	if (d->cancel_latched || !d->streaming || READ_ONCE(d->halted) ||
	    d->poisoned || d->removing)
		return 0;
	for (i = 0; i < N; i++) {
		if (READ_ONCE(d->s[i].st) != FREE)
			continue;
		result = submit(d, &d->s[i]);
		if (result)
			break;
	}
	if (result) {
		d->poisoned = true;
		d->err = result;
	} else {
		dma_async_issue_pending(d->chan);
	}
	return result;
}

static void refill(struct work_struct *work)
{
	struct d *d = container_of(work, struct d, refill);
	int result;

	mutex_lock(&d->lock);
	result = fill(d);
	/* A later descriptor can fail after earlier ones were submitted. Resolve
	 * DMA ownership and wake readers now, not only at the next read timeout.
	 */
	if (result)
		stop(d, result);
	mutex_unlock(&d->lock);
}
static void stop(struct d *d, int error)
{
	unsigned long flags;
	int i, terminated;

	WRITE_ONCE(d->stopping, true);
	d->streaming = false;
	WRITE_ONCE(d->halted, false);
	d->gen++;
	terminated = dmaengine_terminate_sync(d->chan);
	if (terminated) {
		d->poisoned = true;
		d->dma_unsafe = true;
		d->err = terminated;
		/* Keep stopping asserted and the module pinned until recovery. */
	} else {
		d->dma_unsafe = false;
		d->err = error;
		if (d->dma_pinned) {
			d->dma_pinned = false;
			module_put(THIS_MODULE);
		}
	}
	spin_lock_irqsave(&d->qlock, flags);
	d->head = d->tail = d->nready = 0;
	for (i = 0; i < N; i++)
		if (!terminated && d->s[i].st != COPYING)
			d->s[i].st = FREE;
	spin_unlock_irqrestore(&d->qlock, flags);
	if (!terminated)
		WRITE_ONCE(d->stopping, false);
	wake_up_interruptible(&d->wait);
}
static ssize_t rxread(struct file *file, char __user *buffer, size_t count,
		      loff_t *offset)
{
	struct d *d = file->private_data;
	struct slot *slot = NULL;
	unsigned long flags;
	long waited;
	u32 generation, slot_generation = 0;
	int result;

	(void)offset;
	if (count < ZED_RXTH_BLOCK_BYTES)
		return -EMSGSIZE;
	if (mutex_lock_interruptible(&d->read_lock))
		return -ERESTARTSYS;
	mutex_lock(&d->lock);
	if (d->removing || d->poisoned) {
		result = d->removing ? -ENODEV : -EIO;
		goto out;
	}
	/* ABORT and this automatic-start decision are serialized by d->lock.
	 * If ABORT won, this open owner remains cancelled until close/reopen. */
	if (d->cancel_latched) {
		result = -ECANCELED;
		goto out;
	}
	if (!d->streaming && !d->nready) {
		d->gen++;
		d->streaming = true;
		result = fill(d);
		if (result) {
			stop(d, result);
			goto out;
		}
	}
	generation = d->gen;
	mutex_unlock(&d->lock);
	waited = wait_event_interruptible_timeout(d->wait,
		READ_ONCE(d->nready) || READ_ONCE(d->gen) != generation ||
		READ_ONCE(d->poisoned) || READ_ONCE(d->removing),
		msecs_to_jiffies(READ_ONCE(d->to)));
	mutex_lock(&d->lock);
	/* Resolve the reason BEFORE stop increments generation. Otherwise a
	 * timeout or signal was incorrectly returned as an external ABORT.
	 */
	if (d->removing)
		result = -ENODEV;
	else if (d->poisoned)
		result = -EIO;
	else if (d->gen != generation)
		result = -ECANCELED;
	else if (waited < 0)
		result = waited;
	else if (!waited)
		result = -ETIMEDOUT;
	else
		result = 0;
	if (result) {
		if (result == -ETIMEDOUT)
			atomic64_inc(&d->timedout);
		if (d->gen == generation)
			stop(d, result);
		goto out;
	}
	spin_lock_irqsave(&d->qlock, flags);
	if (d->nready) {
		slot = &d->s[d->q[d->head]];
		d->head = (d->head + 1) % N;
		d->nready--;
		slot->st = COPYING;
		slot_generation = slot->gen;
	}
	spin_unlock_irqrestore(&d->qlock, flags);
	if (!slot) {
		result = -ECANCELED;
		goto out;
	}
	mutex_unlock(&d->lock);
	/* Coherent allocation: no streaming-map sync call is applicable here.
	 * The slot remains COPYING until the entire user copy has finished.
	 */
	result = copy_to_user(buffer, slot->cpu, ZED_RXTH_BLOCK_BYTES) ?
		-EFAULT : ZED_RXTH_BLOCK_BYTES;
	mutex_lock(&d->lock);
	spin_lock_irqsave(&d->qlock, flags);
	if (!d->dma_unsafe)
		slot->st = FREE;
	if (d->streaming && !d->poisoned && !d->removing)
		WRITE_ONCE(d->halted, false);
	spin_unlock_irqrestore(&d->qlock, flags);
	if (slot_generation != d->gen)
		result = d->removing ? -ENODEV : -ECANCELED;
	if (result < 0) {
		atomic64_inc(&d->delivery);
		stop(d, result);
	} else if (d->streaming) {
		schedule_work(&d->refill);
	}
out:
	mutex_unlock(&d->lock);
	mutex_unlock(&d->read_lock);
	return result;
}
static long io(struct file *f, unsigned int x, unsigned long a)
{
	struct d *d = f->private_data;
	struct zed_rxth_timeout t;
	struct zed_rxth_info i = {
		.abi_version = ZED_RXTH_ABI_VERSION,
		.format_version = ZED_RXTH_FORMAT_VERSION,
		.block_bytes = ZED_RXTH_BLOCK_BYTES,
		.header_bytes = ZED_RXTH_HEADER_BYTES,
		.samples_per_block = ZED_RXTH_SAMPLES_PER_BLOCK,
		.bytes_per_sample = ZED_RXTH_BYTES_PER_SAMPLE,
		.flags = ZED_RXTH_INFO_DMAENGINE_OWNER |
			ZED_RXTH_INFO_EXCLUSIVE_OPEN |
			ZED_RXTH_INFO_FIXED_BLOCK |
			ZED_RXTH_INFO_NO_MMAP |
			ZED_RXTH_INFO_STICKY_IDLE_ABORT,
		.reserved = 0,
	};
	struct zed_rxth_status s;

	if (x == ZED_RXTH_IOC_GET_INFO)
		return copy_to_user((void __user *)a, &i, sizeof(i)) ? -EFAULT : 0;
	if (x == ZED_RXTH_IOC_ABORT) {
		mutex_lock(&d->lock);
		/* Set before stop() so a late read cannot re-start DMA after this
		 * owner-scoped cancellation linearizes. */
		d->cancel_latched = true;
		stop(d, -ECANCELED);
		atomic64_inc(&d->aborted);
		mutex_unlock(&d->lock);
		return 0;
	}
	if (x == ZED_RXTH_IOC_SET_TIMEOUT) {
		if (copy_from_user(&t, (void __user *)a, sizeof(t)) || !t.timeout_ms ||
		    t.timeout_ms > ZED_RXTH_TIMEOUT_MAX_MS || t.reserved)
			return -EINVAL;
		mutex_lock(&d->lock);
		if (d->streaming) {
			mutex_unlock(&d->lock);
			return -EBUSY;
		}
		d->to = t.timeout_ms;
		mutex_unlock(&d->lock);
		return 0;
	}
	if (x != ZED_RXTH_IOC_GET_STATUS)
		return -ENOTTY;
	mutex_lock(&d->lock);
	s.submitted_blocks = atomic64_read(&d->submitted);
	s.completed_blocks = atomic64_read(&d->completed);
	s.aborted_blocks = atomic64_read(&d->aborted);
	s.timeout_blocks = atomic64_read(&d->timedout);
	s.delivery_failures = atomic64_read(&d->delivery);
	/* The existing last_error field exposes -ECANCELED for both idle and
	 * active ABORT without changing the fixed status ABI layout. */
	s.last_error = d->err;
	s.state = d->removing ? ZED_RXTH_STATE_REMOVING :
		(d->poisoned ? ZED_RXTH_STATE_POISONED :
		 (d->streaming || d->nready ? ZED_RXTH_STATE_ACTIVE : ZED_RXTH_STATE_IDLE));
	s.timeout_ms = d->to;
	s.reserved = 0;
	mutex_unlock(&d->lock);
	return copy_to_user((void __user *)a, &s, sizeof(s)) ? -EFAULT : 0;
}

static int op(struct inode *n, struct file *f)
{
	struct d *d = container_of(f->private_data, struct d, misc);

	mutex_lock(&d->lock);
	if (d->removing || atomic_cmpxchg(&d->open, 0, 1)) {
		mutex_unlock(&d->lock);
		return d->removing ? -ENODEV : -EBUSY;
	}
	/* A successful new open is the only normal reuse boundary. */
	d->cancel_latched = false;
	kref_get(&d->ref);
	mutex_unlock(&d->lock);
	f->private_data = d;
	return 0;
}

static int rel(struct inode *n, struct file *f)
{
	struct d *d = f->private_data;

	mutex_lock(&d->lock);
	stop(d, -ECANCELED);
	mutex_unlock(&d->lock);
	cancel_work_sync(&d->refill);
	atomic_set(&d->open, 0);
	kref_put(&d->ref, destroy);
	return 0;
}

static const struct file_operations fo = {
	.owner = THIS_MODULE, .open = op, .release = rel, .read = rxread,
	.unlocked_ioctl = io, .llseek = noop_llseek,
};
static int probe(struct platform_device *p)
{
	struct dma_slave_config config = { .direction = DMA_DEV_TO_MEM };
	struct d *d;
	int i, result;

	d = kzalloc(sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	d->dev = get_device(&p->dev);
	if (!d->dev) {
		kfree(d);
		return -ENODEV;
	}
	kref_init(&d->ref);
	mutex_init(&d->lock);
	mutex_init(&d->read_lock);
	spin_lock_init(&d->qlock);
	init_waitqueue_head(&d->wait);
	INIT_WORK(&d->refill, refill);
	d->to = 1000;
	d->chan = dma_request_chan(&p->dev,"rxth");
	if (IS_ERR(d->chan)) {
		result = PTR_ERR(d->chan);
		d->chan = NULL;
		goto bad;
	}
	/* Map coherent buffers in the DMA controller's address domain. */
	d->dma_dev = get_device(dmaengine_get_dma_device(d->chan));
	result = dmaengine_slave_config(d->chan, &config);
	if (result)
		goto bad;
	for (i = 0; i < N; i++) {
		d->s[i].d = d;
		d->s[i].n = i;
		d->s[i].cpu = dma_alloc_coherent(d->dma_dev, ZED_RXTH_BLOCK_BYTES,
					       &d->s[i].dma, GFP_KERNEL);
		if (!d->s[i].cpu) {
			result = -ENOMEM;
			goto bad;
		}
	}
	d->misc.minor = MISC_DYNAMIC_MINOR;
	d->misc.name = DEV;
	d->misc.fops = &fo;
	d->misc.parent = d->dev;
	result = misc_register(&d->misc);
	if (result)
		goto bad;
	platform_set_drvdata(p, d);
	return 0;
bad:
	kref_put(&d->ref, destroy);
	return result;
}
static void remove(struct platform_device *p)
{
	struct d *d = platform_get_drvdata(p);

	mutex_lock(&d->lock);
	d->removing = true;
	stop(d, -ENODEV);
	mutex_unlock(&d->lock);
	misc_deregister(&d->misc);
	cancel_work_sync(&d->refill);
	kref_put(&d->ref, destroy);
}

static ssize_t ring_capacity_show(struct device *dev,
		struct device_attribute *attribute, char *buffer)
{
	return sysfs_emit(buffer, "%u\n", (unsigned int)N);
}
static DEVICE_ATTR_RO(ring_capacity);

static ssize_t ring_halts_show(struct device *dev,
		struct device_attribute *attribute, char *buffer)
{
	struct d *d = dev_get_drvdata(dev);

	return sysfs_emit(buffer, "%lld\n", (long long)atomic64_read(&d->halts));
}
static DEVICE_ATTR_RO(ring_halts);

static ssize_t ready_blocks_show(struct device *dev,
		struct device_attribute *attribute, char *buffer)
{
	struct d *d = dev_get_drvdata(dev);

	return sysfs_emit(buffer, "%u\n", (unsigned int)READ_ONCE(d->nready));
}
static DEVICE_ATTR_RO(ready_blocks);

static struct attribute *rxth_attrs[] = {
	&dev_attr_ring_capacity.attr,
	&dev_attr_ring_halts.attr,
	&dev_attr_ready_blocks.attr,
	NULL,
};
ATTRIBUTE_GROUPS(rxth);

static const struct of_device_id mt[] = {
	{ .compatible = "mimo,rf-rx-transport-v1" }, {}
};
MODULE_DEVICE_TABLE(of, mt);
static struct platform_driver pd = {
	.probe = probe,
	.remove = remove,
	.driver = {
		.name = "zedboard-rx-transport",
		.of_match_table = mt,
		.dev_groups = rxth_groups,
	},
};
module_platform_driver(pd);
MODULE_LICENSE("GPL");
