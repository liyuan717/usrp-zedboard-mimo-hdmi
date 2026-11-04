"""Host-only independent ADC/packet/file audit. Never supplied to the receiver."""
from pathlib import Path
import ast,hashlib,struct,sys,zlib
from original_file_protocol import manifest,chunks
APP=Path(__file__).resolve().parent

def audit(evidence,source):
 D=APP.parent/'deliverables/usrp-zedboard-integration-20261026/realtime-hdmi'
 # Reuse the already verified ADC contract function without importing its media-specific grader.
 tree=ast.parse((D/'short-video-staged-r2-20261030/grade_evidence.py').read_text())
 node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='validate_adc')
 scope=dict(hashlib=hashlib,struct=struct,zlib=zlib)
 exec(compile(ast.Module(body=[node],type_ignores=[]),'frozen-ADC-contract','exec'),scope)
 adc=scope['validate_adc'](evidence/'new-adc-output.rxth',expected_blocks=5120)
 sys.path.append(str(D/'resume6-candidate/runtime/python/tools'))
 sys.path.append(str(APP.parent/'deliverables/usrp-zedboard-integration-20261026/candidates/usrp-preroll-gain12/usrp/mrf1-v1/protocol'))
 from read_rxonly_delivery import iter_delivery
 from reference_mrf1 import packet
 with (evidence/'received-live.rfd').open('rb') as f:records=list(iter_delivery(f))
 if not records:raise ValueError('no actual RF delivery records')
 session=records[0].packets[0][24:40]
 head=manifest(source,session);data=list(chunks(source,session))
 expected=[(head,head)]+[(data[i],data[min(i+1,len(data)-1)]) for i in range(0,len(data),2)]
 ids=[r.frame_id for r in records]
 if not len(expected)<=len(ids)<=len(expected)+3 or ids!=[1]*(len(ids)-len(expected)+1)+list(range(2,len(expected)+1)):raise ValueError('RF order/count differs from source')
 for row in records:
  if row.rejected or row.build_id!=1:raise ValueError('rejected RF record or wrong PL build')
  for lane,raw in enumerate(row.packets):
   if raw!=packet(expected[row.frame_id-1][lane],row.frame_id,lane):raise ValueError('RF packet differs byte-for-byte from selected source')
 received=evidence/'received-original.mp4'
 if received.read_bytes()!=source.read_bytes():raise ValueError('recovered original not byte-identical')
 return dict(valid=True,ADC=adc,RF_records=len(records),unique_RF_frames=len(expected),CRC_correct_byte_identical_packets=len(records)*2,original_file_byte_identical=True,original_file_sha256=hashlib.sha256(received.read_bytes()).hexdigest())
