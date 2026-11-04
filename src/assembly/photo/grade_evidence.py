"""Host-only independent grading after RF decisions; never deployed to board."""
from pathlib import Path
import hashlib,json,struct,sys,zlib
HERE=Path(__file__).resolve().parent
D=HERE.parent
sys.path.insert(0,str(D/'resume6-candidate/runtime/python/tools'))
from read_rxonly_delivery import iter_delivery
from acceptance_gate import check
LEDGER=HERE/'tx-photo-offline/tx-packets.jsonl'

def validate_adc(path,expected_blocks=1024):
    raw=path.read_bytes()
    if len(raw)!=expected_blocks*8224:raise ValueError('ADC exact length')
    previous=None
    for index in range(expected_blocks):
        block=raw[index*8224:(index+1)*8224]
        magic,version,size,flags,epoch,seq,first,count,crc=struct.unpack_from('<4sBBHIIQII',block)
        if magic!=b'RXTH' or version!=1 or size!=32 or count!=1024 or zlib.crc32(block[:28])!=crc:raise ValueError('ADC header/CRC')
        if not epoch or not(flags==1 or index==0 and flags in (0x11,0x19) and seq==0):raise ValueError('ADC fault flags/epoch')
        if previous is not None and (epoch!=previous[0] or seq!=previous[1]+1 or first!=previous[2]+1024):raise ValueError('ADC discontinuity')
        if any(value&15 for value in block[32::2]):raise ValueError('ADC MSB12 alignment')
        previous=epoch,seq,first
    return {'blocks':expected_blocks,'all_headers_CRC_flags_continuity_alignment_valid':True,'sha256':hashlib.sha256(raw).hexdigest()}

def compare_packets(records,expected):
    for record in records:
        if record.rejected or record.build_id!=1:raise ValueError('rejected packet/build')
        for lane,packet in enumerate(record.packets):
            if packet!=expected[(record.frame_id,lane)]:raise ValueError('packet differs from independently pinned TX ledger')
    return len(records)*2

def recover_au(records):
    data=bytearray(8200);seen=bytearray(8200)
    for record in records:
        for packet in record.packets:
            payload=packet[20:148]
            magic,version,flags,size,session,seq,total,offset,crc=struct.unpack_from('<4sBBHIIIII',payload)
            if (magic,version,session,seq,total)!=(b'RVF2',2,714,0,8200) or not 1<=size<=100 or offset+size>total:raise ValueError('RVF2 scope/length')
            piece=payload[28:28+size]
            for j,value in enumerate(piece,offset):
                if seen[j] and data[j]!=value:raise ValueError('conflicting RVF2 duplicate')
                seen[j]=1;data[j]=value
    if not all(seen):raise ValueError('incomplete AU fragments')
    if zlib.crc32(data)!=crc:raise ValueError('RVF2 envelope CRC')
    magic,version,codec,flags,seq,width,height,raw_size,size,crc,pts=struct.unpack_from('<4sBBHIHHIIIQ',data)
    h264=data[36:]
    if (magic,version,codec,flags,seq,width,height,size)!=(b'ZV2D',2,2,7,0,1024,600,8164) or zlib.crc32(h264)!=crc:raise ValueError('ZVID2 identity/CRC')
    digest=hashlib.sha256(h264).hexdigest()
    if digest!='79e8b08ee3fb89ed525117ac1409ac65e77a4109b271a31d564fc6644a9fdd14':raise ValueError('source H264 AU byte mismatch')
    return {'source_session':714,'source_sequence':0,'H264_bytes':len(h264),'H264_sha256':digest,'source_AU_byte_identical':True}

def grade(root):
    if hashlib.sha256(LEDGER.read_bytes()).hexdigest()!='4a1536dc775703b69a756b968048a0bcb34950d3295e6dccf982b20d0289960e':raise ValueError('TX grading ledger pin')
    expected={(x['frame_id'],x['stream']):bytes.fromhex(x['packet_hex']) for x in map(json.loads,LEDGER.read_text().splitlines())}
    adc=validate_adc(root/'new-adc-output.rxth',expected_blocks=2048)
    with (root/'received-live.rfd').open('rb') as stream:records=list(iter_delivery(stream))
    packet_count=compare_packets(records,expected)
    au=recover_au(records)
    board=json.loads((root/'result.json').read_text());video=json.loads((root/'video-report.json').read_text());proof=json.loads((root/'hdmi-proof.json').read_text())
    problems=check(board.get('native_summary',{}),video,proof,[r.frame_id for r in records])
    if problems:raise ValueError('; '.join(problems))
    if board.get('valid') is not True:raise ValueError('board round not valid')
    health=board.get('health')
    if not isinstance(health,dict):raise ValueError('hold-only scanout health missing')
    if health.get('visual_only_not_sampled') is True:
        return {'valid':True,'ADC':adc,'CRC_correct_byte_identical_packets':packet_count,'unique_frames':len(set(r.frame_id for r in records)), 'AU':au,'physical_HDMI_boundary_verified':True,'scanout_sampler_omitted':True,'technical_scope':'visual-only isolation without /dev/mem scanout reads','visual_confirmation_pending':True,'continuous_realtime_verified':False}
    if health.get('errors'):raise ValueError('scanout sampler errors')
    if health.get('observer_error_OR')!=0:raise ValueError('display observer error')
    if health.get('maximum_active_sample_gap_ns',0)>=16666667:raise ValueError('scanout sampler exceeded one 60 Hz display interval')
    if health.get('confirmed_missing_EOF_vsync_intervals')!=0:raise ValueError('observed missing scanout EOF/VSYNC')
    return {'valid':True,'ADC':adc,'CRC_correct_byte_identical_packets':packet_count,'unique_frames':len(set(r.frame_id for r in records)), 'AU':au,'physical_HDMI_boundary_verified':True,'scanout_health':health,'visual_confirmation_pending':True,'continuous_realtime_verified':False}
