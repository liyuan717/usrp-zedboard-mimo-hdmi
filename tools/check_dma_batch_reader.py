#!/usr/bin/env python3
"""Cross-check PS C batch parsing against RTL vectors and bounded corruption/recovery cases."""
import argparse,hashlib,http.server,json,re,shutil,struct,subprocess,threading,zlib
from pathlib import Path
from dma_batch_stream_probe import decode_batch
from workspace_storage_guard import check as disk_check,GIB
from board_command import command
from hardware_lease import HardwareLease

def fixture(ref,out):
    batches=[];data=bytearray()
    for row in (ref/'batches.txt').read_text().splitlines():
        phase,word,last=row.split();data.extend(int(word,16).to_bytes(8,'little'))
        if int(last):batches.append(bytes(data));data.clear()
    assert len(batches)==10 and not data
    decoded=[decode_batch(b,i) for i,b in enumerate(batches)];cases=[]
    def add(data,records,flags,length=32768,drop=0):cases.append((bytes(data),records,flags,length,drop))
    for data,(records,meta) in zip(batches,decoded):add(data,records,8 if meta['flags']&1 else 0)
    add(batches[-1],[],2)
    def changed(data,offset,fmt,value):
        p=bytearray(data);struct.pack_into(fmt,p,offset,value);return p
    for offset,fmt,value in [(0,'I',0),(4,'H',99),(32740,'I',0xffffffff),(32752,'Q',999)]:add(changed(batches[0],offset,'<'+fmt,value),[],1)
    add(batches[0],[],1,length=8);add(batches[0],[],1,length=0xffffffff)
    def seq(base,n):return changed(changed(base,16,'<Q',n),32752,'<Q',n)
    first=decoded[0][0];second_offset=32+len(first[0])
    add(changed(seq(batches[0],10),second_offset+8,'<I',0xffffffff),first[:1],16)
    add(changed(seq(batches[0],11),32+54,'<H',65535),first[1:],16)
    add(changed(seq(batches[0],12),32+12,'<H',65530),first[1:],0)
    add(seq(batches[-1],14),decoded[-1][0],4);add(seq(batches[-1],13),[],32)
    add(seq(batches[0],15),first[1:],0,drop=1)
    add(changed(seq(batches[0],16),32744,'<I',len(first)+1),first,16)
    add(batches[0],[],1,length=0)
    add(changed(seq(batches[-1],17),32,'<I',0),[],16)
    add(seq(batches[-1],18),decoded[-1][0],0)
    crc=0;expected=[]
    with (out/'fixture.bin').open('wb') as f:
        for i,(data,records,flags,length,drop) in enumerate(cases):
            f.write(struct.pack('<II',length,drop)+data)
            for record in records:crc=zlib.crc32(record,crc)
            expected.append(f'CASE {i} flags={flags} records={len(records)} crc={crc:08x}')
    expected.append(f'TOTAL batches=18 records=434 bad_metadata=7 bad_records=4 duplicate=1 late=1 missing=1 damaged=3 unknown=1 consumer_drop=1 crc={crc:08x}')
    (out/'expected.txt').write_text('\n'.join(expected)+'\n');return expected

def validate(text,expected):
    rows=[line.strip() for line in text.splitlines() if line.startswith('CASE ') or line.startswith('TOTAL ')]
    assert rows==expected,(rows,expected)

def captured_fixture(ref,out):
    """All actual ADC-replay batches, with independent committed-record CRC.

    Require the complete52RTL proof first; a partial simulation file is not
    accepted as a finite capture. This remains file replay, not physical DMA.
    """
    proof=json.loads((ref/'receipt.json').read_text())
    assert proof['independent_SOURCE_validation']['SOURCE_records']==136
    assert proof['independent_batch_validation']['all_committed_record_bytes_equal']
    data=bytearray();batches=0;records=0;crc=0;expected=[]
    with (out/'fixture.bin').open('wb') as f:
        for row in (ref/'dma-batches.txt').read_text().splitlines():
            word,last=row.split();data.extend(int(word,16).to_bytes(8,'little'))
            if int(last):
                decoded,meta=decode_batch(bytes(data),batches)
                assert not meta['flags']&1,'damaged batch in clean finite ADC proof'
                f.write(struct.pack('<II',len(data),0)+data)
                for record in decoded:crc=zlib.crc32(record,crc)
                expected.append(f'CASE {batches} flags=0 records={len(decoded)} crc={crc:08x}')
                batches+=1;records+=len(decoded);data.clear()
    assert not data and records==142 and batches==proof['independent_batch_validation']['fixed_batches']
    expected.append(f'TOTAL batches={batches} records={records} bad_metadata=0 bad_records=0 duplicate=0 late=0 missing=0 damaged=0 unknown=0 consumer_drop=0 crc={crc:08x}')
    (out/'expected.txt').write_text('\n'.join(expected)+'\n');return expected

