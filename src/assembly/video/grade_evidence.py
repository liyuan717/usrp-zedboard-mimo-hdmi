"""Host-only independent grading after RF decisions; never deployed to board."""
from pathlib import Path
import hashlib,json,struct,sys,zlib
HERE=Path(__file__).resolve().parent
D=HERE.parent
sys.path.insert(0,str(D/'resume6-candidate/runtime/python/tools'))
from read_rxonly_delivery import iter_delivery
from acceptance_gate import check
LEDGER=D/'live-baseline/tx-live02/tx-packets.jsonl'

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

def recover_aus(records):
    groups={}
    for record in records:
        for packet in record.packets:
            payload=packet[20:148]
            magic,version,flags,size,session,seq,total,offset,crc=struct.unpack_from('<4sBBHIIIII',payload)
            if magic!=b'RVF2' or version!=2 or session!=714 or seq not in range(10) or not 36<=total<=65536 or not 1<=size<=100 or offset+size>total:raise ValueError('RVF2 identity/length')
            if seq not in groups:groups[seq]=(bytearray(total),bytearray(total),crc)
            data,seen,expected_crc=groups[seq]
            if total!=len(data) or crc!=expected_crc:raise ValueError('RVF2 conflicting envelope')
            for j,value in enumerate(payload[28:28+size],offset):
                if seen[j] and data[j]!=value:raise ValueError('conflicting RVF2 duplicate')
                seen[j]=1;data[j]=value
    if sorted(groups)!=list(range(10)):raise ValueError('missing AU sequences')
    aus=[]
    for seq in range(10):
        data,seen,crc=groups[seq]
        if not all(seen) or zlib.crc32(data)!=crc:raise ValueError('incomplete or CRC-invalid AU')
        magic,version,codec,flags,source_seq,width,height,raw_size,size,crc,pts=struct.unpack_from('<4sBBHIHHIIIQ',data)
        h264=bytes(data[36:])
        if (magic,version,codec,source_seq,width,height)!=(b'ZV2D',2,2,seq,1024,600) or size!=len(h264) or raw_size!=1024*600*2 or zlib.crc32(h264)!=crc:raise ValueError('ZVID2 identity/size/CRC')
        aus.append({'sequence':seq,'flags':flags,'PTS_ms':pts,'H264_bytes':len(h264),'H264_sha256':hashlib.sha256(h264).hexdigest(),'h264':h264})
    return aus

def verify_source_aus(aus):
    ledger=D/'live-baseline/tx-live02/source-ledger.jsonl'
    if hashlib.sha256(ledger.read_bytes()).hexdigest()!='1a9bf67de5738c390a76e50e187e5f4f8e4905c80c4336801b9153d456a1b1c6':raise ValueError('source AU ledger pin')
    source=[json.loads(row) for row in ledger.read_text().splitlines()]
    if len(source)!=10 or len(aus)!=10:raise ValueError('source AU count')
    for au,wanted in zip(aus,source):
        if au['sequence']!=wanted['sequence'] or au['flags']!=wanted['flags'] or au['PTS_ms']!=wanted['source_timestamp_ms'] or au['H264_bytes']!=wanted['annexb_bytes'] or au['H264_sha256']!=wanted['annexb_sha256']:raise ValueError('source AU differs bytewise or metadata')
    if hashlib.sha256(b''.join(au['h264'] for au in aus)).hexdigest()!='1a5ee998aa5d7fcd1ef57d65ddfd949719151ecf0abfad3747543432feb7780b':raise ValueError('full encoded source pin')
    return [{k:v for k,v in au.items() if k!='h264'} for au in aus]

def grade(root):
    if hashlib.sha256(LEDGER.read_bytes()).hexdigest()!='8b4676fd82a3469a89ee79a7237d75d6ddbdd69c1a9bf84d892a3b8055b319bb':raise ValueError('TX grading ledger pin')
    expected={(x['frame_id'],x['stream']):bytes.fromhex(x['packet_hex']) for x in map(json.loads,LEDGER.read_text().splitlines())}
    adc=validate_adc(root/'new-adc-output.rxth',expected_blocks=2048)
    with (root/'received-live.rfd').open('rb') as stream:records=list(iter_delivery(stream))
    packet_count=compare_packets(records,expected)
    aus=verify_source_aus(recover_aus(records))
    board=json.loads((root/'result.json').read_text());video=json.loads((root/'video-report.json').read_text());proof=json.loads((root/'hdmi-proof.json').read_text())
    problems=check(board.get('native_summary',{}),video,proof,[r.frame_id for r in records])
    if problems:raise ValueError('; '.join(problems))
    if board.get('valid') is not True or board.get('fresh_RF_stage_completed') is not True or board.get('current_run_RFD_only') is not True:raise ValueError('fresh RF-to-display round not valid')
    ready=json.loads((root/'RF_READY.json').read_text())
    if ready['RFD_sha256']!=hashlib.sha256((root/'received-live.rfd').read_bytes()).hexdigest():raise ValueError('display source changed after fresh RF validation')
    pace=video.get('diagnostic_source_pacing',{})
    schedule=pace.get('schedule',[])
    if pace.get('scale')!=5 or [x['sequence'] for x in schedule]!=list(range(10)):raise ValueError('missing ten-frame paced playback')
    if schedule[-1]['released_monotonic_s']-schedule[0]['released_monotonic_s']<8.9:raise ValueError('short video played as a burst')
    health=board.get('health')
    if not isinstance(health,dict):raise ValueError('hold-only scanout health missing')
    if health.get('visual_only_not_sampled') is True:
        return {'valid':True,'ADC':adc,'CRC_correct_byte_identical_packets':packet_count,'unique_frames':len(set(r.frame_id for r in records)), 'AUs':aus,'physical_HDMI_boundary_verified':True,'scanout_sampler_omitted':True,'technical_scope':'visual-only isolation without /dev/mem scanout reads','visual_confirmation_pending':True,'continuous_realtime_verified':False}
    if health.get('errors'):raise ValueError('scanout sampler errors')
    if health.get('observer_error_OR')!=0:raise ValueError('display observer error')
    if health.get('maximum_active_sample_gap_ns',0)>=16666667:raise ValueError('scanout sampler exceeded one 60 Hz display interval')
    if health.get('confirmed_missing_EOF_vsync_intervals')!=0:raise ValueError('observed missing scanout EOF/VSYNC')
    return {'valid':True,'ADC':adc,'CRC_correct_byte_identical_packets':packet_count,'unique_frames':len(set(r.frame_id for r in records)), 'AUs':aus,'physical_HDMI_boundary_verified':True,'scanout_health':health,'visual_confirmation_pending':True,'continuous_realtime_verified':False}
