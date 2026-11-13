#!/usr/bin/env python3
"""Controls on saved physical record bytes; never an RF/capacity measurement."""
import argparse,json,struct
from pathlib import Path
from g0_source_compact import batches,HEADER
from dma_batch_stream_probe import decode_batch
from review_g0_source_long import review, rolling_service

def main(a):
    out=a.output.resolve();out.mkdir(exist_ok=False);raw=list(batches(a.capture))
    # Both grid offsets retain two symbols. Between 1 and 10 ms, a
    # five-second window has lost the first and has not gained the last.
    offsets=rolling_service([1_000_000,500_000_000,5_010_000_000],0,5_020_000_000)
    assert offsets['5']['minimum_grid_unique_Bps']==204.8
    assert offsets['5']['minimum_all_offsets_unique_Bps']==102.4
    source=None;location=None;terminal=None
    for seq,(_,data) in enumerate(raw):
        records,_=decode_batch(data,seq);pos=32
        for record in records:
            if source is None and struct.unpack_from('<H',record,12)[0]==1:
                source=record;location=(seq,pos)
            if terminal is None and struct.unpack_from('<H',record,12)[0]==2:
                terminal=(seq,pos)
            pos+=len(record)
    assert source is not None
    def save(name,change,omit=None,repeat=None,extend_last=False):
        path=out/(name+'.v3bc')
        with path.open('wb') as f:
            f.write(HEADER)
            for seq,(timestamp,data) in enumerate(raw):
                if seq==omit:continue
                if extend_last and seq==len(raw)-1:timestamp+=40_000_000_000
                data=change(seq,bytearray(data));valid=struct.unpack_from('<I',data,32740)[0]
                compactable=data[:4]==b'V3DB' and data[-32:-28]==b'V3DE' and valid<=32704 and valid%8==0
                body=data[:32+valid]+data[-32:] if compactable else data
                f.write(struct.pack('<IIQ',len(body),0,timestamp));f.write(body)
                if seq==repeat:f.write(struct.pack('<IIQ',len(body),0,timestamp));f.write(body)
        result=review(path,25,0x56334601,4)
        (out/(name+'.json')).write_text(json.dumps(result,indent=2)+'\n')
        assert not result['full_G0_RF_capacity_or_video_passed']
        window=result['fixed_steady_window']
        if extend_last:
            assert window['unique_Bps']==0 and window['five_second_unique_Bps']==[0]*6
            assert window['minimum_five_second_unique_Bps']==0
            rolling=result['rolling_service_in_fixed_steady_window']
            assert rolling['1']=={'window_count':1451,'minimum_grid_unique_Bps':0,'minimum_all_offsets_unique_Bps':0}
            assert rolling['5']=={'window_count':1251,'minimum_grid_unique_Bps':0,'minimum_all_offsets_unique_Bps':0}
            assert rolling['maximum_gap_between_SOURCE_bearing_batches_ns'] is None
        else:
            assert window['unique_Bps'] is None and window['five_second_unique_Bps']==[]
            assert window['minimum_five_second_unique_Bps'] is None
            assert result['rolling_service_in_fixed_steady_window'] is None
        return result
    clean=save('clean',lambda seq,data:data)
    assert clean['SOURCE_unique']==86 and clean['record_crc32']=='fa15bfad'
    assert clean['SOURCE_restored_unique_bytes']==9216 and clean['offered_SOURCE_symbols']==220
    assert clean['direct_unique_SOURCE_by_TX']=={'0':41,'1':27}
    def duplicate(seq,data):
        if seq==len(raw)-1:
            assert struct.unpack_from('<I',data,32740)[0]==0
            data[32:32+len(source)]=source
            struct.pack_into('<II',data,32740,len(source),1)
        return data
    dup=save('duplicate',duplicate)
    assert dup['SOURCE_unique']==86 and dup['counts']['duplicate_SOURCE']==1
    def mutate(offset,value):
        def change(seq,data):
            if seq==location[0]:data[location[1]+offset]^=value
            return data
        return change
    damaged=save('bad-payload',mutate(64,1))
    assert damaged['SOURCE_unique']==85 and damaged['counts']['bad_SOURCE_payload']==1
    identity=save('bad-session',mutate(16,1))
    assert identity['SOURCE_unique']==85 and identity['counts']['bad_SOURCE_identity']==1
    assert terminal is not None
    def bad_terminal(seq,data):
        if seq==terminal[0]:data[terminal[1]+59]|=0x80
        return data
    invalid_terminal=save('bad-FEC-terminal',bad_terminal)
    assert invalid_terminal['SOURCE_unique']==86 and invalid_terminal['counts']['bad_FEC_terminal_record']==1
    def bad_batch(seq,data):
        if seq==0:data[0]^=1
        return data
    broken=save('bad-first-batch',bad_batch)
    assert broken['SOURCE_unique']==86 and broken['counts']['bad_batches']==1
    gap=save('missing-batch',lambda seq,data:data,omit=0)
    assert gap['SOURCE_unique']==86 and gap['counts']['missing_batches']==1
    repeated=save('duplicate-batch',lambda seq,data:data,repeat=0)
    assert repeated['SOURCE_unique']==86 and repeated['counts']['duplicate_batches']==1
    burst=save('short-burst-long-idle',lambda seq,data:data,extend_last=True)
    assert burst['SOURCE_unique']==86 and burst['fixed_steady_window']['covered_by_observation']
    result={'saved_physical_record_bytes_controls_passed':True,
            'cases':['all_offsets_between_grid_starts','clean','duplicate','bad-payload','bad-session','bad-FEC-terminal',
                     'bad-first-batch','missing-batch','duplicate-batch','short-burst-long-idle'],
            'duplicate_does_not_increase_unique_bytes':True,
            'local_bad_records_and_batches_do_not_stop_later_counting':True,
            'timestamps_are_offline_fixture_values_not_RF_time':True,
            'actual_RF_or_capacity_passed':False}
    (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--capture',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);main(p.parse_args())
