"""Unfrozen Stage1 forward-timebase control payload.

Wire format and finite timed-UHD observation projection. The projection uses
an explicit candidate oscillator allowance pending hardware calibration.
"""
import struct
from dataclasses import dataclass
from fractions import Fraction

FORMAT=struct.Struct('<4sBBHQqq')
TX_PERIOD_SAMPLES=80000
TX_TRAINING_SAMPLE=416*4+20  # TX RRC group delay, same public training reference as RX.
TX_CLOCK_ALLOWANCE_PPM=200
PROFILE_REPEAT_PERIODS=8


def clock_rate_bounds(begin, end):
 """Enclose the observed host/device clock ratio without a timed TX start."""
 first_before,first_after,first_ticks=begin
 before_ns,after_ns,observed_ticks=end
 ticks=observed_ticks-first_ticks
 if not (0<first_before<=first_after<before_ns<=after_ns<1<<63 and 2<=ticks and
         first_after-first_before<=20_000_000 and after_ns-before_ns<=20_000_000):
  raise ValueError('invalid post-EOB clock observation')
 low=Fraction(before_ns-first_after,(ticks+1)*250)
 high=Fraction(after_ns-first_before,(ticks-1)*250)
 return dict(host_ns_per_device_nominal_ns_ppm=[float((x-1)*1_000_000) for x in (low,high)],
             observed_ticks=ticks,projection_allowance_ppm=TX_CLOCK_ALLOWANCE_PPM,
             observed_rate_within_projection=(Fraction(1_000_000,1_000_000+TX_CLOCK_ALLOWANCE_PPM)
                                               <=low<=high<=Fraction(1_000_000,1_000_000-TX_CLOCK_ALLOWANCE_PPM)))


def training_host_interval(tx_ticks, observations):
 """Intersect endpoint bounds using the candidate rate allowance in either direction."""
 intervals=[]
 for before_ns,after_ns,ticks in observations:
  delta=(tx_ticks-ticks)*250
  allowance=(abs(delta)*TX_CLOCK_ALLOWANCE_PPM+999_799)//999_800+251
  intervals.append((before_ns+delta-allowance,after_ns+delta+allowance))
 low=max(row[0] for row in intervals)
 high=min(row[1] for row in intervals)
 if low>high:raise ValueError('TX observations contradict bounded-rate projection')
 return low,high

@dataclass(frozen=True)
class TxClockBracket:
 before_ns:int
 after_ns:int
 observed_ticks:int
 start_ticks:int
 clock_domain:str='MONOTONIC'

 def __post_init__(self):
  if not (self.clock_domain in ('MONOTONIC','MONOTONIC_RAW') and 0<self.before_ns<=self.after_ns<1<<63 and
          self.after_ns-self.before_ns<=20_000_000 and
          0<=self.observed_ticks<self.start_ticks<1<<63 and
          self.start_ticks-self.observed_ticks==1_000_000):
   raise ValueError('invalid 4MSPS timed-SOB observation')

 def at_training(self,sf,capture_epoch_us):
  if not (0<=sf<3200 and 0<capture_epoch_us<1<<63):
   raise ValueError('invalid finite superframe or camera epoch')
  sample=sf*TX_PERIOD_SAMPLES+TX_TRAINING_SAMPLE
  delta_ns=(self.start_ticks-self.observed_ticks+sample)*250
  # Bound rate error in the clock conversion and round outward. One sample
  # of extra allowance covers quantization of the observed device time.
  allowance_ns=(delta_ns*TX_CLOCK_ALLOWANCE_PPM+999_799)//999_800+250
  lower=(self.before_ns+delta_ns-allowance_ns)//1000-capture_epoch_us
  upper=(self.after_ns+delta_ns+allowance_ns+999)//1000-capture_epoch_us
  return ForwardTimebase(sample,lower,upper)

 def rate_after_burst(self,before_ns,after_ns,observed_ticks):
  return clock_rate_bounds((self.before_ns,self.after_ns,self.observed_ticks),
                           (before_ns,after_ns,observed_ticks))

def load_clock_bracket(value,raw=False):
 if raw:
  if not isinstance(value,dict) or set(value)!=set(('clock_domain','values')) or value['clock_domain']!='MONOTONIC_RAW':
   raise ValueError('explicit RAW timed TX observation required')
  value=value['values']
 if not isinstance(value,list) or len(value)!=4 or any(type(v) is not int for v in value):
  raise ValueError('invalid timed TX observation shape')
 return TxClockBracket(*value,clock_domain='MONOTONIC_RAW' if raw else 'MONOTONIC')

@dataclass(frozen=True)
class ForwardTimebase:
 tx_sample_index:int
 source_lower_us:int
 source_upper_us:int

 def pack(self):
  if not (0<=self.tx_sample_index<1<<64 and
          -(1<<63)<=self.source_lower_us<=self.source_upper_us<1<<63):
   raise ValueError('bounded forward timebase interval')
  return FORMAT.pack(b'VRT3',1,240,1,self.tx_sample_index,self.source_lower_us,self.source_upper_us)

def unpack(payload):
 if len(payload)!=32:raise ValueError('32B forward control payload required')
 magic,major,profile,flags,sample,low,high=FORMAT.unpack(payload)
 if (magic,major,profile,flags)!=(b'VRT3',1,240,1) or low>high:
  raise ValueError('unsupported or reversed forward control')
 return ForwardTimebase(sample,low,high)
