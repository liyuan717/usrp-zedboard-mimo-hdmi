"""Retire five extra read-only RF snapshots while preserving their register addresses."""
from hashlib import sha256
import json
from pathlib import Path

PATHS = {
    'axi_ad9361/axi_ad9361_rx_channel.v': (
        '  rf_rx_pn_pair_observer #(', '    .snapshot_up(rx_observer_snapshot_up));',
        'rx_observer_snapshot_up', (
            '  axi_ad9361_rx_pnmon', '.adc_pn_err (adc_pn_err_s)', '.adc_pn_oos (adc_pn_oos_s)',
            '.up_debug_read_data (rx_observer_snapshot_up)', '.DEBUG_READ_DATA_ENABLE (1)',
        ),
    ),
    'axi_ad9361/xilinx/axi_ad9361_lvds_if.v': (
        '  rf_rx_frame_window_observer i_rx_frame_observer (', '    .snapshot_up(rx_observer_up));',
        'rx_observer_up', ('adc_valid_p', 'adc_status_p', 'rx_locked', 'rx_frame_s'),
    ),
}


def trim(text, anchors):
    begin, end, signal, required = anchors
    if any(text.count(anchor) != 1 for anchor in (begin, end)):
        raise ValueError(f'Ambiguous read-only snapshot {signal}')
    first = text.index(begin)
    last = text.index(end, first) + len(end)
    replacement = f"  // Extra diagnostic snapshot omitted from the forward receiver.\n  assign {signal} = 32'd0;"
    reduced = text[:first] + replacement + text[last:]
    unchanged = text[:first] + text[last:]
    if any(reduced.count(anchor) != unchanged.count(anchor) or anchor not in unchanged for anchor in required):
        raise ValueError(f'Functional datapath or register context changed around {signal}')
    return reduced


def stage(platform, baseline):
    lib = Path('sources/vendor/library')
    result = {}
    for name, anchors in PATHS.items():
        relative = lib/name
        file = platform/relative
        original = file.read_bytes()
        if original != (baseline/relative).read_bytes():
            raise ValueError(f'R19 vendor source changed before trim {name}')
        replacement = trim(original.decode(), anchors).encode()
        file.write_bytes(replacement)
        result[str(relative)] = {'before_sha256': sha256(original).hexdigest(),
                                 'after_sha256': sha256(replacement).hexdigest()}
    info = platform/'source-inputs.json'
    manifest = json.loads(info.read_text())
    manifest['RF_optional_diagnostic_snapshots'] = {
        'enabled': False, 'changed_vendor_sources': result,
        'scope': 'Four PN pair debug snapshots and one LVDS frame-window snapshot return zero',
        'standard_PN_and_LVDS_datapath_preserved': True,
        'stage1_resource_credit_until_paired_synthesis': False,
    }
    info.write_text(json.dumps(manifest, indent=2)+'\n')
    return result
