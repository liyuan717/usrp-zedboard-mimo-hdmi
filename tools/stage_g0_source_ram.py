#!/usr/bin/env python3
"""Stage an already qualified finite SOURCE bit with the verified RAM kernel and DTB."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from zlib import crc32
from review_g0_source_route import critical_roots, scoped_cdc_equivalence

HWH = Path('project/receiver_v2.gen/sources_1/bd/receiver_v2/hw_handoff/receiver_v2.hwh')
BIT = Path('project/g0_source.bit')
ROUTE = Path('route-review.json')
DCP = Path('project/g0_source_review_routed.dcp')
FILES = ('hdmi_pipeline.bit', 'hdmi.dtb', 'zImage')
BIT_MARKER = 'G0_SOURCE_CANDIDATE_BIT_GENERATED_NOT_FULL_V3_ACCEPTANCE'


def hardware_handoff(platform):
    original = (platform / HWH).read_bytes()
    # Only the EDKSYSTEM generation timestamp may differ between candidates.
    stable, count = re.subn(rb'(?m)(^<EDKSYSTEM [^\r\n]*?) TIMESTAMP="[^"\r\n]*"',
                           rb'\1', original, count=1)
    if count != 1:
        raise ValueError('hardware handoff timestamp missing')
    return stable


def asset_record(name, data):
    return {'file': name, 'bytes': len(data), 'crc32': f'{crc32(data):08x}'}


def stage(platform, baseline, seed, bit_log, output):
    if output.exists():
        raise FileExistsError(output)
    route_raw = (platform / ROUTE).read_bytes()
    route = json.loads(route_raw)
    if not (route.get('finite_RX_only_G0_candidate_review_passed')
            and route.get('route_complete_no_errors')
            and route.get('typed_RF_HDMI_counter65_contracts_passed')
            and all(route[key] >= 0 for key in ('WNS_ns', 'WHS_ns', 'WPWS_ns'))):
        raise ValueError('finite SOURCE route review must pass before staging RAM assets')
    implementation_log = route.get('implementation_log')
    if implementation_log and sha256(Path(implementation_log['path']).read_bytes()).hexdigest() != implementation_log['sha256']:
        raise ValueError('implementation log differs from the finite SOURCE route review')
    cone = route['inherited_CDC_scoped_cone_equivalence']
    if cone is None and route.get('inherited_CDC_representative_rows_equal'):
        # The route review omits the expanded cone when the representative
        # rows match. The prebit query still captured both DCPs and the cones.
        candidate_cdc = critical_roots((platform / 'project/implementation_cdc.rpt').read_text())
        baseline_cdc = critical_roots((baseline / 'project/implementation_cdc.rpt').read_text())
        if candidate_cdc != baseline_cdc or candidate_cdc != route['inherited_CDC_critical_rows']:
            raise ValueError('CDC rows differ from the reviewed candidates')
        cone = scoped_cdc_equivalence(platform, baseline, candidate_cdc, baseline_cdc)
    if cone is None:
        raise ValueError('candidate checkpoint binding absent from route review')
    if sha256((platform / DCP).read_bytes()).hexdigest() != cone['candidate_DCP_sha256']:
        raise ValueError('routed checkpoint differs from reviewed candidate')
    handoff = hardware_handoff(platform)
    if handoff != hardware_handoff(baseline):
        raise ValueError('hardware handoff changed; revalidate the kernel and DTB')
    bit_lines = bit_log.read_text().splitlines()
    required_lines = {
        f'Command: open_checkpoint {(platform / DCP).resolve()}',
        f'Command: write_bitstream -force {(platform / BIT).resolve()}',
        'write_bitstream completed successfully',
        BIT_MARKER,
    }
    if not required_lines.issubset(bit_lines):
        raise ValueError('successful diagnostic bit generation log for this candidate required')
    seed_manifest = json.loads((seed / 'BOOT_MANIFEST.json').read_text())
    seed_proof = sha256((baseline / ROUTE).read_bytes()).hexdigest()
    if seed_manifest['routed_review_sha256'] != seed_proof:
        raise ValueError('seed RAM assets do not belong to the baseline route')
    seed_data = {name: (seed / name).read_bytes() for name in FILES}
    if seed_manifest['files'] != [asset_record(name, seed_data[name]) for name in FILES]:
        raise ValueError('seed RAM asset differs from its CRC/size record')
    if seed_data[FILES[0]] != (baseline / BIT).read_bytes():
        raise ValueError('seed bit differs from the baseline bit')
    bit = (platform / BIT).read_bytes()
    if not bit:
        raise ValueError('candidate bit is empty')
    data = {**seed_data, FILES[0]: bit}
    manifest = {
        'scope': 'Finite fixed8/2x2 SOURCE G0 RAM assets. Requires same-boot native validation.',
        'files': [asset_record(name, data[name]) for name in FILES],
        'routed_review_sha256': sha256(route_raw).hexdigest(),
        'baseline_routed_review_sha256': seed_proof,
        'hardware_handoff_without_timestamp_sha256': sha256(handoff).hexdigest(),
        'reused_verified_kernel_and_DTB': True,
        'hardware_or_SD_written': False,
        'full_production_IO_or_G0_passed': False,
    }
    output.mkdir()
    for name in FILES:
        (output / name).write_bytes(data[name])
    (output / 'BOOT_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('platform', 'baseline', 'seed', 'bit-log', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    print(json.dumps(stage(**vars(parser.parse_args())), indent=2))
