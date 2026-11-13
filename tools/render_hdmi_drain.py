#!/usr/bin/env python3
"""Apply the existing bounded-drain repair to a scoped G0 external provider."""
import importlib.util
from pathlib import Path


def render():
    root = Path(__file__).resolve().parents[1]
    reference = root / "vendor/hdmi_kernel/drain_reference.py"
    spec = importlib.util.spec_from_file_location("v3_drain_reference", reference)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    replace = module.replace_once
    def scoped_replace(text, before, after, label):
        if label == "DT opt-in parse":
            # The old diagnostic DT lacks the new property. This external
            # provider already requires an exact address, direction and override.
            # Preserve all drain/status semantics; make opting in explicit here.
            before = "\tINIT_LIST_HEAD(&dmac->chan.active_descs);\n"
            after = ("\tdmac->terminate_sync_wait = terminate_terminal != 0;\n"
                     "\tdmac->terminate_sync_dbg_status = terminate_terminal;\n"
                     "\tdev_info(&pdev->dev, \"V3 HDMI terminal state %#x\\n\", terminate_terminal);\n\n" + before)
        return replace(text, before, after, label)
    module.replace_once = scoped_replace
    text = module.render((root / "vendor/hdmi_kernel/base_oneshot.c").read_text())
    text = replace(text, "static bool enable_diag;\n",
                   "static uint terminate_terminal;\nmodule_param(terminate_terminal, uint, 0400);\n"
                   "MODULE_PARM_DESC(terminate_terminal, \"Explicit HDMI reset-manager terminal state; 0 disables wait\");\n"
                   "static bool enable_diag;\n", "diagnostic opt-in parameter")
    text = text.replace('"hdmi-dmac-oneshot"', '"video-radio-v3-hdmi-dmac"')
    out = root / "ps/kernel/g0_hdmi_dmac.c"
    if out.exists():
        raise ValueError("Fresh generated source path required")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(out)


if __name__ == "__main__":
    render()
