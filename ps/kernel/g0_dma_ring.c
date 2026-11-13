// SPDX-License-Identifier: GPL-2.0
/* G0 DMA ownership probe on the existing finite 16B ADC recorder. NOT the
 * production SOURCE/batch ABI. No RF processing, TX control or cyclic overwrite.
 * Free buffers are queued once, completed -> ready -> user -> explicit release.
 * The real provider/mmap/stop contracts are measured independently of RF video.
 */
#include <linux/dmaengine.h>
#include <linux/dma-mapping.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/kref.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/poll.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include "g0_dma_ring.h"

#define CSR_RUN 8
#define CSR_STATUS 12
#define CSR_DELIVERED 16
#define CSR_OVERFLOW 24
#define CSR_BAD_KEEP 28
#define CSR_ACCEPTED 36
#define CSR_LIMIT 44
#define TOTAL_BYTES (V3_RING_SLOTS * V3_RING_SLOT_BYTES)
#define ALLOC_BYTES (TOTAL_BYTES + V3_RING_SLOT_BYTES)
enum slot_state { SLOT_FREE, SLOT_QUEUED, SLOT_READY, SLOT_USER };
struct ring;
struct ring_slot {
	struct ring *ring;
	enum slot_state state;
	u32 generation;
	u64 sequence;
};
struct ring {
	struct kref refs;
	struct device *dev, *dma_dev;
	void __iomem *csr;
	struct dma_chan *chan;
	void *memory;
	dma_addr_t dma;
	struct miscdevice misc;
	struct mutex control;
	spinlock_t lock;
	wait_queue_head_t wait;
	atomic_t opened;
	struct ring_slot slots[V3_RING_SLOTS];
	bool running, dead, poisoned, pinned;
	int error;
	u32 generation, planned, submitted, completed;
	u64 next_sequence;
};

static void ring_free(struct kref *ref)
{
	struct ring *d = container_of(ref, struct ring, refs);
	dma_free_coherent(d->dma_dev, ALLOC_BYTES, d->memory, d->dma);
	dma_release_channel(d->chan);
	put_device(d->dma_dev);
	put_device(d->dev);
	kfree(d);
}

static void ring_done(void *opaque, const struct dmaengine_result *result)
{
	struct ring_slot *s = opaque;
	struct ring *d = s->ring;
	unsigned long flags;
	spin_lock_irqsave(&d->lock, flags);
	if (d->running && s->state == SLOT_QUEUED && s->generation == d->generation) {
		if (!result || result->result != DMA_TRANS_NOERROR || result->residue) {
			d->error = -EIO;
			d->poisoned = true;
			d->running = false;
		} else {
			dma_rmb();
			s->state = SLOT_READY;
			d->completed++;
		}
	}
	spin_unlock_irqrestore(&d->lock, flags);
	wake_up_interruptible(&d->wait);
}

/* control mutex serializes submit/stop; IRQ only publishes completed buffers. */
static int ring_queue(struct ring *d)
{
	unsigned long flags;
	unsigned int i;
	for (i = 0; i < V3_RING_SLOTS; i++) {
		struct ring_slot *s = &d->slots[i];
		struct dma_async_tx_descriptor *tx;
		dma_cookie_t cookie;
		spin_lock_irqsave(&d->lock, flags);
		if (!d->running || d->submitted >= d->planned || s->state != SLOT_FREE) {
			spin_unlock_irqrestore(&d->lock, flags);
			continue;
		}
		s->state = SLOT_QUEUED;
		s->generation = d->generation;
		s->sequence = d->next_sequence++;
		d->submitted++;
		spin_unlock_irqrestore(&d->lock, flags);
		tx = dmaengine_prep_slave_single(d->chan, d->dma + i * V3_RING_SLOT_BYTES,
			V3_RING_SLOT_BYTES, DMA_DEV_TO_MEM, DMA_PREP_INTERRUPT | DMA_CTRL_ACK);
		if (!tx)
			return -ENOMEM;
		tx->callback_result = ring_done;
		tx->callback_param = s;
		cookie = dmaengine_submit(tx);
		if (dma_submit_error(cookie))
			return dma_submit_error(cookie);
	}
	dma_async_issue_pending(d->chan);
	return 0;
}

