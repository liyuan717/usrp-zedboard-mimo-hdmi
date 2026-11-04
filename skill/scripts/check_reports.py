#!/usr/bin/env python3
import argparse,json,re
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--utilization',type=Path,required=True);p.add_argument('--timing',type=Path,required=True);a=p.parse_args();u=a.utilization.read_text();t=a.timing.read_text();rows={}
for label in ['Slice LUTs','Slice Registers','Block RAM Tile','DSPs']:
 m=re.search(r'\|\s*'+re.escape(label)+r'\*?\s*\|([^\n]+)',u)
 if not m:raise SystemExit('missing resource: '+label)
 cols=[x.strip() for x in m[1].split('|') if x.strip()];rows[label]=cols
m=re.search(r'WNS\(ns\).*?\n\s*[- ]+\n\s*([\d.-]+)\s+([\d.-]+)\s+\d+\s+\d+\s+([\d.-]+)\s+([\d.-]+)',t,re.S)
if not m:raise SystemExit('missing design timing summary')
wns,tns,whs,ths=map(float,m.groups())
if wns<0 or whs<0 or tns!=0 or ths!=0:raise SystemExit('timing not closed')
print(json.dumps(dict(resources=rows,wns_ns=wns,tns_ns=tns,whs_ns=whs,ths_ns=ths,scope='Reports only; no RF throughput or visual inference'),indent=2))
