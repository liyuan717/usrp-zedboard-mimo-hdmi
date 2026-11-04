import sys,tempfile,unittest,hashlib,zlib,struct
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from original_file_protocol import manifest,chunks,Assembler,DATA
class OriginalFileTests(unittest.TestCase):
 def test_original_container_roundtrip_and_negative_controls(self):
  media=Path(__file__).resolve().parents[1]/'video-data/media/0902871f070d72c80a68a35cde15c326/original'
  raw=media.read_bytes() if media.exists() else bytes(range(256))*500
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'source';source.write_bytes(raw);session=bytes(range(16))
   head=manifest(source,session);packets=list(chunks(source,session))
   a=Assembler(root/'valid')
   try:
    a.accept(head);a.accept(head)
    for p in packets:a.accept(p);a.accept(p)
    result=a.finish();self.assertEqual(Path(result['path']).read_bytes(),raw)
    self.assertEqual(result['sha256'],hashlib.sha256(raw).hexdigest());self.assertFalse(result['RF_verified'])
   finally:a.close()
   for case in ['missing','crc','session','digest','duplicate']:
    a=Assembler(root/case)
    try:
     a.accept(head)
     with self.assertRaises(ValueError):
      if case=='missing':a.accept(packets[1])
      elif case=='crc':a.accept(packets[0][:-1]+bytes([packets[0][-1]^1]))
      elif case=='session':
       p=bytearray(packets[0]);p[4]^=1;struct.pack_into('<I',p,124,zlib.crc32(p[:124]));a.accept(p)
      elif case=='duplicate':
       a.accept(packets[0]);p=bytearray(packets[0]);p[32]^=1;struct.pack_into('<I',p,124,zlib.crc32(p[:124]));a.accept(p)
      else:
       p=bytearray(packets[-1]);p[32]^=1;struct.pack_into('<I',p,124,zlib.crc32(p[:124]))
       for packet in packets[:-1]:a.accept(packet)
       a.accept(p);a.finish()
    finally:a.close()
if __name__=='__main__':unittest.main()