static int ring_stop(struct ring *d)
{
	struct dma_async_tx_descriptor *drain;
	unsigned long flags;
	u32 status;
	int ret, term;
	unsigned int i;
	spin_lock_irqsave(&d->lock, flags);
	d->running = false;
	spin_unlock_irqrestore(&d->lock, flags);
	/* A dedicated, unmapped drain buffer makes STOP independent of user
	 * leases. If every user buffer is held, the provider FIFO can otherwise
	 * be full and prevent the PL's final admitted record from draining. */
	if (readl(d->csr + CSR_STATUS) & 6) {
		drain = dmaengine_prep_slave_single(d->chan, d->dma + TOTAL_BYTES,
			V3_RING_SLOT_BYTES, DMA_DEV_TO_MEM, DMA_CTRL_ACK);
		if (drain) {
			dma_cookie_t cookie = dmaengine_submit(drain);
			if (!dma_submit_error(cookie)) dma_async_issue_pending(d->chan);
		}
	}
	writel(0, d->csr + CSR_RUN);
	/* Keep DMA enabled while the PL finishes its currently admitted 16B
	 * record. Then provider terminate_sync disables DMA and joins callbacks. */
	ret = read_poll_timeout(readl, status, !(status & 6), 10, 100000,
		false, d->csr + CSR_STATUS);
	term = dmaengine_terminate_sync(d->chan);
	if (term && !d->pinned) {
		d->pinned = true;
		d->poisoned = true;
		kref_get(&d->refs);
		__module_get(THIS_MODULE);
		dev_err(d->dev, "DMA termination failed; memory/provider pinned\n");
	}
	if (term)
		ret = term;
	spin_lock_irqsave(&d->lock, flags);
	if (ret) {
		d->poisoned = true;
		d->error = ret;
	} else {
		for (i = 0; i < V3_RING_SLOTS; i++)
			if (d->slots[i].state == SLOT_QUEUED)
				d->slots[i].state = SLOT_FREE;
	}
	spin_unlock_irqrestore(&d->lock, flags);
	wake_up_interruptible(&d->wait);
	return ret;
}

static u64 ring_count(struct ring *d, u32 offset)
{
	u32 hi = readl(d->csr + offset + 4), lo = readl(d->csr + offset);
	u32 again = readl(d->csr + offset + 4);
	if (hi != again) {
		hi = again;
		lo = readl(d->csr + offset);
	}
	return ((u64)hi << 32) | lo;
}

