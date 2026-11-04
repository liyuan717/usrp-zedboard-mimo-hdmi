"""Bounded, read-only AVC MP4 demux for the current board. No re-encoding.
Called on the board only after the original file has passed its received SHA.
"""
import struct,zlib
from pathlib import Path

def boxes(raw,start=0,end=None):
 end=len(raw) if end is None else end
 while start<end:
  if end-start<8:raise ValueError('short MP4 box')
  n,kind=struct.unpack_from('>I4s',raw,start);head=8
  if n==1:
   if end-start<16:raise ValueError('short large MP4 box')
   n=struct.unpack_from('>Q',raw,start+8)[0];head=16
  elif n==0:n=end-start
  if n<head or start+n>end:raise ValueError('MP4 box bounds')
  yield kind,start+head,start+n
  start+=n

def one(raw,start,end,kind):
 found=[(a,b) for k,a,b in boxes(raw,start,end) if k==kind]
 if len(found)!=1:raise ValueError('missing/duplicate MP4 '+repr(kind))
 return found[0]

def rows(raw,span,fmt):
 a,b=span;unit=struct.Struct('>'+fmt)
 if b-a<8:raise ValueError('short MP4 table')
 n=struct.unpack_from('>I',raw,a+4)[0]
 if n>4096 or b-a!=8+n*unit.size:raise ValueError('MP4 table size')
 return [unit.unpack_from(raw,a+8+i*unit.size) for i in range(n)]

