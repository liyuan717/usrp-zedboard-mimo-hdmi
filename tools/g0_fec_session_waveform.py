#!/usr/bin/env python3
"""Finite dual-TX G0 waveform carrying actual in-band VRS3 descriptors and FEC.
Not video, production profile or throughput signoff. Fixed8/RS20/32/S512;
source truth belongs only to TX and post-reception comparison.
"""
import argparse,json,struct,zlib
from pathlib import Path
import numpy as np
from scipy import signal
from g0_packet_probe import PERIOD,STREAM_BYTES,DATA_INDICES,PILOTS,HEADER_START,DATA_START,public_prefix,phy_header,map_bytes
from g0_sounder import RATE,SPS,rrc
from g0_packet_fec import encode,source_pattern
from workspace_storage_guard import check as disk_check,GIB

def descriptor(first,count=8,serial=None):
 return struct.pack('<4sBBHBBHHHHHIBBHI',b'VRS3',1,0xf0,0,20,32,512,1024,600,20,1,first,count,0,0,first if serial is None else serial)

def packet(session,sf,physical_slot,cache,erase):
 if physical_slot<2:
  first=sf*14//32;kind=3;block=0xffffffff;index=0;body=descriptor(first)+bytes(480)
 else:
  block,index=divmod(sf*14+physical_slot-2,32);kind=1 if index<20 else 2
  if block not in cache:cache[block]=encode(source_pattern(block))
  body=cache[block][index].tobytes()
 header=struct.pack('<4sBBHIIIIIHH',b'VRL3',1,kind,0,session,sf,sf*16+physical_slot,0xffffffff,block,index,512)
 wire=bytearray(header+body+struct.pack('<I',zlib.crc32(header+body)))
 damaged=kind==1 and index<erase
 if damaged:wire[-1]^=1
 return wire,{'physical_slot':physical_slot,'kind':kind,'block':block,'symbol':index,'intentional_CRC_error':damaged}

def modulate_packets(session,sf,packet_at,prefix,h):
 s=np.zeros((PERIOD,2),dtype=complex);s[:1408]=prefix
 bits=np.unpackbits(np.frombuffer(phy_header(session,sf),dtype=np.uint8),bitorder='big').astype(np.int8);s[HEADER_START:DATA_START,0]=np.repeat(1-2*bits,2)
 frame=[]
 for tx in range(2):
  payload=bytearray()
  for slot in range(8):
   p,info=packet_at(slot*2+tx);payload.extend(p);info.update({'TX':tx,'slot':slot});frame.append(info)
  assert len(payload)==STREAM_BYTES;s[DATA_INDICES,tx]=map_bytes(payload)
 s[PILOTS]=[1,0];s[PILOTS+1]=[0,1]
 # Polyphase interpolation skips the inserted zeros. Crop the filter
 # tail at the same period boundary as the zero-stuffed reference.
 iq=signal.upfirdn(h,s,up=SPS,axis=0)[:PERIOD*SPS]*.14
 packed=np.empty((len(iq),4),dtype='<f4');packed[:,0::2]=iq.real;packed[:,1::2]=iq.imag
 return packed,frame

def frame_iq(session,sf,cache,erase,prefix,h):
 return modulate_packets(session,sf,lambda physical_slot:packet(session,sf,physical_slot,cache,erase),prefix,h)

def main(a):
 if not 8<=a.frames<=48 or not 0<=a.erase_sources<=12:raise ValueError('finite8..48 frames and0..12 source erasures required')
 out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);before=disk_check('before-fec-session-waveform');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 try:
  prefix=public_prefix();h=rrc();cache={};maximum=0.;rows=[]
  with (out/'iq.fc32').open('wb') as f:
   for sf in range(a.frames):
    packed,frame=frame_iq(a.session,sf,cache,a.erase_sources,prefix,h)
    maximum=max(maximum,float(np.max(np.abs(packed))));assert maximum<=.125;packed.tofile(f);rows.append({'superframe':sf,'packets':frame})
  result={'scope':__doc__,'production_use_allowed':False,'g0_passed':False,'profile_id':240,'feedback_enabled':False,'FMCOMMS3_TX_enabled':False,'session':a.session,'sample_rate_hz':RATE,'symbol_rate_hz':RATE//SPS,'period_symbols':PERIOD,'period_samples':PERIOD*SPS,'repetitions':a.frames,'total_samples':a.frames*PERIOD*SPS,'duration_seconds':a.frames*PERIOD*SPS/RATE,'max_IQ_component':maximum,'scale':.14,'FEC':{'banks':8,'k':20,'n':32,'symbol_bytes':512},'inband_descriptors_per_period':2,'data_packets_per_period':14,'intentional_CRC_bad_sources_per_block':a.erase_sources,'descriptor_airtime_and_damage_are_probe_overhead_not_final_capacity_acceptance':True}
  (out/'waveform.json').write_text(json.dumps(result,indent=2)+'\n');(out/'transmitted-packets.json').write_text(json.dumps(rows,indent=2)+'\n');print(json.dumps(result),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-fec-session-waveform'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--frames',type=int,default=48);p.add_argument('--session',type=lambda x:int(x,0),default=0x56334601);p.add_argument('--erase-sources',type=int,default=4);main(p.parse_args())
