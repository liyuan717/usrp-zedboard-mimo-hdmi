#!/usr/bin/env python3
"""Exercise actual C bounded slice reassembly with real video, local loss and cache pressure."""
import argparse,hashlib,json,os,random,struct,subprocess
from pathlib import Path
from codec_sweep import annexb_units,access_units
E=struct.Struct('<QQIIII');H=struct.Struct('<IIIIQIBBH');VIDEO=struct.Struct('<IQIIIHHHBBII')
SID=0x56334601

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
 proof=json.loads((a.symbols/'receipt.json').read_text());assert proof['clean_roundtrip_original_AnnexB_byteexact'] and proof['all200_packet_PTS_and_durations_exact20fps']
 packed=(a.symbols/'video-source-symbols.bin').read_bytes();symbols=[packed[i:i+512] for i in range(0,len(packed),512)];raw=(v/proof['input']).read_bytes()
 gold={};i=0
 for f,au in enumerate(access_units(annexb_units(raw))):
  for kind,data in au:gold[i]=(f,f*50000,kind,data);i+=1
 units={}
 for p in symbols:
  if any(p):units.setdefault(VIDEO.unpack_from(p)[2],[]).append(p)
 assert len(gold)==len(units)
 binaries={}
 for name,flags in [('normal',['-O2']),('sanitized',['-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer'])]:
  binary=out/('probe-'+name);subprocess.run(['gcc','-std=c11','-Wall','-Wextra','-Werror',*flags,str(v/'ps/g0_video_fragment.c'),str(v/'ps/g0_video_fragment_probe.c'),'-o',str(binary)],check=True);binaries[name]=binary
 class Events:
  def __init__(self):self.rows=[];self.local=0;self.source=0;self.session=SID;self.session_to(SID)
  def row(self,command,payload=bytes(512),session=None):
   assert len(payload)==512
   self.rows.append(E.pack(self.local,self.source,self.session if session is None else session,command,1,0)+payload)
  def session_to(self,session):self.session=session;self.source=0;self.row(2)
  def push(self,p,session=None):
   capture=VIDEO.unpack_from(p)[1] if any(p) else self.source
   self.local=max(self.local+10,capture);self.source=max(self.source,capture);self.row(0,p,session)
  def tick(self,delta):self.local+=delta;self.source+=delta;self.row(1)
  def drop(self,n):self.row(3,struct.pack('<I',n)+bytes(508))
 def decode(path):
  data=path.read_bytes();pos=0;records={};order=[]
  while pos<len(data):
   assert len(data)-pos>=32
   s,u,f,n,t,duration,kind,flags,reserved=H.unpack_from(data,pos);pos+=32
   assert n<=1048576 and pos+n<=len(data) and not reserved and duration==50000 and u not in records
   records[u]=(f,t,kind,data[pos:pos+n]);order.append(u);pos+=n
  return records,order
 def run(name,ev,expected,checks,ordered=False):
  folder=out/name;folder.mkdir();events=b''.join(ev.rows);(folder/'events.bin').write_bytes(events)
  results={}
  for label,binary in binaries.items():
   env={**os.environ,'ASAN_OPTIONS':'detect_leaks=1:abort_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'}
   r=subprocess.run([str(binary),str(folder/'events.bin'),str(folder/(label+'.units'))],capture_output=True,text=True,env=env)
   (folder/(label+'.stdout')).write_text(r.stdout);(folder/(label+'.stderr')).write_text(r.stderr)
   assert r.returncode==0,(name,label,r.returncode,r.stderr[-1800:])
   stats=json.loads(r.stdout);actual,order=decode(folder/(label+'.units'));assert actual==expected,(name,label,len(actual),len(expected),set(actual)^set(expected))
   if ordered:assert order==list(expected),(name,label,'NAL delivery order differs')
   assert stats['current_bytes']==stats['current_units']==stats['alloc_failures']==0
   assert stats['peak_bytes']<=2097152 and stats['peak_units']<=32 and stats['units_delivered']==len(expected)
   checks(stats,actual)
   results[label]={'stats':stats,'exact_units':len(actual),'delivered_order':order,'payload_sha256':hashlib.sha256((folder/(label+'.units')).read_bytes()).hexdigest()}
  rows.append({'name':name,'events':len(ev.rows),'ordered_delivery':ordered,'results':results})
 rows=[]
 ev=Events()
 for p in symbols:ev.push(p)
 run('clean',ev,gold,lambda s,a:None)
 # Bad CRC is an erasure upstream: omit one fragment, never send corrupt payload.
 target=next(u for u,(f,t,k,d) in gold.items() if f==12 and k==1 and len(d)>472)
 missing={target}|{u for u,(f,*_) in gold.items() if f==40}
 data=[p for p in symbols if any(p) and VIDEO.unpack_from(p)[0]!=40 and not(VIDEO.unpack_from(p)[2]==target and VIDEO.unpack_from(p)[6]==1)]
 rng=random.Random(5633);reordered=[]
 for start in range(0,len(data),32):
  chunk=data[start:start+32];rng.shuffle(chunk);reordered.extend(chunk)
 ev=Events()
 for j,p in enumerate(reordered):
  ev.push(p)
  if j%11==0:ev.push(p)
 # Necessary malformed-field checks remain local and cannot block later content.
 for offset,fmt,value in [(16,'<I',0xffffffff),(24,'<H',473),(28,'<H',65535),(36,'<I',1)]:
  p=bytearray(symbols[-9]);struct.pack_into(fmt,p,offset,value);ev.push(bytes(p))
 def loss_checks(s,a):
  assert s['duplicate_fragments']>0 and s['invalid_fragments']==4 and s['expired_units']>=1
  assert any(f==12 for f,*_ in a.values()) and any(f==199 for f,*_ in a.values())
 run('loss_reorder_duplicates_invalid_continue',ev,{u:g for u,g in gold.items() if u not in missing},loss_checks)
 # An incomplete old unit times out even if no subsequent RF data arrives.
 target=next(u for u,g in gold.items() if len(units[u])>2);ev=Events();ev.push(units[target][0]);ev.tick(500001)
 for p in units[target][1:]:ev.push(p)
 later={u:g for u,g in gold.items() if g[0]>=195}
 for u in later:
  for p in units[u]:ev.push(p)
 run('idle_expiry_then_later_units',ev,later,lambda s,a: s['expired_units']>=1 or (_ for _ in ()).throw(AssertionError('expiry absent')))
 # Exercise both32unit and2MiB allocation bounds, then change admitted session.
 ev=Events()
 for j in range(40):
  p=VIDEO.pack(0,0,1000+j,473,0,472,0,2,1,0,50000,0)+bytes(472);ev.push(p)
 for j in range(4):
  p=VIDEO.pack(0,0,1100+j,1048576,0,472,0,2222,1,0,50000,0)+bytes(472);ev.push(p)
 ev.session_to(SID+1);ev.push(symbols[0],session=SID)
 for p in symbols:ev.push(p)
 def pressure_checks(s,a):assert s['evicted_units']>0 and s['peak_units']==32 and s['stale_fragments']>=1 and s['session_changes']==2
 run('bounded_pressure_session_reentry',ev,gold,pressure_checks)
 ev=Events();ev.drop(5)
 for p in symbols:ev.push(p)
 def output_checks(s,a):assert s['output_dropped']==5 and s['units_completed']==len(gold) and max(g[0] for g in a.values())==199
 run('busy_output_drops_continue',ev,{u:g for u,g in gold.items() if u>=5},output_checks)
 # Unit and frame IDs wrap32bits; monotonically increasing capture PTS does not wrap.
 ev=Events();wrapped={}
 for p in symbols:
  if not any(p):ev.push(p);continue
  p=bytearray(p);f,t,u,*_=VIDEO.unpack_from(p);uid=(0xfffffff0+u)&0xffffffff;fid=(0xfffffff0+f)&0xffffffff;struct.pack_into('<I',p,0,fid);struct.pack_into('<I',p,12,uid);ev.push(bytes(p));g=gold[u];wrapped[uid]=(fid,*g[1:])
 ev.push(symbols[0]) # Outside bounded old history: safely stale, not redelivered.
 run('ID_wrap_bounded_old_data',ev,wrapped,lambda s,a: s['stale_fragments']>=1 or (_ for _ in ()).throw(AssertionError('old data not dropped')))
 # Same cache now holds complete NALs until bounded serial-ID delivery. No
 # duplicate NAL queue or whole-AU completion condition is introduced.
 def ordered_events():
  ev=Events();ev.row(4);return ev
 data=[p for p in symbols if any(p)];rng=random.Random(0xa011);ordered=[]
 for start in range(0,len(data),32):
  chunk=data[start:start+32];rng.shuffle(chunk);ordered.extend(chunk)
 ev=ordered_events()
 for p in ordered:ev.push(p)
 run('ordered_clean_reordered',ev,gold,lambda s,a:None,True)
 ordered_target=next(u for u,(f,t,k,d) in gold.items() if f==12 and k==1 and len(d)>472)
 ev=ordered_events()
 for j,p in enumerate(ordered):
  f,t,u,total,offset,length,index,*_=VIDEO.unpack_from(p)
  if f==40 or (u==ordered_target and index==1):continue
  ev.push(p)
  if j%11==0:ev.push(p)
 ordered_missing={ordered_target}|{u for u,(f,*_) in gold.items() if f==40}
 run('ordered_loss_continue',ev,{u:g for u,g in gold.items() if u not in ordered_missing},
     lambda s,a: s['ordered_skipped_units']>=len(ordered_missing) or (_ for _ in ()).throw(AssertionError('missing IDs not skipped')),True)
 ev=ordered_events();new_wrap={}
 for p in symbols:
  if not any(p):ev.push(p);continue
  p=bytearray(p);f,t,u,*_=VIDEO.unpack_from(p);uid=(0xfffffff0+u)&0xffffffff;fid=(0xfffffff0+f)&0xffffffff
  struct.pack_into('<I',p,0,fid);struct.pack_into('<I',p,12,uid);ev.push(bytes(p));g=gold[u];new_wrap[uid]=(fid,*g[1:])
 run('ordered_wrap',ev,new_wrap,lambda s,a:None,True)
 partial=next(u for u in units if len(units[u])>2);following=partial+1
 ev=ordered_events();ev.push(units[partial][0])
 for p in units[following]:ev.push(p)
 ev.tick(500001)
 run('ordered_idle_partial_expiry',ev,{following:gold[following]},
     lambda s,a: s['expired_units']==1 and s['ordered_skipped_units']==1 or (_ for _ in ()).throw(AssertionError('idle known-hole recovery')),True)
 # A useful complete later NAL must proceed after bounded reorder. Repair
 # arriving after that deadline stays stale, rather than resurrecting the gap.
 ev=ordered_events();ev.push(units[partial][0])
 for p in units[following]:ev.push(p)
 ev.tick(200001)
 for p in units[partial][1:]:ev.push(p)
 run('ordered_partial_HOL_deadline_late_drop',ev,{following:gold[following]},
     lambda s,a: s['expired_units']==1 and s['ordered_skipped_units']==1 and s['stale_fragments']>0 or (_ for _ in ()).throw(AssertionError('known partial blocked useful later NAL')),True)
 ev=ordered_events();ev.push(units[partial][0])
 for p in units[following]:ev.push(p)
 ev.tick(100000)
 for p in units[partial][1:]:ev.push(p)
 run('ordered_partial_repaired_before_HOL_deadline',ev,{u:gold[u] for u in (partial,following)},
     lambda s,a: s['expired_units']==0 or (_ for _ in ()).throw(AssertionError('premature partial drop')),True)
 ev=ordered_events();ev.push(units[partial][0]);ev.tick(200001)
 for p in units[partial][1:]:ev.push(p)
 run('ordered_partial_no_later_complete_retains_idle_TTL',ev,{partial:gold[partial]},
     lambda s,a: s['expired_units']==0 or (_ for _ in ()).throw(AssertionError('idle partial TTL shortened without useful later content')),True)
 ev=ordered_events();ev.drop(5)
 for p in symbols:ev.push(p)
 run('ordered_output_busy_continue',ev,{u:g for u,g in gold.items() if u>=5},output_checks,True)
 ev=ordered_events();ev.push(units[partial][0]);ev.session_to(SID+1)
 for p in symbols:ev.push(p)
 run('ordered_session_reentry',ev,gold,lambda s,a: s['session_changes']==2 or (_ for _ in ()).throw(AssertionError('session reset')),True)
 ev=ordered_events()
 for u in (0,2):
  for p in units[u]:ev.push(p)
 ev.tick(200001)
 for p in units[1]:ev.push(p)
 run('ordered_unknown_hole_deadline_late_drop',ev,{u:gold[u] for u in (0,2)},
     lambda s,a: s['ordered_skipped_units']==1 and s['stale_fragments']>=1 or (_ for _ in ()).throw(AssertionError('unknown-hole deadline/late fragment')),True)
 result={'scope':__doc__,'host_ASan_UBSan_actual_C_passed':True,'scenarios':rows,'source_sha256':{n:hashlib.sha256((v/n).read_bytes()).hexdigest() for n in ['ps/include/g0_video_fragment.h','ps/g0_video_fragment.c','ps/g0_video_fragment_probe.c']},'fixed_cache_limit_bytes':2097152,'fixed_unit_limit':32,'whole_AU_completeness_gate':False,'whole_Rf_or_native_ARM_or_decoder_recovery_or_G0_passed':False,'new_PL_resources':0}
 (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps({'scenarios':len(rows),'ASan_UBSan':True,'whole_AU_gate':False,'native_ARM_or_RF':False}))
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--symbols',type=Path,required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