def access_units(path):
 if not 0<Path(path).stat().st_size<=180000:raise ValueError('finite MP4 limit 180000 bytes')
 raw=Path(path).read_bytes()
 if not 0<len(raw)<=180000:raise ValueError('finite MP4 limit 180000 bytes')
 top=list(boxes(raw));mdat=[(a,b) for k,a,b in top if k==b'mdat']
 if len(mdat)!=1 or any(k==b'moof' for k,a,b in top):raise ValueError('one nonfragmented MP4 mdat required')
 moov=one(raw,0,len(raw),b'moov');tracks=[]
 for k,a,b in boxes(raw,*moov):
  if k!=b'trak':continue
  mdia=one(raw,a,b,b'mdia');hdlr=one(raw,*mdia,b'hdlr')
  if raw[hdlr[0]+8:hdlr[0]+12]==b'vide':tracks.append(mdia)
 if len(tracks)!=1:raise ValueError('one video track required')
 mdia=tracks[0];mdhd=one(raw,*mdia,b'mdhd')
 a,b=mdhd
 if raw[a]!=0:raise ValueError('unsupported mdhd version')
 timescale=struct.unpack_from('>I',raw,a+12)[0]
 if not timescale:raise ValueError('zero MP4 timescale')
 minf=one(raw,*mdia,b'minf');stbl=one(raw,*minf,b'stbl')
 table_boxes=list(boxes(raw,*stbl))
 if len({k for k,a,b in table_boxes})!=len(table_boxes):raise ValueError('duplicate sample table')
 children={k:(a,b) for k,a,b in table_boxes}
 if (b'stco' in children)==(b'co64' in children):raise ValueError('ambiguous/missing chunk offsets')
 if b'ctts' in children and any(offset for count,offset in rows(raw,children[b'ctts'],'II')):raise ValueError('B/reordered timestamps not supported')
 stsd=one(raw,*stbl,b'stsd');a,b=stsd
 if struct.unpack_from('>I',raw,a+4)[0]!=1:raise ValueError('one AVC description required')
 entries=list(boxes(raw,a+8,b))
 if len(entries)!=1 or entries[0][0]!=b'avc1':raise ValueError('avc1 required')
 _,a,b=entries[0]
 if struct.unpack_from('>HH',raw,a+24)!=(1024,600):raise ValueError('AVC dimensions not 1024x600')
 ca,cb=one(raw,a+78,b,b'avcC');config=raw[ca:cb]
 if len(config)<7 or config[0]!=1 or config[1]!=66 or not config[2]&0x40:raise ValueError('Constrained Baseline required')
 length_bytes=(config[4]&3)+1;at=6;sps=[];pps=[]
 for group,count in [(sps,config[5]&31)]:
  for _ in range(count):
   size=struct.unpack_from('>H',config,at)[0];at+=2;group.append(config[at:at+size]);at+=size
 if at>=len(config):raise ValueError('missing PPS')
 count=config[at];at+=1
 for _ in range(count):
  size=struct.unpack_from('>H',config,at)[0];at+=2;pps.append(config[at:at+size]);at+=size
 if not sps or not pps or at!=len(config):raise ValueError('AVC config bounds')
 a,b=one(raw,*stbl,b'stsz');default,n=struct.unpack_from('>II',raw,a+4)
 if n!=25 or b-a!=12+(0 if default else n*4):raise ValueError('first version needs exactly25 video frames')
 sizes=[default]*n if default else list(struct.unpack_from('>'+str(n)+'I',raw,a+12))
 schedule=[];stamp=0
 for count,delta in rows(raw,one(raw,*stbl,b'stts'),'II'):
  if count>25 or delta<=0:raise ValueError('sample timing bounds')
  for _ in range(count):schedule.append(stamp);stamp+=delta
 if len(schedule)!=25 or any(t*5!=i*timescale for i,t in enumerate(schedule)):raise ValueError('exact5fps source required')
 offsets=rows(raw,children.get(b'stco',children.get(b'co64')), 'I' if b'stco' in children else 'Q')
 sc=rows(raw,one(raw,*stbl,b'stsc'),'III')
 if not sc or sc[0][0]!=1 or any(c<1 or d!=1 for first,c,d in sc) or any(b[0]<=a[0] for a,b in zip(sc,sc[1:])):raise ValueError('chunk map invalid')
 samples=[];sample=0;index=0
 for chunk,(offset,) in enumerate(offsets,1):
  while index+1<len(sc) and sc[index+1][0]<=chunk:index+=1
  for _ in range(sc[index][1]):
   if sample>=n:raise ValueError('sample map exceeds count')
   size=sizes[sample]
   if not 0<size<=180000 or offset<mdat[0][0] or offset+size>mdat[0][1]:raise ValueError('sample outside mdat')
   nal=[];cursor=offset
   while cursor<offset+size:
    if cursor+length_bytes>offset+size:raise ValueError('short NAL length')
    length=int.from_bytes(raw[cursor:cursor+length_bytes],'big');cursor+=length_bytes
    if not 0<length<=offset+size-cursor:raise ValueError('NAL bounds')
    nal.append(raw[cursor:cursor+length]);cursor+=length
   slices=[x for x in nal if x[0]&31 in (1,5)]
   if len(slices)!=1:raise ValueError('single slice required')
   key=slices[0][0]&31==5
   # AUD and avcC parameter sets are decoder framing, not video encoding.
   nal=[x for x in nal if x[0]&31 not in (7,8,9)]
   body=b'\x00\x00\x00\x01\x09\xf0'+b''.join(b'\x00\x00\x00\x01'+x for x in ((sps+pps if key else [])+nal))
   if sample==0 and not key:raise ValueError('initial IDR required')
   samples.append((body,sample*200,7 if key else 0));sample+=1;offset+=size
 if sample!=n:raise ValueError('incomplete sample table')
 return samples

def playback_rfd(path,target,session=714):
 from reference_rvf2 import encode
 from reference_mrf1 import packet
 units=access_units(path);fid=0
 with Path(target).open('xb') as out:
  for seq,(body,pts,flags) in enumerate(units):
   _,fragments=encode(body,session_id=session,sequence=seq,timestamp_ms=pts,flags=flags)
   for i in range(0,len(fragments),2):
    fid+=1;payload=packet(fragments[i],fid,0)+packet(fragments[min(i+1,len(fragments)-1)],fid,1)
    header=struct.pack('<4sIII',b'RFD1',fid,1,304)
    out.write(header+struct.pack('<I',zlib.crc32(payload,zlib.crc32(header)))+payload)
 return dict(AUs=len(units),records=fid,scope='decoder adapter derived solely from verified received original file; not raw RF evidence')
