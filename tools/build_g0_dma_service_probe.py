#!/usr/bin/env python3
"""Build an isolated R22 DMA service trace. Production sources stay unchanged."""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import build_g0_stream_decode
from workspace_storage_guard import check


def replace_once(path, before, after):
    text = path.read_text()
    if text.count(before) != 1:
        raise ValueError(f'Unexpected receiver structure in {path.name}')
    path.write_text(text.replace(before, after))


def main(args):
    build_g0_stream_decode.main(args)
    repo = Path(__file__).resolve().parents[1]
    out = args.output.resolve()
    source = out / 'sources'
    header = source / 'ps/include/g0_source_service_trace.hpp'
    shutil.copyfile(repo / 'tests/g0_source_service_trace.hpp', header)
    dma = source / 'ps/include/g0_source_dma_ingress.hpp'
    decoder = source / 'ps/g0_stream_decode.cpp'
    replace_once(dma, '#include "g0_au_decode_queue.hpp"',
                 '#include "g0_au_decode_queue.hpp"\n#include "g0_source_service_trace.hpp"')
    replace_once(dma, 'struct SourceDmaStats {', 'struct SourceDmaStats {\n G0SourceServiceTrace trace;')
    replace_once(dma, 'stats.started=owner.started=true;', 'stats.started=owner.started=true;stats.trace.start();')
    replace_once(dma, '   ++stats.acquired;\n   tick(fd);',
                 '   ++stats.acquired;\n   stats.trace.released(fd,desc.sequence);\n   stats.trace.processing_begin();\n   tick(fd);')
    replace_once(dma, '   (void)v3b_consume(&stats.parser,saved.data(),saved.size(),output,opaque);',
                 '   (void)v3b_consume(&stats.parser,saved.data(),saved.size(),output,opaque);\n   stats.trace.processing_end();')
    replace_once(decoder, '   <<",\\\"SOURCE_DMA_private_copied_bytes\\\":"<<input.dma.copied_bytes',
                 '   <<",\\\"diagnostic_DMA_service\\\":"<<input.dma.trace.json()\n'
                 '   <<",\\\"SOURCE_DMA_private_copied_bytes\\\":"<<input.dma.copied_bytes')
    if args.pause_once_us:
        replace_once(dma, '   stats.trace.released(fd,desc.sequence);',
                     f'   if(stats.acquired==6000)usleep({args.pause_once_us});\n'
                     '   stats.trace.released(fd,desc.sequence);')
    commands = json.loads((out / 'commands.json').read_text())
    try:
        for index in (0, len(commands)-1):
            with (out / f'trace-build-{index}.log').open('w') as log:
                subprocess.run(commands[index], stdout=log, stderr=subprocess.STDOUT, check=True)
        receipt = json.loads((out / 'receipt.json').read_text())
        receipt['source_sha256'].update({str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in (dma, decoder, header)})
        receipt['diagnostic_DMA_service_trace'] = True
        receipt['normal_receiver_source_modified'] = False
        receipt['controlled_service_pause'] = dict(after_acquired_batch=6000, requested_us=args.pause_once_us) if args.pause_once_us else None
        (out / 'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    finally:
        (out / 'disk-after-trace-build.json').write_text(json.dumps(check('after-DMA-service-trace-build'), indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pause-once-us', type=int, choices=(25000, 100000), default=0,
                        help='finite positive control, pause ingress once after batch 6000')
    main(parser.parse_args())
