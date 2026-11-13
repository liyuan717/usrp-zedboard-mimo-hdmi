#!/usr/bin/env python3
"""Independent finite wire/CRC/RS/video conservation and legacy IQ regression.
No RF device, native timing or PL throughput claim.
"""
import argparse,hashlib,json,struct,subprocess,zlib
from pathlib import Path
from g0_video_air_stream import VideoSchedule,preflight
from g0_fec_session_waveform import frame_iq,public_prefix,rrc,PERIOD,SPS,RATE
from g0_packet_probe import DATA_INDICES,PACKET_BYTES
from g0_packet_fec import recover
from workspace_storage_guard import check,GIB

def main(a):
 out=a.output.resolve();out.mkdir(exist_ok=False);v=Path(__file__).resolve().parents[1]
 (out/'disk-before.json').write_text(json.dumps(check('before-video-wire-controls'),indent=2)+'\n')
 result={'completed':False,'RF_started':False,'PL_or_RF_video_passed':False}
 try:
  commit=subprocess.check_output(['git','rev-parse','f6399e4'],cwd=v,text=True).strip()
  old=subprocess.check_output(['git','show',commit+':video_radio_v3/tools/g0_fec_session_waveform.py'],cwd=v)
  legacy={'__name__':'legacy_waveform_control'};exec(compile(old,'legacy-waveform','exec'),legacy)
  prefix=public_prefix();h=rrc();regression=[]
  for erase in [0,12]:
   old_cache={};new_cache={};digest=hashlib.sha256()
   for sf in range(48):
    left,_=legacy['frame_iq'](0x56334601,sf,old_cache,erase,prefix,h)
    right,_=frame_iq(0x56334601,sf,new_cache,erase,prefix,h)
    assert left.tobytes()==right.tobytes();digest.update(right.tobytes())
   regression.append({'periods':48,'source_CRC_erasures_per_block':erase,'all_float32_IQ_bytes_equal':True,'IQ_sha256':digest.hexdigest()})
  raw=a.symbols.read_bytes();assert len(raw)<=64*1024*1024
  schedule=VideoSchedule(a.symbols,0x56334601,a.availability);plan=schedule.plan;opened={};eager_opened={};ends={};declared=None;serial=None
  received={};current=0;expected_index=0;digest=hashlib.sha256();tx_counts=[0,0];tx_hash=[hashlib.sha256(),hashlib.sha256()]
  descriptors=controls=erased=0;frame_last={}
  for sf in range(plan['periods']):
   for b in range(schedule.block,min(schedule.block+8,plan['source_blocks'])):eager_opened.setdefault(b,sf*20000)
   wires,_=schedule.period(sf)
   if sf<8:assert all(w[5]==5 for w in wires),'initial160ms must not declare/age banks or send video'
   for slot,wire in enumerate(wires):
    assert len(wire)==548 and zlib.crc32(wire[:-4])==struct.unpack_from('<I',wire,544)[0]
    magic,major,kind,flags,sid,frame,seq,hint,block,index,size=struct.unpack_from('<4sBBHIIIIIHH',wire)
    assert (magic,major,flags,sid,frame,seq,hint,size)==(b'VRL3',1,0,0x56334601,sf,sf*16+slot,0xffffffff,512)
    body=wire[32:544]
    if kind==3:
     assert body[:4]==b'VRS3';first,count=struct.unpack_from('<IB',body,20);new_serial=struct.unpack_from('<I',body,28)[0]
     assert 1<=count<=2 and first+count<=plan['source_blocks']
     if serial is not None:
      assert ((new_serial-serial)&0xffffffff)<0x80000000
      if new_serial==serial:assert declared==(first,count)
     declared=(first,count);serial=new_serial;descriptors+=1
     for b in range(first,first+count):
      source=raw[b*10240:(b+1)*10240]
      captures=[struct.unpack_from('<Q',source,p+4)[0] for p in range(0,10240,512) if source[p:p+512]!=bytes(512)]
      ready=max(captures)+50000 if schedule.actual_ready is None else schedule.actual_ready[b]
      assert ready<=sf*20000,'descriptor must not age an unavailable block'
      opened.setdefault(b,sf*20000)
     continue
    if kind==5:
     assert declared is None and body==bytes(512);controls+=1;continue
    assert kind in (1,2) and declared is not None and declared[0]<=block<sum(declared)
    assert block==current and index==expected_index and kind==(1 if index<20 else 2)
    expected_index+=1;tx_counts[slot&1]+=1;tx_hash[slot&1].update(body)
    # Inject12 source CRC failures after construction; the RX oracle discards
    # their equations and reconstructs from the20 remaining rows, never a pattern.
    bad=bytearray(wire)
    if index<12:bad[-1]^=1;erased+=1
    if zlib.crc32(bad[:-4])==struct.unpack_from('<I',bad,544)[0]:received[index]=body
    if index==31:
     restored=recover(received);assert restored is not None
     expected=raw[block*10240:(block+1)*10240];assert restored.tobytes()==expected
     digest.update(restored.tobytes());received={};current+=1;expected_index=0
     last_symbol=int(DATA_INDICES[((slot//2)+1)*PACKET_BYTES*4-1])
     end=sf*20000+((last_symbol+1)*SPS+len(h)-1)*1000000/RATE;ends[block]=end
     assert end-opened[block]+20000<500000,'include20ms FEC job allowance below actual500ms bank deadline'
     for p in range(0,10240,512):
      if expected[p:p+512]!=bytes(512):
       f,capture=struct.unpack_from('<IQ',expected,p);frame_last[f]=(capture,max(end,frame_last.get(f,(0,0))[1]))
  assert schedule.finished() and current==plan['source_blocks'] and not received
  assert controls==128 and plan['startup_training_us']==160000
  assert digest.hexdigest()==hashlib.sha256(raw).hexdigest() and all(tx_counts) and tx_hash[0].hexdigest()!=tx_hash[1].hexdigest()
  schedule.close();faults=[]
  for name,offset,patch in [('fragment_length',24,struct.pack('<H',473)),('capture_time',4,struct.pack('<Q',1)),('reserved',36,struct.pack('<I',1)),('nonterminal_zero',0,bytes(512))]:
   data=bytearray(raw);data[offset:offset+len(patch)]=patch;path=out/(name+'.symbols');path.write_bytes(data)
   try:preflight(path)
   except ValueError as e:faults.append({'name':name,'rejected':True,'reason':str(e)})
   else:raise AssertionError('malformed video source accepted: '+name)
  # Legal-sized source fragments representing an unsustainable single-AU burst.
  # This is a queue-budget stimulus, not a decodable video fixture.
  total=800000;count=(total+471)//472;path=out/'over3s-source-queue.symbols'
  with path.open('wb') as f:
   for i in range(count):
    length=min(472,total-i*472);flags=4|(1 if i==0 else 0)|(2 if i==count-1 else 0)
    f.write(struct.pack('<IQIIIHHHBBII',0,0,0,total,i*472,length,i,count,5,flags,50000,0)+bytes([0x5a])*length+bytes(472-length))
   f.write(bytes((-count)%20*512))
  try:preflight(path)
  except ValueError as e:
   assert 'queue exceeds3s' in str(e);faults.append({'name':'modeled_source_queue_over3s','rejected':True,'reason':str(e)})
  else:raise AssertionError('over3s source service admitted')
  if a.availability:
   manifest=json.loads(a.availability.read_text())
   for name in ['digest','future_capture','early_ready','reverse_ready','reverse_capture','frame_count','block_count']:
    bad=json.loads(json.dumps(manifest))
    if name=='digest':bad['source_sha256']='0'*64
    elif name=='future_capture':bad['capture_times_us'][-1]=64000001
    elif name=='early_ready':bad['block_ready_us']=[0]*len(bad['block_ready_us'])
    elif name=='reverse_ready':bad['block_ready_us'][-1]=0
    elif name=='reverse_capture':bad['capture_times_us'][1]=0
    elif name=='frame_count':bad['capture_times_us'].pop()
    elif name=='block_count':bad['block_ready_us'].pop()
    path=out/(name+'.json');path.write_text(json.dumps(bad))
    try:preflight(a.symbols,path)
    except ValueError as e:faults.append({'name':'actual_ready_'+name,'rejected':True,'reason':str(e)})
    else:raise AssertionError('malformed measured readiness accepted: '+name)
  result.update(completed=True,plan=plan,legacy_reference_commit=commit,legacy_source_sha256=hashlib.sha256(old).hexdigest(),legacy_IQ_regression=regression,
   all_RS_blocks_recovered_after12_source_CRC_erasures=True,RS_blocks_compared=current,source_sha256=digest.hexdigest(),recovered_source_bytes=len(raw),CRC_erasure_packets=erased,
   dual_TX_coded_packets=tx_counts,dual_TX_payload_sha256=[p.hexdigest() for p in tx_hash],descriptor_packets=descriptors,startup_control_packets=controls,
   maximum_descriptor_to_coded_tail_us=max(ends[b]-opened[b] for b in ends),
   eager_eight_future_descriptor_modeled_deadline_risk_blocks=[b for b in ends if ends[b]-eager_opened[b]+20000>=500000],
   modeled_maximum_capture_to_coded_tail_us=max(end-capture for capture,end in frame_last.values()),
   source_cache_peak_blocks=schedule.peak_source_blocks,coded_cache_peak_blocks=schedule.peak_coded_blocks,
   input_controls=faults,RF_capacity_or_source_age_passed=False)
 finally:
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  (out/'disk-after.json').write_text(json.dumps(check('after-video-wire-controls'),indent=2)+'\n')
 print(json.dumps(result,indent=2),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--symbols',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 p.add_argument('--availability',type=Path);main(p.parse_args())
