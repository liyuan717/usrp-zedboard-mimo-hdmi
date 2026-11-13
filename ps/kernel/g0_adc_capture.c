// SPDX-License-Identifier: GPL-2.0
/* Finite G0 measurement DMA owner. No RF algorithms or TX controls.
 * Sysfs: echo record_count > capture_records; read capture_data afterward.
 * The PL limit stops admission exactly at N complete 16-byte records. This
 * avoids speculative DMA FIFO tails and enables repeatable finite captures.
 * Failure never becomes a success by resetting counters or force-loading.
 */
#include <linux/completion.h>
#include <linux/dma-mapping.h>
#include <linux/dmaengine.h>
#include <linux/io.h>
#include <linux/iopoll.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/sysfs.h>

#define MAX_BYTES (32U * 1024U * 1024U)
#define RECORD_BYTES 16U
static unsigned int capture_mib = 8;
module_param(capture_mib, uint, 0444);
MODULE_PARM_DESC(capture_mib, "Finite capture allocation in MiB, 1..32 (default 8)");
#define CSR_RUN 8
#define CSR_STATUS 12
#define CSR_DELIVERED 16
#define CSR_OVERFLOW 24
#define CSR_BAD_KEEP 28
#define CSR_FENCES 32
#define CSR_ACCEPTED 36
#define CSR_LIMIT 44
#define CSR_REMAINING 48

struct g0_capture {
	struct device *dev, *dma_dev;
	void __iomem *csr;
	struct dma_chan *chan;
	void *buffer;
	dma_addr_t dma;
	struct mutex lock;
	struct completion done;
	size_t valid_bytes, buffer_bytes;
	bool poisoned, pinned;
	int error;
	u32 records, overflow, bad_keep, fences;
	u64 accepted, delivered;
};

static u64 g0_count(struct g0_capture *d, u32 offset)
{
	u32 hi, lo, again;
	do {
		hi = readl(d->csr + offset + 4);
		lo = readl(d->csr + offset);
		again = readl(d->csr + offset + 4);
	} while (hi != again);
	return ((u64)hi << 32) | lo;
}

static void g0_done(void *opaque)
{
	struct g0_capture *d = opaque;
	complete(&d->done);
}

static int g0_terminate(struct g0_capture *d)
{
	int ret = dmaengine_terminate_sync(d->chan);
	if (ret && !d->pinned) {
		/* Preserve DMA memory, provider/device references and code if the
		 * provider cannot establish that callbacks/writes have stopped. */
		d->pinned = true;
		d->poisoned = true;
		__module_get(THIS_MODULE);
		dev_err(d->dev, "DMA termination failed; capture memory pinned until reboot\n");
	}
	return ret;
}

