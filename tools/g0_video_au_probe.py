#!/usr/bin/env python3
"""Actual C bounded partial-AU adapter, byte oracle and host sanitizer tests; no RF signoff."""
import argparse,hashlib,json,os,struct,subprocess
from pathlib import Path
H=struct.Struct('<IIIIQIBBH');A=struct.Struct('<4sHHQQII')

def units(data):
 pos=0;out=[]
 while pos<len(data):
  h=list(H.unpack_from(data,pos));pos+=32;n=h[3];assert 0<n<=1048576
  out.append((h,data[pos:pos+n]));pos+=n
 assert pos==len(data);return out
def pack(rows):return b''.join(H.pack(*h)+p for h,p in rows)
def aus(data):
 pos=0;out=[]
 while pos<len(data):
  magic,version,size,identity,capture,n,damaged=A.unpack_from(data,pos);pos+=32
  assert (magic,version,size)==(b'V3AU',1,32) and n<=1048576 and damaged in (0,1)
  out.append((identity,capture,data[pos:pos+n],damaged));pos+=n
 assert pos==len(data);return out
def oracle(rows):
 groups=[]
 for h,p in rows:
  identity=(h[0]<<32)|h[2]
  if not groups or groups[-1][0]!=identity:groups.append([identity,h[4],[],False])
  groups[-1][2].append(p);groups[-1][3]|=h[6] in (1,5)
 return [(i,t,b''.join(p)) for i,t,p,vcl in groups if vcl]

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(exist_ok=False)
 ref=a.fragments.resolve();proof=json.loads((ref/'receipt.json').read_text());assert proof['host_ASan_UBSan_actual_C_passed']
 binaries={}
 for name,flags in [('normal',['-O2']),('sanitized',['-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer'])]:
  p=out/name;subprocess.run(['gcc','-std=c11','-Wall','-Wextra','-Werror',*flags,str(v/'ps/g0_video_au.c'),str(v/'ps/g0_video_au_probe.c'),'-o',str(p)],check=True);binaries[name]=p
 cases=[]
 def run(name,data,expected,check,drop=0):
  d=out/name;d.mkdir();(d/'input.units').write_bytes(data);rows=[]
  for label,binary in binaries.items():
   env={**os.environ,'ASAN_OPTIONS':'detect_leaks=1:abort_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'}
   r=subprocess.run([str(binary),str(d/'input.units'),str(d/(label+'.aus')),str(drop)],capture_output=True,text=True,env=env)
   (d/(label+'.stdout')).write_text(r.stdout);(d/(label+'.stderr')).write_text(r.stderr);assert r.returncode==0,(name,label,r.stderr)
   actual=aus((d/(label+'.aus')).read_bytes());assert [(i,t,p) for i,t,p,_ in actual]==expected,(name,label,len(actual),len(expected))
   stats=json.loads(r.stdout);assert stats['peak_bytes']<=1048576;check(stats,actual)
   rows.append(dict(name=label,stats=stats,AU_count=len(actual),output_sha256=hashlib.sha256((d/(label+'.aus')).read_bytes()).hexdigest()))
  cases.append(dict(name=name,probe_drop_AUs=drop,results=rows))
 for name in ['ordered_clean_reordered','ordered_loss_continue','ordered_wrap','ordered_output_busy_continue','ordered_session_reentry']:
  data=(ref/name/'normal.units').read_bytes();u=units(data)
  def check(s,r,name=name):
   assert s['invalid']==s['stale']==s['overflow']==0
   if name=='ordered_loss_continue':assert s['damaged']>0 and any((i&0xffffffff)==12 for i,*_ in r) and (r[-1][0]&0xffffffff)==199
  run(name,data,oracle(u),check)
 clean=units((ref/'ordered_clean_reordered/normal.units').read_bytes())
 no_end=[([*h[:7],h[7]&~2,*h[8:]] if h[2]==12 else h[:],p) for h,p in clean]
 run('missing_AU_end_continue',pack(no_end),oracle(no_end),lambda s,r:s['damaged']>0 or (_ for _ in ()).throw(AssertionError('partial flag')))
 # A fully lost AU and a missing tail must not mark the next explicit AU start.
 boundary_gap=[(h,p) for h,p in clean if h[2]!=10 and not (h[2]==12 and h[7]&2)]
 def check_boundary_gap(stats, rows):
  by_frame={identity&0xffffffff:damaged for identity,_,_,damaged in rows}
  assert 10 not in by_frame and by_frame[11]==0 and by_frame[12]==1 and by_frame[13]==0
  assert stats['damaged']==1
 run('unit_gap_at_explicit_AU_boundary',pack(boundary_gap),oracle(boundary_gap),check_boundary_gap)
 damaged=[(h[:],(b'\x07'+p[1:]) if j==0 else p) for j,(h,p) in enumerate(clean)]
 run('bad_NAL_header_continue',pack(damaged),oracle(damaged[1:]),lambda s,r:s['invalid']==1 and (r[-1][0]&0xffffffff)==199 or (_ for _ in ()).throw(AssertionError('invalid NAL recovery')))
 # Large legal NAL followed by another exceeds the AU cap. Drop this local
 # frame; a later ordinary frame is still submitted, with no unbounded copy.
 sid=clean[0][0][0];big=b'\x00\x00\x00\x01\x61'+bytes(1048570)
 stress=[([sid,0,0,len(big),0,50000,1,1,0],big),([sid,1,0,10,0,50000,1,2,0],b'\0\0\0\1\x61'+bytes(5))]
 later=[]
 for j,(h,p) in enumerate(row for row in clean if row[0][2]==1):h=h[:];h[1]=100+j;later.append((h,p))
 run('AU_cap_then_later_frame',pack(stress+later),oracle(later),lambda s,r:s['overflow']==1 and len(r)==1 or (_ for _ in ()).throw(AssertionError('AU cap/recovery')))
 run('AU_output_busy_continue',pack(clean),oracle(clean)[5:],lambda s,r:s['output_dropped']==5 and s['submitted']==200 and (r[-1][0]&0xffffffff)==199 or (_ for _ in ()).throw(AssertionError('AU output pressure')),5)
 result=dict(scope=__doc__,host_actual_C_ASan_UBSan_passed=True,cases=cases,PL_added=0,additional_PS_fixed_AU_cap_bytes=1048576,decoder_padding_bytes=64,
             source_sha256={n:hashlib.sha256((v/n).read_bytes()).hexdigest() for n in ['ps/include/g0_video_au.h','ps/g0_video_au.c','ps/g0_video_au_probe.c']},whole_AU_completeness_required=False,native_decoder_or_RF_or_G0_passed=False)
 (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({'cases':len(cases),'passed':True}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--fragments',type=Path,required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
