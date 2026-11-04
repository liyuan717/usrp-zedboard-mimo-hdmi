"""Bounded OFFLINE display pacing from received public RVF2/ZVID2 headers."""
import struct,time

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
