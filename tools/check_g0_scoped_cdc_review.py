#!/usr/bin/env python3
"""Negative controls for scoped CDC evidence; no hardware or netlist changes."""
import argparse,copy,hashlib,json
from pathlib import Path
from review_g0_source_route import scoped_cdc_equivalence,critical_roots

def main(a):
    out=a.output.resolve();out.mkdir(exist_ok=False)
    platform=a.platform.resolve();baseline=a.baseline.resolve()
    cdc=critical_roots((platform/'project/implementation_cdc.rpt').read_text())
    old=critical_roots((baseline/'project/implementation_cdc.rpt').read_text())
    assert cdc!=old
    rows=[]
    for name in ['normal_representative_change','unknown_source','business_destination','new_CDC_kind','extra_critical_row',
                 'missing_fanin_root','extra_fanin_root','fault_to_business_D','stale_DCP_digest','stale_TSV_digest']:
        d=out/name;d.mkdir();(d/'project').mkdir()
        (d/'project/g0_source_review_routed.dcp').symlink_to(platform/'project/g0_source_review_routed.dcp')
        (d/'sources').symlink_to(platform/'sources',target_is_directory=True)
        (d/'prebit-review.log').symlink_to(platform/'prebit-review.log')
        text=(platform/'fault-cones.tsv').read_text();meta=json.loads((platform/'fault-cones.meta.json').read_text())
        r=copy.deepcopy(cdc)
        if name=='unknown_source':r[0][1]='receiver_v2_i/business/unknown_reg/C'
        if name=='business_destination':r[0][2]=r[0][2].replace('/CLR','/D')
        if name=='new_CDC_kind':r[0][0]='CDC-1'
        if name=='extra_critical_row':r.append(r[0])
        if name=='missing_fanin_root':
            lines=text.splitlines();fields=lines[0].split('\t');fields.pop();lines[0]='\t'.join(fields);text='\n'.join(lines)+'\n'
        if name=='extra_fanin_root':
            lines=text.splitlines();lines[0]+='\treceiver_v2_i/business/unsafe_reg/C';text='\n'.join(lines)+'\n'
        if name=='fault_to_business_D':
            lines=text.splitlines();i=next(i for i,s in enumerate(lines) if '\tfanout\t' in s)
            lines[i]+='\treceiver_v2_i/business/unsafe_reg/D';text='\n'.join(lines)+'\n'
        if name=='stale_TSV_digest':text=text.replace('candidate\tfanin','candidate\tfanout',1)
        else:meta['proof_TSV_sha256']=hashlib.sha256(text.encode()).hexdigest()
        if name=='stale_DCP_digest':meta['candidate_DCP_sha256']='0'*64
        (d/'fault-cones.tsv').write_text(text);(d/'fault-cones.meta.json').write_text(json.dumps(meta))
        passed=True
        try:scoped_cdc_equivalence(d,baseline,r,old)
        except (AssertionError,KeyError,ValueError):passed=False
        assert passed==(name=='normal_representative_change'),name
        rows.append(dict(name=name,accepted=passed,expected_result_observed=True))
    result=dict(scope=__doc__,completed=True,controls=rows,no_new_exceptions_or_waivers=True,
                actual_R7_R11_DCPs_read_only=True,RF_started=False,full_IO_CDC_production_or_G0_passed=False)
    (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
    print('SCOPED_CDC_CONTROLS_PASS',len(rows))
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['platform','baseline','output']:p.add_argument('--'+n,type=Path,required=True)
    main(p.parse_args())
