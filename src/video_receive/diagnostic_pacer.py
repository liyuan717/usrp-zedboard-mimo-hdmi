"""Bounded OFFLINE display pacing from received public RVF2/ZVID2 headers."""
import struct,time,zlib
from types import SimpleNamespace

class PacketPacedTarget:
    """Same delivery-target interface; pace each verified packet separately.

    A record can straddle AU boundaries. Forward its old-AU tail BEFORE waiting
    on the next AU's PTS. All bytes and ordering still reach the original target.
    The original reassembly/codec/CRC implementation remains authoritative.
    """
    def __init__(self,target,pacer,deadline):
        self._target,self._pacer,self._deadline=target,pacer,deadline
    def __getattr__(self,name):
        return getattr(self._target,name)
    def consume_packet(self,stream,packet,*,now=None):
        if type(packet) is not bytes or len(packet)!=152 or stream not in (0,1):
            raise ValueError('packet pacing requires intact dual-stream MRF packet')
        if (packet[:4]!=b'MRF1' or packet[4]!=1 or packet[5]!=stream
                or zlib.crc32(packet[:16])!=struct.unpack_from('<I',packet,16)[0]
                or zlib.crc32(packet[20:148])!=struct.unpack_from('<I',packet,148)[0]):
            raise ValueError('packet pacing refuses invalid MRF identity or CRC')
        self._pacer.before_record(SimpleNamespace(packets=(packet,)),self._deadline)
        return self._target.consume_packet(stream,packet,now=self._pacer.clock())

class DiagnosticPacer:
    def __init__(self,scale,session,clock=time.monotonic,sleep=time.sleep):
        if not 1<=scale<=10:raise ValueError('diagnostic scale must be1..10')
        self.scale,self.session,self.clock,self.sleep=scale,session,clock,sleep
        self.start=self.base_pts=None
        self.seen={}
        self.schedule=[]
        self.last_seq=-1
        self.last_pts=-1
    def before_record(self,record,deadline):
        for packet in record.packets:
            payload=packet[20:148]
            if len(payload)!=128:raise ValueError('diagnostic needs complete MRF1 payload')
            magic,version,flags,size,session,seq,total,offset,crc=struct.unpack_from('<4sBBHIIIII',payload)
            if magic!=b'RVF2' or version!=2 or session!=self.session:raise ValueError('diagnostic RVF2 identity')
            if offset:continue
            if size<36:raise ValueError('diagnostic first fragment lacks ZVID2 header')
            h=struct.unpack_from('<4sBBHIHHIIIQ',payload,28)
            if h[0]!=b'ZV2D' or h[1]!=2 or h[2]!=2 or h[4]!=seq:raise ValueError('diagnostic ZVID2 identity')
            pts=h[-1]
            if seq in self.seen:
                if self.seen[seq]!=pts:raise ValueError('conflicting source PTS')
                continue
            if len(self.seen)>=10 or seq<=self.last_seq or pts<self.last_pts:raise ValueError('diagnostic source order/bound')
            if self.start is None:self.start=self.clock();self.base_pts=pts
            if pts-self.base_pts>2000:raise ValueError('diagnostic source span exceeds2s')
            target=self.start+(pts-self.base_pts)*.001*self.scale
            if target>=deadline:raise ValueError('paced source exceeds offline deadline')
            while self.clock()<target:self.sleep(min(.05,target-self.clock()))
            self.schedule.append(dict(sequence=seq,source_pts_ms=pts,target_monotonic_s=target,released_monotonic_s=self.clock()))
            self.seen[seq]=pts;self.last_seq=seq;self.last_pts=pts