static long ring_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct ring *d = file->private_data;
	struct v3_ring_start start;
	struct v3_ring_desc desc = {};
	struct v3_ring_status state = {};
	unsigned long flags;
	unsigned int i, found = V3_RING_SLOTS;
	int ret = 0;
	mutex_lock(&d->control);
	if (d->dead) { ret = -ENODEV; goto done; }
	switch (cmd) {
	case V3_RING_START:
		if (copy_from_user(&start, (void __user *)arg, sizeof(start))) { ret = -EFAULT; break; }
		if (!start.batches || start.batches > 1024 || start.reserved) { ret = -EINVAL; break; }
		if (d->poisoned) { ret = -EIO; break; }
		if (d->running || (readl(d->csr + CSR_STATUS) & 6)) { ret = -EBUSY; break; }
		if (!(readl(d->csr + CSR_STATUS) & 1)) { ret = -ENOLINK; break; }
		for (i = 0; i < V3_RING_SLOTS; i++)
			if (d->slots[i].state != SLOT_FREE) { ret = -EBUSY; break; }
		if (ret) break;
		d->generation++;
		d->planned = start.batches;
		d->submitted = d->completed = 0;
		d->error = 0;
		writel(start.batches * (V3_RING_SLOT_BYTES / 16), d->csr + CSR_LIMIT);
		if (readl(d->csr + CSR_LIMIT) != start.batches * (V3_RING_SLOT_BYTES / 16)) { ret = -EIO; break; }
		spin_lock_irqsave(&d->lock, flags);
		d->running = true;
		spin_unlock_irqrestore(&d->lock, flags);
		ret = ring_queue(d);
		if (ret) { ring_stop(d); break; }
		writel(1, d->csr + CSR_RUN);
		break;
	case V3_RING_ACQUIRE:
		spin_lock_irqsave(&d->lock, flags);
		for (i = 0; i < V3_RING_SLOTS; i++)
			if (d->slots[i].state == SLOT_READY &&
			    (found == V3_RING_SLOTS || d->slots[i].sequence < d->slots[found].sequence)) found = i;
		if (found != V3_RING_SLOTS) {
			struct ring_slot *s = &d->slots[found];
			s->state = SLOT_USER;
			desc.slot = found; desc.generation = s->generation;
			desc.bytes = V3_RING_SLOT_BYTES; desc.sequence = s->sequence;
		} else ret = d->error ? d->error : -EAGAIN;
		spin_unlock_irqrestore(&d->lock, flags);
		if (!ret && copy_to_user((void __user *)arg, &desc, sizeof(desc))) {
			spin_lock_irqsave(&d->lock, flags);
			d->slots[found].state = SLOT_READY;
			spin_unlock_irqrestore(&d->lock, flags);
			ret = -EFAULT;
		}
		break;
	case V3_RING_RELEASE:
		if (copy_from_user(&desc, (void __user *)arg, sizeof(desc))) { ret = -EFAULT; break; }
		if (desc.slot >= V3_RING_SLOTS || desc.bytes != V3_RING_SLOT_BYTES || desc.reserved) { ret = -EINVAL; break; }
		spin_lock_irqsave(&d->lock, flags);
		if (d->slots[desc.slot].state != SLOT_USER || d->slots[desc.slot].generation != desc.generation ||
		    d->slots[desc.slot].sequence != desc.sequence) ret = -ESTALE;
		else d->slots[desc.slot].state = SLOT_FREE;
		spin_unlock_irqrestore(&d->lock, flags);
		if (!ret && d->running) {
			ret = ring_queue(d);
			if (ret) { d->error = ret; ring_stop(d); }
		}
		break;
	case V3_RING_STOP:
		ret = ring_stop(d);
		break;
	case V3_RING_STATUS:
		state.abi = V3_RING_ABI; state.slots = V3_RING_SLOTS; state.slot_bytes = V3_RING_SLOT_BYTES;
		spin_lock_irqsave(&d->lock, flags);
		state.generation = d->generation; state.running = d->running; state.poisoned = d->poisoned;
		state.pinned = d->pinned; state.error = d->error;
		state.planned = d->planned; state.submitted = d->submitted; state.completed = d->completed;
		for (i = 0; i < V3_RING_SLOTS; i++) {
			state.ready += d->slots[i].state == SLOT_READY;
			state.queued += d->slots[i].state == SLOT_QUEUED;
			state.user += d->slots[i].state == SLOT_USER;
		}
		spin_unlock_irqrestore(&d->lock, flags);
		state.overflow = readl(d->csr + CSR_OVERFLOW); state.bad_keep = readl(d->csr + CSR_BAD_KEEP);
		state.accepted = ring_count(d, CSR_ACCEPTED); state.delivered = ring_count(d, CSR_DELIVERED);
		if (copy_to_user((void __user *)arg, &state, sizeof(state))) ret = -EFAULT;
		break;
	default: ret = -ENOTTY;
	}
done:
	mutex_unlock(&d->control);
	return ret;
}

static __poll_t ring_poll(struct file *file, poll_table *wait)
{
	struct ring *d = file->private_data;
	unsigned long flags;
	__poll_t mask = 0;
	unsigned int i;
	poll_wait(file, &d->wait, wait);
	spin_lock_irqsave(&d->lock, flags);
	if (d->dead || d->error) mask |= EPOLLERR;
	for (i = 0; i < V3_RING_SLOTS; i++) if (d->slots[i].state == SLOT_READY) mask |= EPOLLIN | EPOLLRDNORM;
	spin_unlock_irqrestore(&d->lock, flags);
	return mask;
}

static int ring_open(struct inode *inode, struct file *file)
{
	struct ring *d = container_of(file->private_data, struct ring, misc);
	if (atomic_cmpxchg(&d->opened, 0, 1)) return -EBUSY;
	mutex_lock(&d->control);
	if (d->dead) { mutex_unlock(&d->control); atomic_set(&d->opened, 0); return -ENODEV; }
	kref_get(&d->refs); file->private_data = d;
	mutex_unlock(&d->control);
	return nonseekable_open(inode, file);
}

