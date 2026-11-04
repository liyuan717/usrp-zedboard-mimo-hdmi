import copy,json,struct,zlib,sys
from pathlib import Path
from types import SimpleNamespace
import acceptance_gate as gate
import grade_evidence as g

def main():
    n={'captured_blocks':2048,'crc_rejected_frames':0,'detected_frames':42,'delivery':{'ipc_enqueued_records':42,'ipc_dropped_records':0}}
    v={'valid':True,'accepted':[{'accepted_payload_crc32':'12345678'}],'terminal':{'stop_ack':True,'helper_returncode':0,'presenter_returncode':0,'pending':0,'workers_alive':[],'session_state':'closed'},'diagnostics':{'helper_stderr':'CODEC_PROFILE_V1 {"frames":1}'}}
    p={'physical':True,'acceptance_valid':True,'capture_status':'complete','presented_at_hdmi_boundary':1,'scanout_repeats':600,'proof_failures':0,'backend_failures':0,'failed_closed':False,'capture_invalidated':False,'records':[{'token':1,'drm_sequence':1,'observer_session':1,'source_session_id':714,'source_frame_id':0,'payload_crc32':0x12345678}]}
    ids=[1]+list(range(1,42));assert not gate.check(n,v,p,ids)
    cases=[]
    for target,key,value in [('native','crc_rejected_frames',1),('native','captured_blocks',2047),('video','valid',False),('proof','physical',False),('proof','presented_at_hdmi_boundary',0),('proof','backend_failures',1),('proof','scanout_repeats',99)]:
        nn,vv,pp=copy.deepcopy((n,v,p));{'native':nn,'video':vv,'proof':pp}[target][key]=value
        assert gate.check(nn,vv,pp,ids);cases.append(target+':'+key)
    for key,val in [('token',0),('source_frame_id',1),('payload_crc32',0)]:
        pp=copy.deepcopy(p);pp['records'][0][key]=val;assert gate.check(n,v,pp,ids);cases.append('proof:'+key)
    assert gate.check(n,v,p,ids[:-1]);cases.append('missing RF frame')
    ledger={(x['frame_id'],x['stream']):bytes.fromhex(x['packet_hex']) for x in map(json.loads,g.LEDGER.read_text().splitlines())}
    records=[SimpleNamespace(frame_id=k,build_id=1,rejected=False,packets=tuple(ledger[k,s] for s in (0,1))) for k in ids]
    assert g.compare_packets(records,ledger)==84
    au=g.recover_au(records);assert au['H264_bytes']==8164
    bad=copy.deepcopy(records);pkt=bytearray(bad[0].packets[0]);pkt[50]^=1;bad[0].packets=(bytes(pkt),bad[0].packets[1])
    try:g.compare_packets(bad,ledger)
    except ValueError:cases.append('payload corruption')
    else:raise AssertionError('payload corruption accepted')
    raw=g.D/'live-resume6-20261027/gain18i-dup1-20261027/board-evidence/new-adc-output.rxth'
    adc=g.validate_adc(raw)
    Path(__file__).with_name('GATE-TEST.json').write_text(json.dumps({'pass':True,'positive_gate':True,'negative_cases':cases,'ledger_AU_reassembly':au,'independent_historical_ADC':adc},indent=2)+'\n')
    print('gate positives/negatives, exact AU recovery, independent ADC validation passed')
if __name__=='__main__':main()
