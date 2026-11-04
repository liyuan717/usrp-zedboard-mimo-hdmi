#!/usr/bin/env python3
"""Compose a fresh, XSA-bound boot DTB. Never accesses a board/block device."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from build_reference_ps_spi_diagnostic import parse_fdt, encode_fdt

BASE_SHA = "8265aff89752d3b9bbd489f2789f1c3c9699b0da5aade4d1682d2df2fc141bf5"
XSA_SHA = "7a5780a74707806e589f0baffc1716bb8f0c48c3661f49949fdd8837ee1c552b"
RANGES = {
    "axi_iic_main": (0x41600000, 0x10000),
    "axi_hdmi_dma": (0x43000000, 0x10000),
    "hdmi_presentation_observer": (0x44A20000, 0x10000),
    "axi_hdmi_core": (0x70E00000, 0x10000),
    "axi_hdmi_clkgen": (0x79000000, 0x10000),
}
NEW_SUFFIXES = {
    "clock-controller@79000000", "dma-controller@43000000", "finite-tx@43c10000",
    "i2c@41600000", "i2c@41600000/hdmi@39", "i2c@41600000/hdmi@39/ports",
    "i2c@41600000/hdmi@39/ports/port@0", "i2c@41600000/hdmi@39/ports/port@0/endpoint",
    "i2c@41600000/hdmi@39/ports/port@1", "lcd-controller@70e00000",
    "lcd-controller@70e00000/port", "lcd-controller@70e00000/port/endpoint",
    "presentation-observer@44a20000",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def cells(*values):
    return struct.pack(">" + "I" * len(values), *values)


def symbol(nodes, name):
    value = nodes["/__symbols__"][name]
    require(value.endswith(b"\0"), "invalid symbol")
    path = value[:-1].decode("ascii")
    require(path in nodes, "symbol target absent: " + name)
    return path


def phandle(nodes, name):
    return nodes[symbol(nodes, name)]["phandle"]


def audit_hwh(data):
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
        root = ET.fromstring(archive.read("zedboard_rf_system.hwh"))
    for name, (base, size) in RANGES.items():
        matches = [x for x in root.findall(".//MEMRANGE") if x.get("INSTANCE") == name]
        require(bool(matches), "XSA missing address: " + name)
        for item in matches:
            require(int(item.get("BASEVALUE"), 0) == base and
                    int(item.get("HIGHVALUE"), 0) == base + size - 1,
                    "XSA address mismatch: " + name)
    modules = {m.get("INSTANCE"): m for m in root.findall(".//MODULE")}

    def net(instance, port):
        ports = [p for p in modules[instance].findall("./PORTS/PORT") if p.get("NAME") == port]
        require(len(ports) == 1, "XSA port missing/ambiguous")
        return ports[0].get("SIGNAME")

    require(net("hdmi_presentation_observer", "irq") == net("interrupt_concat", "In11"),
            "observer IRQ is not IRQ_F2P[11]")
    require(net("interrupt_concat", "dout") == net("ps7", "IRQ_F2P"), "GIC concat disconnected")
    require(net("axi_hdmi_dma", "irq") == net("interrupt_concat", "In6"), "HDMI DMA IRQ mismatch")
    require(net("axi_iic_main", "iic2intc_irpt") == net("interrupt_concat", "In7"), "I2C IRQ mismatch")
    return {name: {"base": base, "size": size} for name, (base, size) in RANGES.items()}


def normalize_dac(data):
    """DT overlay blobs cannot encode /delete-property/ against their base.

    Make the two already-intended DAC deletions explicit, with no other edits.
    """
    h, reserve, nodes = parse_fdt(data)
    dac = nodes[symbol(nodes, "cf_ad9361_dac_core_0")]
    removed = {key: dac.pop(key).hex() for key in ("dmas", "dma-names") if key in dac}
    return encode_fdt(h, reserve, nodes), removed


def audit_dtb(base_data, result_data):
    _, base_reserve, base = parse_fdt(base_data)
    _, result_reserve, nodes = parse_fdt(result_data)
    require(base_reserve == result_reserve, "reserved memory map changed")
    expected_new = {symbol(base, "fpga_axi") + "/" + suffix for suffix in NEW_SUFFIXES}
    require(set(nodes) - set(base) == expected_new, "unapproved new/missing DT nodes")
    allowed = {
        symbol(base, "rf_tx_dmac"): {"status"},
        symbol(base, "cf_ad9361_dac_core_0"): {"status", "dmas", "dma-names", "adi,axi-dds-default-scale"},
        symbol(base, "rf_rxth_dmac"): {"adi,expect-s-axis-tlast"},
        "/__symbols__": None,
    }
    for path, props in base.items():
        require(path in nodes, "base node removed: " + path)
        if path in allowed and allowed[path] is None:
            for key, value in props.items():
                require(nodes[path].get(key) == value, "base symbol changed: " + key)
            continue
        for key in set(props) | set(nodes[path]):
            if key not in allowed.get(path, set()):
                require(props.get(key) == nodes[path].get(key), "unapproved base change: " + path + ":" + key)
    observer = nodes[symbol(nodes, "hdmi_presentation_observer")]
    require(observer["compatible"] == b"seu,hdmi-presentation-observer-1.0\0", "observer compatible")
    require(observer["reg"] == cells(0x44A20000, 0x10000), "observer range")
    require(observer["interrupt-parent"] == phandle(nodes, "intc"), "observer GIC")
    require(observer["interrupts"] == cells(0, 55, 4), "observer IRQ")
    require(observer["clocks"] == phandle(nodes, "clkc") + cells(15), "observer clock")
    core = nodes[symbol(nodes, "axi_hdmi_core")]
    require(core["adi,presentation-observer"] == phandle(nodes, "hdmi_presentation_observer"), "driver observer binding")
    require(core["dmas"] == phandle(nodes, "hdmi_dma") + cells(0), "HDMI DMA binding")
    require(nodes[symbol(nodes, "hdmi_dma")]["interrupts"] == cells(0, 35, 4), "HDMI DMA IRQ")
    require(nodes[symbol(nodes, "axi_iic_main")]["interrupts"] == cells(0, 36, 4), "HDMI I2C IRQ")
    for name, instance in (("axi_iic_main", "axi_iic_main"), ("hdmi_dma", "axi_hdmi_dma"),
                           ("axi_hdmi_core", "axi_hdmi_core"), ("hdmi_clock", "axi_hdmi_clkgen")):
        reg = nodes[symbol(nodes, name)]["reg"]
        address, size = struct.unpack(">II", reg)
        expected, extent = RANGES[instance]
        require(address == expected and 0 < size <= extent, "HDMI range: " + name)
    opted = [path for path, props in nodes.items() if "adi,expect-s-axis-tlast" in props]
    require(opted == [symbol(nodes, "rf_rxth_dmac")], "TLAST must opt in exactly RXTH")
    require(nodes[opted[0]]["reg"] == cells(0x7C440000, 0x10000), "RXTH range")
    require(nodes[symbol(nodes, "rf_tx_dmac")]["status"] == b"disabled\0", "TX DMA active")
    require(nodes[symbol(nodes, "finite_tx")]["status"] == b"disabled\0", "finite TX active")
    dac = nodes[symbol(nodes, "cf_ad9361_dac_core_0")]
    require("dmas" not in dac and "dma-names" not in dac and dac["adi,axi-dds-default-scale"] == cells(0), "DAC autostart risk")
    for endpoint, target in (("axi_hdmi_out", "adv7511_in"), ("adv7511_in", "axi_hdmi_out")):
        require(nodes[symbol(nodes, endpoint)]["remote-endpoint"] == phandle(nodes, target), "HDMI graph link")
    return {"base_nodes_preserved": len(base), "result_nodes": len(nodes),
            "rxth_tlast_only": opted[0], "physical_acceptance": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--xsa", type=Path, required=True)
    p.add_argument("--hdmi-overlay", type=Path, required=True)
    p.add_argument("--observer-overlay", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--dtc", default="dtc")
    p.add_argument("--fdtoverlay", default="fdtoverlay")
    a = p.parse_args()
    inputs = {name: path.read_bytes() for name, path in (("base", a.base), ("xsa", a.xsa),
              ("hdmi-overlay", a.hdmi_overlay), ("observer-overlay", a.observer_overlay))}
    require(digest(inputs["base"]) == BASE_SHA, "unapproved RX9 base DTB")
    require(digest(inputs["xsa"]) == XSA_SHA, "unapproved hp1v1t XSA")
    hwh = audit_hwh(inputs["xsa"])
    a.out.mkdir(parents=True, exist_ok=False)
    for name, blob in inputs.items():
        (a.out / name).write_bytes(blob)
    commands = []

    def run(argv):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        commands.append({"argv": argv, "returncode": proc.returncode,
                         "stdout": proc.stdout, "stderr": proc.stderr})
        (a.out / "commands.json").write_text(json.dumps(commands, indent=2), encoding="utf-8")
        require(proc.returncode == 0, "device tree tool failed; inspect commands.json")

    for name in ("hdmi-overlay", "observer-overlay"):
        run([a.dtc, "-@", "-I", "dts", "-O", "dtb", "-o", str(a.out / (name + ".dtbo")), str(a.out / name)])
    run([a.fdtoverlay, "-i", str(a.out / "base"), "-o", str(a.out / "merged.dtb"),
         str(a.out / "hdmi-overlay.dtbo"), str(a.out / "observer-overlay.dtbo")])
    result, removed = normalize_dac((a.out / "merged.dtb").read_bytes())
    audit = audit_dtb(inputs["base"], result)
    (a.out / "devicetree.dtb").write_bytes(result)
    receipt = {"schema": "mimo-hdmi-pipeline-dtb-v1", "status": "OFFLINE_DT_AUDIT_PASSED_HARDWARE_PENDING",
               "inputs": {name: {"sha256": digest(blob), "bytes": len(blob)} for name, blob in inputs.items()},
               "hwh": hwh, "audit": audit, "explicit_dac_property_deletions": removed,
               "output": {"sha256": digest(result), "bytes": len(result)},
               "requires": "matching RXTH TLAST-opt-in kernel and observer-enabled HDMI driver"}
    (a.out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