static int g0_capture_records(struct g0_capture *d, u32 records)
{
	struct dma_async_tx_descriptor *tx;
	dma_cookie_t cookie;
	u64 accepted, delivered;
	u32 status, overflow, bad_keep, fences;
	int ret, term;
	size_t bytes = (size_t)records * RECORD_BYTES;

	if (d->poisoned)
		return -EIO;
	d->valid_bytes = 0;
	d->records = records;
	if (readl(d->csr + CSR_STATUS) & 6) {
		d->poisoned = true;
		return -EBUSY;
	}
	ret = read_poll_timeout(readl, status, status & 1, 500, 500000,
		false, d->csr + CSR_STATUS);
	if (ret)
		return -ENOLINK;
	accepted = g0_count(d, CSR_ACCEPTED);
	delivered = g0_count(d, CSR_DELIVERED);
	overflow = readl(d->csr + CSR_OVERFLOW);
	bad_keep = readl(d->csr + CSR_BAD_KEEP);
	fences = readl(d->csr + CSR_FENCES);
	writel(records, d->csr + CSR_LIMIT);
	if (readl(d->csr + CSR_LIMIT) != records)
		return -EIO;
	reinit_completion(&d->done);
	tx = dmaengine_prep_slave_single(d->chan, d->dma, bytes,
		DMA_DEV_TO_MEM, DMA_PREP_INTERRUPT | DMA_CTRL_ACK);
	if (!tx)
		return -ENOMEM;
	tx->callback = g0_done;
	tx->callback_param = d;
	cookie = dmaengine_submit(tx);
	ret = dma_submit_error(cookie);
	if (ret)
		goto stop;
	dma_async_issue_pending(d->chan);
	writel(1, d->csr + CSR_RUN);
	if (!wait_for_completion_timeout(&d->done, msecs_to_jiffies(2000))) {
		ret = -ETIMEDOUT;
		goto stop;
	}
	if (dma_async_is_tx_complete(d->chan, cookie, NULL, NULL) != DMA_COMPLETE) {
		ret = -EIO;
		goto stop;
	}
	ret = read_poll_timeout(readl, status, !(status & 6), 10, 100000,
		false, d->csr + CSR_STATUS);
	if (ret || readl(d->csr + CSR_REMAINING)) {
		ret = -EIO;
		goto stop;
	}
	dma_rmb();
stop:
	writel(0, d->csr + CSR_RUN);
	term = g0_terminate(d);
	if (term)
		ret = term;
	d->accepted = g0_count(d, CSR_ACCEPTED) - accepted;
	d->delivered = g0_count(d, CSR_DELIVERED) - delivered;
	d->overflow = readl(d->csr + CSR_OVERFLOW) - overflow;
	d->bad_keep = readl(d->csr + CSR_BAD_KEEP) - bad_keep;
	d->fences = readl(d->csr + CSR_FENCES) - fences;
	if (!ret && (d->accepted != records || d->delivered != records))
		ret = -EIO;
	if (ret) {
		/* A truncated descriptor may leave a half record at the AXIS
		 * boundary. A fresh hardware configuration is required to retry. */
		d->poisoned = true;
	} else {
		d->valid_bytes = bytes;
	}
	return ret;
}

static ssize_t capture_records_store(struct device *dev,
	struct device_attribute *attr, const char *buf, size_t count)
{
	struct g0_capture *d = dev_get_drvdata(dev);
	u32 records;
	int ret = kstrtou32(buf, 0, &records);
	if (ret)
		return ret;
	if (!records || records > d->buffer_bytes / RECORD_BYTES)
		return -ERANGE;
	mutex_lock(&d->lock);
	ret = g0_capture_records(d, records);
	d->error = ret;
	mutex_unlock(&d->lock);
	return ret ? ret : count;
}
static DEVICE_ATTR_WO(capture_records);

static ssize_t capture_state_show(struct device *dev,
	struct device_attribute *attr, char *buf)
{
	struct g0_capture *d = dev_get_drvdata(dev);
	ssize_t len;
	mutex_lock(&d->lock);
	len = sysfs_emit(buf, "error=%d poisoned=%u pinned=%u records=%u bytes=%zu accepted=%llu delivered=%llu overflow=%u bad_keep=%u fences=%u status=0x%x buffer_bytes=%zu\n",
		d->error, d->poisoned, d->pinned, d->records, d->valid_bytes,
		d->accepted, d->delivered, d->overflow, d->bad_keep, d->fences,
		readl(d->csr + CSR_STATUS), d->buffer_bytes);
	mutex_unlock(&d->lock);
	return len;
}
static DEVICE_ATTR_RO(capture_state);

static ssize_t capture_data_read(struct file *file, struct kobject *kobj,
	struct bin_attribute *attr, char *buf, loff_t offset, size_t count)
{
	struct g0_capture *d = dev_get_drvdata(kobj_to_dev(kobj));
	ssize_t len = 0;
	mutex_lock(&d->lock);
	if (offset >= 0 && offset < d->valid_bytes) {
		len = min_t(size_t, count, d->valid_bytes - offset);
		memcpy(buf, d->buffer + offset, len);
	}
	mutex_unlock(&d->lock);
	return len;
}
static struct bin_attribute capture_data = {
	.attr = { .name = "capture_data", .mode = 0400 },
	.size = MAX_BYTES,
	.read = capture_data_read,
};
static struct attribute *g0_attrs[] = {
	&dev_attr_capture_records.attr, &dev_attr_capture_state.attr, NULL,
};
static const struct attribute_group g0_group = { .attrs = g0_attrs };

