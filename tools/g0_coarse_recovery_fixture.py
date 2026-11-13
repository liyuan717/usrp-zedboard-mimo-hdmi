#!/usr/bin/env python3
"""Synthetic false-coarse/invalid-training then fresh measured RF; no truth injected into DUT config."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from g0_fixed_differential_model import FixedDifferentialSelector
from g0_packet_probe import public_prefix


def main(a):
    out=a.output.resolve();out.mkdir(exist_ok=False)
    dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))])
    real=np.fromfile(a.capture,dtype=dtype)
    assert len(real)==2097152 and np.all(np.diff(real['tag'])==1)
    # Select a public candidate independently; this location only chooses
    # bytes for the synthetic stimulus, never a search/configuration tag.
    lanes=real['iq'][:500000].astype(float)
    lanes=np.rint(lanes-lanes[:80000].mean(axis=0))
    y=lanes[:,0::2]+1j*lanes[:,1::2];y[:82048]=0
    selected=FixedDifferentialSelector(y,public_prefix()[256:384,0],hit_denominator=10).next_candidate(82052)
    assert selected
    k=selected['selected_start_index'];prefix=160000;fake=110000;copied=540;retained=800000
    rng=np.random.default_rng(0xc0a5e)
    raw=np.zeros(prefix+retained,dtype=dtype)
    raw['iq'][:prefix]=np.rint(rng.normal(0,45,(prefix,4))).astype('<i2')
    raw['iq'][fake:fake+copied]=real['iq'][k:k+copied]
    # Training for the synthetic public candidate is unrelated bounded noise.
    # Later real samples are unchanged; there is no receiver reset or fence.
    raw['iq'][prefix:]=real['iq'][:retained]
    raw['tag']=(np.uint64(7)<<np.uint64(48))+np.uint64(0x20000000)+np.arange(len(raw),dtype='<u8')
    raw.tofile(out/'adc.raw')
    result=dict(scope=__doc__,synthetic_not_an_unchanged_RF_capture=True,
                real_capture_sha256=hashlib.sha256(a.capture.read_bytes()).hexdigest(),
                fixture_sha256=hashlib.sha256(raw.tobytes()).hexdigest(),records=len(raw),
                fake_public_candidate_start=fake,copied_public_samples=copied,
                unrelated_training_after_fake=True,unchanged_real_prefix_starts_at=prefix,
                real_prefix_records=retained,no_DUT_peak_CFO_or_session_config=True,
                receiver_reset_between_segments=False,fullG0_or_recovery_proven=False)
    (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--capture',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    main(p.parse_args())
