#!/usr/bin/env python3
"""Render the HDMI-only AXI-DMAC termination-drain provider overlay.

The ADI DMAengine provider has a software-only ``terminate_all``: it writes
CTRL=0 and retires virtual descriptors, but the AXI-DMAC reset manager may
still be in STATE_SHUTDOWN while an AXIS word is held.  This renderer changes
only an explicitly opted-in provider instance.  It is deliberately based on
unique source anchors instead of a source hash: the caller must inspect and
test the generated provider before compiling it.

It never modifies the supplied source tree.  The output must be a fresh path.
"""
from __future__ import annotations

import argparse
from pathlib import Path


PROPERTY = "adi,terminate-sync-dbg-status"


def replace_once(text: str, before: str, after: str, label: str) -> str:
    count = text.count(before)
    if count != 1:
        raise ValueError(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(before, after)


def render(source: str) -> str:
    """Return the reviewed HDMI termination-drain variant of *source*."""
    source = replace_once(
        source,
        "#include <linux/io.h>\n",
        "#include <linux/io.h>\n#include <linux/iopoll.h>\n",
        "iopoll include",
    )
    source = replace_once(
        source,
        "#define AXI_DMAC_REG_DBG0\t\t0x43c\n",
        "#define AXI_DMAC_REG_DBG0\t\t0x43c\n\n"
        "/* HDMI opt-in: wait for a reset-manager terminal state after CTRL=0. */\n"
        f"#define AXI_DMAC_TERMINATE_SYNC_DBG_STATUS_PROPERTY\t\"{PROPERTY}\"\n"
        "#define AXI_DMAC_TERMINATE_SYNC_POLL_US\t\t50\n"
        "#define AXI_DMAC_TERMINATE_SYNC_TIMEOUT_US\t100000\n",
        "termination-drain register constants",
    )
    source = replace_once(
        source,
        "\tvoid __iomem *base;\n\tint irq;\n\n\tstruct dma_device dma_dev;",
        "\tvoid __iomem *base;\n\tint irq;\n\n"
        "\t/* Both fields remain false unless the DT node opts in. */\n"
        "\tbool terminate_sync_wait;\n"
        "\tbool terminate_sync_failed;\n"
        "\tu32 terminate_sync_dbg_status;\n\n"
        "\tstruct dma_device dma_dev;",
        "provider termination state",
    )
    source = replace_once(
        source,
        "static void axi_dmac_synchronize(struct dma_chan *c)\n"
        "{\n"
        "\tstruct axi_dmac_chan *chan = to_axi_dmac_chan(c);\n\n"
        "\tvchan_synchronize(&chan->vchan);\n"
        "}\n\n"
        "static void axi_dmac_issue_pending(struct dma_chan *c)\n"
        "{\n"
        "\tstruct axi_dmac_chan *chan = to_axi_dmac_chan(c);\n"
        "\tstruct axi_dmac *dmac = chan_to_axi_dmac(chan);\n"
        "\tunsigned long flags;\n"
        "\tu32 ctrl = AXI_DMAC_CTRL_ENABLE;\n",
        "static enum dma_status axi_dmac_tx_status(struct dma_chan *c,\n"
        "\t\t\t\t\t      dma_cookie_t cookie,\n"
        "\t\t\t\t\t      struct dma_tx_state *state)\n"
        "{\n"
        "\tstruct axi_dmac_chan *chan = to_axi_dmac_chan(c);\n"
        "\tstruct axi_dmac *dmac = chan_to_axi_dmac(chan);\n\n"
        "\tif (READ_ONCE(dmac->terminate_sync_failed))\n"
        "\t\treturn DMA_ERROR;\n\n"
        "\treturn dma_cookie_status(c, cookie, state);\n"
        "}\n\n"
        "static void axi_dmac_synchronize(struct dma_chan *c)\n"
        "{\n"
        "\tstruct axi_dmac_chan *chan = to_axi_dmac_chan(c);\n"
        "\tstruct axi_dmac *dmac = chan_to_axi_dmac(chan);\n"
        "\tu32 dbg_status;\n"
        "\tint ret;\n\n"
        "\tvchan_synchronize(&chan->vchan);\n\n"
        "\t/* dmaengine_synchronize() is sleepable; terminate_all() is not. */\n"
        "\tif (!dmac->terminate_sync_wait)\n"
        "\t\treturn;\n\n"
        "\tret = read_poll_timeout(axi_dmac_read, dbg_status,\n"
        "\t\t\t\taxi_dmac_read(dmac, AXI_DMAC_REG_CTRL) == 0 &&\n"
        "\t\t\t\tdbg_status == dmac->terminate_sync_dbg_status,\n"
        "\t\t\t\tAXI_DMAC_TERMINATE_SYNC_POLL_US,\n"
        "\t\t\t\tAXI_DMAC_TERMINATE_SYNC_TIMEOUT_US, false,\n"
        "\t\t\t\tdmac, AXI_DMAC_REG_DBG0);\n"
        "\tif (ret) {\n"
        "\t\tWRITE_ONCE(dmac->terminate_sync_failed, true);\n"
        "\t\tdev_err(dmac->dma_dev.dev,\n"
        "\t\t\t\"terminate drain timeout: ctrl=%#x dbg0=%#x expected=%#x\\n\",\n"
        "\t\t\taxi_dmac_read(dmac, AXI_DMAC_REG_CTRL), dbg_status,\n"
        "\t\t\tdmac->terminate_sync_dbg_status);\n"
        "\t\treturn;\n"
        "\t}\n\n"
        "\tWRITE_ONCE(dmac->terminate_sync_failed, false);\n"
        "}\n\n"
        "static void axi_dmac_issue_pending(struct dma_chan *c)\n"
        "{\n"
        "\tstruct axi_dmac_chan *chan = to_axi_dmac_chan(c);\n"
        "\tstruct axi_dmac *dmac = chan_to_axi_dmac(chan);\n"
        "\tunsigned long flags;\n"
        "\tu32 ctrl = AXI_DMAC_CTRL_ENABLE;\n\n"
        "\tif (unlikely(READ_ONCE(dmac->terminate_sync_failed))) {\n"
        "\t\tdev_err(dmac->dma_dev.dev,\n"
        "\t\t\t\"refusing DMA enable after unfinished termination drain\\n\");\n"
        "\t\treturn;\n"
        "\t}\n",
        "synchronize/issue-pending block",
    )
    source = replace_once(
        source,
        "\tdma_dev->device_tx_status = dma_cookie_status;\n",
        "\tdma_dev->device_tx_status = axi_dmac_tx_status;\n",
        "tx-status callback",
    )
    source = replace_once(
        source,
        "\tdmac->chan.expect_s_axis_tlast =\n"
        "\t\tdevice_property_read_bool(&pdev->dev, \"adi,expect-s-axis-tlast\");\n",
        "\tdmac->chan.expect_s_axis_tlast =\n"
        "\t\tdevice_property_read_bool(&pdev->dev, \"adi,expect-s-axis-tlast\");\n\n"
        "\tdmac->terminate_sync_wait =\n"
        "\t\tdevice_property_present(&pdev->dev,\n"
        "\t\t\tAXI_DMAC_TERMINATE_SYNC_DBG_STATUS_PROPERTY);\n"
        "\tif (dmac->terminate_sync_wait) {\n"
        "\t\tret = device_property_read_u32(&pdev->dev,\n"
        "\t\t\tAXI_DMAC_TERMINATE_SYNC_DBG_STATUS_PROPERTY,\n"
        "\t\t\t&dmac->terminate_sync_dbg_status);\n"
        "\t\tif (ret)\n"
        "\t\t\treturn ret;\n"
        "\t\tdev_info(&pdev->dev,\n"
        "\t\t\t \"termination drain enabled: dbg0 terminal %#x\\n\",\n"
        "\t\t\t dmac->terminate_sync_dbg_status);\n"
        "\t}\n",
        "DT opt-in parse",
    )
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.source.is_symlink() or not args.source.is_file():
        raise SystemExit("source must be a regular file")
    if args.output.exists() or args.output.is_symlink():
        raise SystemExit("refusing to overwrite output")
    rendered = render(args.source.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