def forward_fixture(ref,out):
    proof=json.loads((ref/'receipt.json').read_text())
    assert proof['completed'] and proof['simulation'].startswith('G0_FORWARD_CONTROL_PASS')
    data=(ref/'batches.bin').read_bytes()
    assert hashlib.sha256(data).hexdigest()==proof['DMA_batch_sha256']
    records,meta=decode_batch(data,0)
    assert len(records)==5 and not meta['flags']&1  # timer flush bit1 is normal
    assert sum(struct.unpack_from('<H',r,12)[0]==3 for r in records)==3
    assert sum(struct.unpack_from('<H',r,12)[0]==1 for r in records)==2
    (out/'fixture.bin').write_bytes(struct.pack('<II',32768,0)+data)
    crc=0
    for row in records:crc=zlib.crc32(row,crc)
    expected=[f'CASE 0 flags=0 records=5 crc={crc:08x}',
      f'TOTAL batches=1 records=5 bad_metadata=0 bad_records=0 duplicate=0 late=0 missing=0 damaged=0 unknown=0 consumer_drop=0 crc={crc:08x}']
    (out/'expected.txt').write_text('\n'.join(expected)+'\n')
    return expected

def main(a):
    v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve()
    c=disk_check('before-PS-batch-reader');(out/'disk-before.json').write_text(json.dumps(c,indent=2)+'\n');assert c['disk']['available_bytes']>=5*GIB
    sources=['ps/include/g0_dma_batch.h','ps/g0_dma_batch.c','sim/g0/dma_batch_reader_probe.c']
    try:
        assert not (a.captured_air and a.forward_control)
        expected=(forward_fixture if a.forward_control else (captured_fixture if a.captured_air else fixture))(ref,out);(out/'include').mkdir()
        for name in sources:shutil.copyfile(v/name,out/('include/g0_dma_batch.h' if name.endswith('.h') else Path(name).name))
        flags=['-std=c11','-Wall','-Wextra','-Werror','-I'+str(out/'include')];inputs=[str(out/'g0_dma_batch.c'),str(out/'dma_batch_reader_probe.c')]
        with (out/'host-build.log').open('w') as log:subprocess.run(['gcc',*flags,'-g','-fsanitize=address,undefined',*inputs,'-o',str(out/'host-reader')],stdout=log,stderr=subprocess.STDOUT,check=True)
        result=subprocess.run([str(out/'host-reader'),str(out/'fixture.bin')],text=True,capture_output=True,check=True);(out/'host.log').write_text(result.stdout+result.stderr);validate(result.stdout,expected)
        with (out/'ARM-build.log').open('w') as log:subprocess.run([a.cross_prefix+'gcc',*flags,'-O2','-static',*inputs,'-o',str(out/'ARM-reader')],stdout=log,stderr=subprocess.STDOUT,check=True)
        receipt={'scope':__doc__,'cases':len(expected)-1,'input_reference':str(ref.relative_to(v)),'captured_ADC_batch_replay':a.captured_air,'expected_summary':expected[-1],'host_ASan_UBSan_passed':True,'ARM_compiled':True,'native_passed':False,'source_sha256':{n:hashlib.sha256((v/n).read_bytes()).hexdigest() for n in sources},'RF_video_or_physical_DMA_or_G0_passed':False}
        receipt['forward_SESSION_batch_file_replay']=a.forward_control
        if a.run_native:
            files={'reader':out/'ARM-reader','fixture':out/'fixture.bin'}
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    p=files.get(self.path.lstrip('/'))
                    if self.client_address[0]!='169.254.209.84' or p is None:self.send_error(403);return
                    data=p.read_bytes();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                def log_message(self,*args):pass
            with HardwareLease() as lease:
                server=http.server.ThreadingHTTPServer(('169.254.209.83',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();port=server.server_address[1]
                try:
                    script=f'''test "$(uname -r)" = 6.12.0-xilinx
cat /proc/sys/kernel/random/boot_id
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0
wget -q -O /tmp/v3-batch-reader http://169.254.209.83:{port}/reader
wget -q -O /tmp/v3-batch-fixture http://169.254.209.83:{port}/fixture
chmod 700 /tmp/v3-batch-reader
/tmp/v3-batch-reader /tmp/v3-batch-fixture
test "$(cat /sys/bus/iio/devices/iio:device0/out_altvoltage1_TX_LO_powerdown)" = 1
test "$(cat /sys/kernel/debug/iio/iio:device0/bist_prbs)" = 0'''
                    (out/'native.sh').write_text(script+'\n')
                    text=command('/dev/serial/by-id/usb-2012_Cypress_Semiconductor_Cypress-USB2UART-Ver1.0G_8344058D1E27-if00',script,out/'native.log',30,lease=lease).decode(errors='replace');validate(text,expected)
                    receipt['native_passed']=True
                finally:server.shutdown();server.server_close()
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-PS-batch-reader'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['output','reference']:p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--cross-prefix',required=True);p.add_argument('--run-native',action='store_true');p.add_argument('--captured-air',action='store_true')
    p.add_argument('--forward-control',action='store_true',help='finite actual CRC/shared-BRAM/SESSION/SOURCE RTL batch; not physical DMA or clock-age proof')
    main(p.parse_args())