static int ring_close(struct inode *inode, struct file *file)
{
	struct ring *d = file->private_data;
	unsigned long flags;
	unsigned int i;
	mutex_lock(&d->control);
	if (!d->dead) ring_stop(d);
	/* vm_file retains this open file until all mappings disappear. Only
	 * this final release can reclaim leases abandoned by a dead consumer. */
	spin_lock_irqsave(&d->lock, flags);
	if (!d->pinned)
		for (i = 0; i < V3_RING_SLOTS; i++) d->slots[i].state = SLOT_FREE;
	spin_unlock_irqrestore(&d->lock, flags);
	atomic_set(&d->opened, 0);
	mutex_unlock(&d->control);
	kref_put(&d->refs, ring_free);
	return 0;
}

static int ring_mmap(struct file *file, struct vm_area_struct *vma)
{
	struct ring *d = file->private_data;
	int ret;
	mutex_lock(&d->control);
	if (d->dead) ret = -ENODEV;
	else if (vma->vm_pgoff || vma->vm_end - vma->vm_start != TOTAL_BYTES || (vma->vm_flags & (VM_WRITE | VM_EXEC))) ret = -EINVAL;
	else {
		vm_flags_clear(vma, VM_MAYWRITE | VM_MAYEXEC);
		vm_flags_set(vma, VM_DONTEXPAND | VM_DONTDUMP);
		ret = dma_mmap_coherent(d->dma_dev, vma, d->memory, d->dma, TOTAL_BYTES);
	}
	mutex_unlock(&d->control);
	return ret;
}
static const struct file_operations ring_fops = {
	.owner = THIS_MODULE, .open = ring_open, .release = ring_close,
	.unlocked_ioctl = ring_ioctl, .poll = ring_poll, .mmap = ring_mmap,
};

static int ring_probe(struct platform_device *pdev)
{
	struct ring *d;
	struct resource *r = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	unsigned int i;
	int ret;
	if (!r || r->start != 0x7c430000 || resource_size(r) != 0x10000) return -EINVAL;
	d = kzalloc(sizeof(*d), GFP_KERNEL); if (!d) return -ENOMEM;
	d->dev = get_device(&pdev->dev); kref_init(&d->refs);
	mutex_init(&d->control); spin_lock_init(&d->lock); init_waitqueue_head(&d->wait); atomic_set(&d->opened, 0);
	d->csr = devm_ioremap_resource(&pdev->dev, r);
	if (IS_ERR(d->csr)) { ret = PTR_ERR(d->csr); goto fail; }
	if (readl(d->csr) != 0x44304756 || readl(d->csr + 4) != 0x20261102 || (readl(d->csr + CSR_STATUS) & 6)) { ret = -ENODEV; goto fail; }
	d->chan = dma_request_chan(&pdev->dev, "output");
	if (IS_ERR(d->chan)) { ret = PTR_ERR(d->chan); goto fail; }
	d->dma_dev = get_device(d->chan->device->dev);
	d->memory = dma_alloc_coherent(d->dma_dev, ALLOC_BYTES, &d->dma, GFP_KERNEL);
	if (!d->memory) { ret = -ENOMEM; goto channel; }
	for (i = 0; i < V3_RING_SLOTS; i++) d->slots[i].ring = d;
	d->misc.minor = MISC_DYNAMIC_MINOR; d->misc.name = "v3-g0-dma-ring"; d->misc.fops = &ring_fops; d->misc.parent = &pdev->dev;
	ret = misc_register(&d->misc); if (ret) { dma_free_coherent(d->dma_dev, ALLOC_BYTES, d->memory, d->dma); goto channel; }
	platform_set_drvdata(pdev, d);
	dev_info(&pdev->dev, "G0 queued DMA ring: 8x32KiB; ADC diagnostic only, no TX\n");
	return 0;
channel:
	dma_release_channel(d->chan); put_device(d->dma_dev);
fail:
	put_device(d->dev); kfree(d); return ret;
}

static void ring_remove(struct platform_device *pdev)
{
	struct ring *d = platform_get_drvdata(pdev);
	misc_deregister(&d->misc);
	mutex_lock(&d->control); d->dead = true; ring_stop(d); mutex_unlock(&d->control);
	kref_put(&d->refs, ring_free);
}
static const struct of_device_id ring_match[] = { { .compatible = "seu,video-radio-v3-g0-adc" }, {} };
MODULE_DEVICE_TABLE(of, ring_match);
static struct platform_driver ring_driver = { .probe = ring_probe, .remove = ring_remove,
	.driver = { .name = "video-radio-v3-g0-ring", .of_match_table = ring_match } };
module_platform_driver(ring_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Video Radio V3 G0 queued DMA ownership probe; not cyclic RF video");