static int g0_probe(struct platform_device *pdev)
{
	struct g0_capture *d;
	struct resource *resource;
	int ret;
	if (!capture_mib || capture_mib > MAX_BYTES / (1024U * 1024U))
		return -EINVAL;
	resource = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	if (!resource || resource->start != 0x7c430000 || resource_size(resource) != 0x10000)
		return -EINVAL;
	d = kzalloc(sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	d->dev = get_device(&pdev->dev);
	d->buffer_bytes = (size_t)capture_mib * 1024U * 1024U;
	mutex_init(&d->lock);
	init_completion(&d->done);
	d->csr = devm_ioremap_resource(&pdev->dev, resource);
	if (IS_ERR(d->csr)) {
		ret = PTR_ERR(d->csr);
		goto fail;
	}
	if (readl(d->csr) != 0x44304756 || readl(d->csr + 4) != 0x20261102 ||
		(readl(d->csr + CSR_STATUS) & 6)) {
		ret = -ENODEV;
		goto fail;
	}
	d->chan = dma_request_chan(&pdev->dev, "output");
	if (IS_ERR(d->chan)) {
		ret = PTR_ERR(d->chan);
		goto fail;
	}
	d->dma_dev = get_device(d->chan->device->dev);
	d->buffer = dma_alloc_coherent(d->dma_dev, d->buffer_bytes, &d->dma, GFP_KERNEL);
	if (!d->buffer) {
		ret = -ENOMEM;
		goto release;
	}
	platform_set_drvdata(pdev, d);
	ret = sysfs_create_group(&pdev->dev.kobj, &g0_group);
	if (ret)
		goto free_buffer;
	ret = sysfs_create_bin_file(&pdev->dev.kobj, &capture_data);
	if (ret) {
		sysfs_remove_group(&pdev->dev.kobj, &g0_group);
		goto free_buffer;
	}
	dev_info(&pdev->dev, "G0 finite ADC recorder ready; no RF transmission configured\n");
	return 0;
free_buffer:
	dma_free_coherent(d->dma_dev, d->buffer_bytes, d->buffer, d->dma);
release:
	dma_release_channel(d->chan);
	put_device(d->dma_dev);
fail:
	put_device(d->dev);
	kfree(d);
	return ret;
}

static void g0_remove(struct platform_device *pdev)
{
	struct g0_capture *d = platform_get_drvdata(pdev);
	sysfs_remove_bin_file(&pdev->dev.kobj, &capture_data);
	sysfs_remove_group(&pdev->dev.kobj, &g0_group);
	mutex_lock(&d->lock);
	writel(0, d->csr + CSR_RUN);
	if (d->pinned || g0_terminate(d)) {
		mutex_unlock(&d->lock);
		return;
	}
	mutex_unlock(&d->lock);
	dma_free_coherent(d->dma_dev, d->buffer_bytes, d->buffer, d->dma);
	dma_release_channel(d->chan);
	put_device(d->dma_dev);
	put_device(d->dev);
	kfree(d);
}

static const struct of_device_id g0_match[] = {
	{ .compatible = "seu,video-radio-v3-g0-adc" }, {},
};
MODULE_DEVICE_TABLE(of, g0_match);
static struct platform_driver g0_driver = {
	.probe = g0_probe, .remove = g0_remove,
	.driver = { .name = "video-radio-v3-g0-adc", .of_match_table = g0_match },
};
module_platform_driver(g0_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Finite tagged ADC measurement owner for Video Radio V3 G0");
