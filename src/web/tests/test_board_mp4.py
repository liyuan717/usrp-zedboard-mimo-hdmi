from pathlib import Path
import sys,tempfile,unittest,struct
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from board_mp4 import access_units,boxes
from original_backend import fixed_feeder
class BoardMP4Tests(unittest.TestCase):
 def test_all_prepared_files_and_corrupt_bounds(self):
  sources=sorted(Path('/home/leeeo/Downloads/videos').glob('*.mp4'))
  if not sources:self.skipTest('local prepared media not present')
  for source in sources:
   units=access_units(source)
   self.assertEqual(len(units),25);self.assertEqual([u[1] for u in units],list(range(0,5000,200)))
   self.assertEqual(units[0][2],7)
   with tempfile.TemporaryDirectory() as tmp:
    p=Path(tmp)/'bad.mp4';raw=bytearray(source.read_bytes());raw[0:4]=struct.pack('>I',len(raw)+100);p.write_bytes(raw)
    with self.assertRaises(ValueError):access_units(p)
    p.write_bytes(source.read_bytes()[:12])
    with self.assertRaises(ValueError):access_units(p)
 def test_private_feeder_gain_readback_and_timing(self):
  from types import SimpleNamespace
  stream=b''.join(struct.pack('<4sQI',b'IQB1',i,16384)+bytes(16384*16) for i in range(5))+struct.pack('<4sQI',b'IQB1',5,0)
  def simulate(requested, actual):
   flags={'closed':False,'trigger':False};now=[0.0]
   class Pipe:
    def fileno(self):return -1
    def close(self):flags['closed']=True
   proc=SimpleNamespace(stdin=Pipe(),poll=lambda:0 if flags['closed'] and flags['trigger'] else None)
   readback=dict(serial='3095BA5',configuration_matches_requested=True,clock_source='internal',time_source='internal',channels=[dict(channel=c,sample_rate_hz=4000000,frequency_hz=5500000000,bandwidth_hz=40000000,gain_db=actual,antenna='TX/RX',lo_locked=True) for c in (0,1)])
   def sleep(t):now[0]+=t
   return fixed_feeder(requested)(proc,stream,lambda:readback,lambda:flags.update(trigger=True),lambda:None,clock=lambda:now[0],sleep=sleep,writable=lambda _:True,write=lambda fd,data:len(data))
  self.assertEqual(simulate(0,0)['input_bytes'],len(stream))
  self.assertEqual(simulate(18,18)['input_bytes'],len(stream))
  with self.assertRaises(ValueError):simulate(0,18)
  with self.assertRaises(ValueError):simulate(18,float('nan'))

 def test_size_limit_before_reading_file(self):
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'large.mp4'
   with p.open('wb') as f:f.truncate(180001)
   with patch.object(Path,'read_bytes',side_effect=AssertionError('must reject before allocating')):
    with self.assertRaises(ValueError):access_units(p)
 def test_box_bounds(self):
  for raw in [b'x',struct.pack('>I4s',4,b'moov'),struct.pack('>I4s',1,b'moov')]:
   with self.assertRaises(ValueError):list(boxes(raw))
if __name__=='__main__':unittest.main()
