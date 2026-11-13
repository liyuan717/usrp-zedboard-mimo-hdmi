#!/usr/bin/env python3
"""Exact, fail-closed vendor overlay patch for the util_wfifo read reset.

This does not make a stopped read clock clear FIFO state.  The surrounding
``rfv2_capture_reset_guard`` owns that asynchronous fail-closed isolation.
It changes only the RAM-facing read-state reset so reset cannot feed a BRAM
address/enable through an asynchronous-control register.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPECTED_PARENT_SHA256 = "b5fa20b867651d45a600df19675ac6a904ece8759acd82cf82c048477d2f8a85"
REL = "library/util_wfifo/util_wfifo.v"
CORE_REL = "library/axi_hdmi_tx/axi_hdmi_tx_core.v"
VDMA_REL = "library/axi_hdmi_tx/axi_hdmi_tx_vdma.v"
EXPECTED_CORE_SHA256 = "791ed81ffc399ef50c1bd6691112d2a0bc32aa25aaaaadd8f7040f738aed7684"
EXPECTED_VDMA_SHA256 = "fdd90c0864b6b6a5bd6122f845d105d48ff240890047f8fa07d83d4c96a084c3"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_once(text: str, before: str, after: str, label: str) -> str:
    count = text.count(before)
    if count != 1:
        raise ValueError(f"{label}: expected exactly one anchor, found {count}")
    return text.replace(before, after)


def patch_util_wfifo(parent: Path) -> str:
    """Return the only admitted WFIFO delta from the exact r6 parent source."""
    if sha256(parent) != EXPECTED_PARENT_SHA256:
        raise ValueError(f"unexpected util_wfifo parent SHA256: {sha256(parent)}")
    text = parent.read_text(encoding="utf-8")
    before = '''  always @(posedge dout_clk or negedge dout_rstn) begin
    if (dout_rstn == 1'b0) begin
      dout_req_cnt <= 'd0;
      dout_raddr <= 'd8;
      dout_rd_d <= 'd0;
      dout_valid <= 'd0;
    end else begin
      if (dout_req_t == 1'b1) begin
        dout_req_cnt <= 4'h8;
        dout_raddr <= {dout_rinit, 3'd0};
      end else if (dout_req_cnt[3] == 1'b1) begin
        dout_req_cnt <= dout_req_cnt + 1'b1;
        dout_raddr <= dout_raddr + 1'b1;
      end
      dout_rd_d <= dout_req_cnt[3];
      dout_valid <= dout_rd_d;
    end
  end
'''
    after = '''  // Keep all state which reaches i_mem.addrb in the dout_clk domain.
  // dout_rstn may assert while dout_clk is stopped: the capture reset guard
  // immediately fences CPACK/elastic/public output, while this RAM-facing
  // state intentionally holds its address until the first local clock edge.
  // On that edge it resets before any request can produce dout_valid.  Do not
  // add dout_rstn to this sensitivity list: that would infer async control on
  // the BRAM address path and reintroduce REQP-1839.
  always @(posedge dout_clk) begin
    if (dout_rstn == 1'b0) begin
      dout_req_cnt <= 'd0;
      dout_raddr <= 'd8;
      dout_rd_d <= 'd0;
      dout_valid <= 'd0;
    end else begin
      if (dout_req_t == 1'b1) begin
        dout_req_cnt <= 4'h8;
        dout_raddr <= {dout_rinit, 3'd0};
      end else if (dout_req_cnt[3] == 1'b1) begin
        dout_req_cnt <= dout_req_cnt + 1'b1;
        dout_raddr <= dout_raddr + 1'b1;
      end
      dout_rd_d <= dout_req_cnt[3];
      dout_valid <= dout_rd_d;
    end
  end
'''
    return replace_once(text, before, after, "WFIFO RAM-facing read-state block")


def patch_epoch_source(parent: Path, rel: str) -> str:
    """Pin only the two epoch protocol source flops against phys_opt copying.

    The companion CDC lane applies exact max-delay constraints to these
    original source registers.  The attributes are deliberately not placed on
    either synchronizer chain or a wildcard/replica name; functionality and
    CDC structure remain unchanged.
    """
    expected = {CORE_REL: EXPECTED_CORE_SHA256, VDMA_REL: EXPECTED_VDMA_SHA256}
    anchors = {
        CORE_REL: ("  output  reg             hdmi_epoch_ready,\n",
                   "  (* DONT_TOUCH = \"TRUE\" *) output reg             hdmi_epoch_ready,\n"),
        VDMA_REL: ("  output  reg             vdma_epoch_ack,\n",
                   "  (* DONT_TOUCH = \"TRUE\" *) output reg             vdma_epoch_ack,\n"),
    }
    if rel not in expected:
        raise ValueError(f"unsupported epoch source: {rel}")
    if sha256(parent) != expected[rel]:
        raise ValueError(f"unexpected {rel} parent SHA256: {sha256(parent)}")
    before, after = anchors[rel]
    return replace_once(parent.read_text(encoding="utf-8"), before, after, f"{rel} epoch source")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    parent, out = args.parent.resolve(), args.out.resolve()
    if out.exists():
        raise SystemExit(f"refusing to overwrite: {out}")
    patched = patch_util_wfifo(parent)
    out.mkdir(parents=True)
    target = out / "util_wfifo.v"
    target.write_text(patched, encoding="utf-8", newline="\n")
    receipt = {"status": "PATCHED_NOT_BUILT", "parent": str(parent),
               "parent_sha256": sha256(parent), "output_sha256": sha256(target),
               "contract": "RAM-facing dout read state has synchronous reset; external asynchronous isolation remains guard-owned."}
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
