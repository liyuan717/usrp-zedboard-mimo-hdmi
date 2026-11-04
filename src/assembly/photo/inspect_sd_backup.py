"""Read the existing backup only; create sparse host copies, never open a device."""
from pathlib import Path
import hashlib
import json
import struct
import subprocess
import time

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[2] / 'rfv2-usrp-interop-20261029/build/interop/sd-backup'
receipt = json.loads((SOURCE / 'backup-receipt.json').read_text())
archive = SOURCE / 'original-sd-full.img.zst'
assert archive.resolve() == Path(receipt['compressed_file']).resolve()
out = HERE / 'sd-backup-inspection'
out.mkdir(exist_ok=True)
assert not (out / 'BACKUP-VERIFIED.json').exists()
started = time.monotonic()
process = subprocess.Popen(['/home/leeeo/miniforge3/bin/zstd', '-dc', str(archive)], stdout=subprocess.PIPE)
digest = hashlib.sha256()
position = 0
partitions = []
handles = []
allocated = 0
zero = bytes(65536)
try:
    while chunk := process.stdout.read(1024 * 1024):
        if position == 0:
            assert chunk[510:512] == b'\x55\xaa'
            for index in range(4):
                entry = chunk[446 + index * 16:462 + index * 16]
                kind = entry[4]
                first, sectors = struct.unpack_from('<II', entry, 8)
                if not kind or not sectors:
                    continue
                path = out / f'partition-{index + 1}.sparse.img'
                handle = path.open('xb')
                handle.truncate(sectors * 512)
                handles.append(handle)
                partitions.append(dict(number=index + 1, type=kind, start=first * 512,
                                       bytes=sectors * 512, host_sparse_path=str(path)))
            (out / 'PARTITIONS.json').write_text(json.dumps(partitions, indent=2) + '\n')
            print(json.dumps({'partitions': partitions}), flush=True)
        digest.update(chunk)
        for part, handle in zip(partitions, handles):
            left = max(position, part['start'])
            right = min(position + len(chunk), part['start'] + part['bytes'])
            if left >= right:
                continue
            data = chunk[left - position:right - position]
            for offset in range(0, len(data), len(zero)):
                piece = data[offset:offset + len(zero)]
                if piece != zero[:len(piece)]:
                    allocated += len(piece)
                    if allocated > 768 * 1024 * 1024:
                        raise RuntimeError('host sparse copy allocation budget exceeded')
                    handle.seek(left - part['start'] + offset)
                    handle.write(piece)
        position += len(chunk)
    assert process.wait() == 0
    assert position == receipt['backup_bytes'] == 31914983424
    assert digest.hexdigest() == receipt['raw_sha256']
    result = dict(verified=True, source=str(archive), decompressed_bytes=position,
                  raw_sha256=digest.hexdigest(), compressed_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                  elapsed_seconds=round(time.monotonic() - started, 3),
                  nonzero_copy_bytes=allocated, partitions=partitions,
                  SD_device_opened=False, SD_written=False)
    (out / 'BACKUP-VERIFIED.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)
finally:
    for handle in handles:
        handle.close()
    if process.poll() is None:
        process.terminate()
        process.wait()
