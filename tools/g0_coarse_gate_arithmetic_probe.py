#!/usr/bin/env python3
"""Exact scalar arithmetic/stall and compiled gate-fault controls, not RF acceptance."""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import random
import subprocess
from differential_frontend_cost_probe import SCORE_TB
from g0_coarse_gate_implementation_probe import gate10


def fields(items):
    value = 0
    for x, width in items:
        assert -(1 << (width-1)) <= x < (1 << width)
        value = (value << width) | (x & ((1 << width)-1))
    return value


def main(a):
    out = a.output.resolve()
    out.mkdir(exist_ok=False)
    original = a.baseline.read_text()
    candidate = gate10(original)
    rows = []
    d = 1979168
    # Strict equality, the two neighboring denominators, zero energy and
    # full signed49 rails are checked without the candidate's hit function.
    for sense in (0,1):
        for e in (0,10*d-1,10*d,10*d+1,5*d-1,5*d,5*d+1):
            c = [0]*8
            c[4*sense] = d
            rows.append((c,e))
    rails = (-(1<<48),-(1<<48)+1,-1,0,1,(1<<48)-1)
    for x,y in itertools.product(rails,repeat=2):
        for e in (0,1,(1<<70)-1):
            rows.append(([x,y,y,x,x,x,y,y],e))
    rng = random.Random(0x100c0a)
    for _ in range(512):
        rows.append(([rng.randrange(-(1<<48),1<<48) for _ in range(8)],rng.randrange(1<<70)))
    inputs,gold = [],[]
    for k,(c,e) in enumerate(rows):
        n = [sum(v*v for v in c[s*4:(s+1)*4]) for s in (0,1)]
        sense = int(n[1]>n[0]); selected = c[4*sense:4*sense+4]
        rx = int(sum(v*v for v in selected[2:])>sum(v*v for v in selected[:2]))
        re,im = selected[2*rx:2*rx+2]
        tag = (0x99aa<<48)|((2**48-80+k)%(2**48)); gen=(0xfffffff0+k)%(2**32)
        inputs.append(fields([(gen,32),(tag,64),(e,70),(fields([(v,49) for v in reversed(c)]),392)]))
        gold.append(fields([(n[0],100),(n[1],100),(n[sense],100),(e,70),(tag,64),
                            (gen,32),(sense,1),(rx,1),(int(e!=0 and 10*n[sense]>d*e),1),(re,49),(im,49)]))
    receipt = dict(scope=__doc__, vectors=len(rows), cases=[],
                   oracle='independent Python integer sum-of-four-squares, strict10N>D*E, sense0/RX0 ties',
                   G0_or_full_receiver_or_RF_passed=False)
    for label,rtl in [('candidate',candidate),('old_gate_fault',original),
                      ('always_hit_fault',candidate.replace('out_hit<=energy!=0 && five_n>{{11{1\'b0}},denominator};',"out_hit<=1'b1;"))]:
        folder=out/label;folder.mkdir()
        assert label!='always_hit_fault' or rtl!=candidate
        (folder/'score.sv').write_text(rtl)
        (folder/'score_tb.sv').write_text(SCORE_TB.replace('@LAST@',str(len(rows)-1)).replace('@COUNT@',str(len(rows))))
        (folder/'score-input.hex').write_text(''.join(f'{v:0140x}\n' for v in inputs))
        (folder/'score-gold.hex').write_text(''.join(f'{v:0142x}\n' for v in gold))
        bp=a.vivado_bin.resolve()
        for step,args in [('compile',[bp/'xvlog','--sv','score.sv','score_tb.sv']),
                          ('elaborate',[bp/'xelab','score_tb','-s','score_tb','-mt','off']),
                          ('simulate',[bp/'xsim','score_tb','-runall'])]:
            with (folder/(step+'.log')).open('w') as log:
                r=subprocess.run([str(x) for x in args],cwd=folder,stdout=log,stderr=subprocess.STDOUT)
            if step!='simulate':assert r.returncode==0
        log=(folder/'simulate.log').read_text()
        if label=='candidate':
            assert 'G0_DIFFERENTIAL_SCORE_PASS' in log and 'Fatal:' not in log
            witness=next(x for x in log.splitlines() if x.startswith('G0_DIFFERENTIAL_SCORE_PASS'))
        else:
            assert 'Fatal: score Python exact oracle' in log and 'G0_DIFFERENTIAL_SCORE_PASS' not in log
            witness=next(x for x in log.splitlines() if 'Fatal: score Python exact oracle' in x)
        receipt['cases'].append(dict(name=label,compiled=True,RTL_sha256=hashlib.sha256(rtl.encode()).hexdigest(),witness=witness))
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('baseline','output','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
    main(p.parse_args())
