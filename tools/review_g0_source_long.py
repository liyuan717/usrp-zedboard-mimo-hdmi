#!/usr/bin/env python3
"""Independent bounded SOURCE evidence review. Never opens hardware or signs full G0."""
import argparse,collections,json,struct,zlib
from bisect import bisect_left
from pathlib import Path
from g0_source_compact import batches
from dma_batch_stream_probe import decode_batch

def rolling_service(arrivals,begin,end):
    """Review grid and exact worst offsets using DMA consumer time."""
    result={}
    for seconds in (1,5):
        width=seconds*1_000_000_000
        def received(t):
            return 512*(bisect_left(arrivals,t+width)-bisect_left(arrivals,t))
        windows=[received(t) for t in range(begin,end-width+1,20_000_000)]
        # A half-open window can lose bytes only when its start advances
        # beyond a SOURCE arrival. Check all such changes plus the start.
        departures={t+1 for t in arrivals if begin<=t<end-width}
        worst=min((received(t) for t in departures),default=windows[0])
        result[str(seconds)]={'window_count':len(windows),
                              'minimum_grid_unique_Bps':min(windows)/seconds,
                              'minimum_all_offsets_unique_Bps':min(worst,windows[0])/seconds}
    times=sorted({t for t in arrivals if begin<=t<end})
    result['maximum_gap_between_SOURCE_bearing_batches_ns']=max(
        (b-a for a,b in zip(times,times[1:])),default=None)
    return result


def review(path,frames,session,erase=0,video_source=None,coded_lanes_swapped=False):
    if not 8<=frames<=(3200 if video_source is not None else 3000) or not 0<=erase<=12:
        raise ValueError('finite waveform bounds differ')
    counts=collections.Counter();unique={};direct=collections.Counter();record_crc=0
    first=last=None;source_first=None;errors=[];restored=0;previous=None
    non_source_crc=0
    terminal_codes=collections.Counter()
    outside_offer_codes=collections.Counter()
    deadline_offered_missing_bits=0
    terminal_examples=[]
    if video_source is not None:
        if erase or not 0<len(video_source)<=1200*20*512 or len(video_source)%(20*512):
            raise ValueError('Expected bounded whole original video SOURCE blocks')
        offered_ordinals=len(video_source)//(20*512)*32
    else:
        offered_ordinals=frames*14
    def offered_in_block(block):
        return min(20,max(0,offered_ordinals-block*32))
    for sequence,(arrival,data) in enumerate(batches(path)):
        if first is None:first=arrival
        last=arrival;counts['batches']+=1
        batch_sequence=struct.unpack_from('<Q',data,16)[0]
        try:records,meta=decode_batch(data,batch_sequence)
        except AssertionError as error:
            counts['bad_batches']+=1
            if len(errors)<16:errors.append({'batch':sequence,'error':str(error)})
            continue
        if previous is not None:
            delta=(batch_sequence-previous)&0xffffffffffffffff
            if not delta:counts['duplicate_batches']+=1;continue
            if delta>>63:counts['late_batches']+=1;continue
            counts['missing_batches']+=delta-1
        else:counts['missing_batches']+=batch_sequence
        previous=batch_sequence
        counts['damaged_batches']+=bool(meta['flags']&1)
        counts['rejected_records_reported_by_PL']+=meta['total_rejected']
        for record in records:
            counts['records']+=1;record_crc=zlib.crc32(record,record_crc)
            kind,flags=struct.unpack_from('<HH',record,12)
            counts['kind_'+str(kind) if 1<=kind<=5 else 'unknown_kind']+=1
            if kind!=1:non_source_crc=zlib.crc32(record,non_source_crc)
            if kind==2:
                sid=struct.unpack_from('<I',record,16)[0]
                block,index,payload,status=struct.unpack_from('<IHHI',record,48)
                if len(record)!=64 or sid!=session or index!=65535 or payload or status>>23:
                    counts['bad_FEC_terminal_record']+=1
                    continue
                code=status>>20
                offered=offered_in_block(block)
                offered_missing=(status&((1<<offered)-1)).bit_count()
                if code and len(terminal_examples)<16:
                    terminal_examples.append({'block':block,'code':code,
                        'source_symbols_offered':offered,'reported_missing_offered_bits':offered_missing})
                if offered:
                    terminal_codes[code]+=1
                    if code==5:
                        deadline_offered_missing_bits+=offered_missing
                else:
                    outside_offer_codes[code]+=1
            if kind!=1:continue
            received_session=struct.unpack_from('<I',record,16)[0]
            block,index=struct.unpack_from('<IH',record,48)
            # Do not let corrupt identities grow the set or credit data outside
            # the offered unique waveform. The final partial block is bounded.
            if received_session!=session or index>=offered_in_block(block):
                counts['bad_SOURCE_identity']+=1;continue
            ordinal=block*20+index
            expected=(video_source[ordinal*512:(ordinal+1)*512] if video_source is not None else
                      bytes((block*71+index*29+o*17+o//11+(block+1)*(o%13))&255 for o in range(512)))
            if len(record)!=576 or record[64:]!=expected:
                counts['bad_SOURCE_payload']+=1;continue
            key=(block,index)
            if key in unique:counts['duplicate_SOURCE']+=1;continue
            unique[key]=arrival
            if source_first is None:source_first=arrival
            if flags&1:restored+=512
            else:
                # The frame slot is coded ordinal modulo14 +2. Its parity
                # identifies logical IQ, which the RF probe may remap to a port.
                if index<erase:counts['direct_SOURCE_from_intentionally_bad_CRC']+=1
                direct[((block*32+index)%2)^coded_lanes_swapped]+=1
    span=(last-first)/1e9 if first is not None else 0
    # Predeclared fixed window, never search for the best-looking interval.
    # Acquisition timestamps measure user-space delivery, not air/source age.
    window_start=source_first+5_000_000_000 if source_first is not None else None
    window_end=window_start+30_000_000_000 if window_start is not None else None
    covered=window_end is not None and last>=window_end
    subwindow_bytes=[0]*6 if covered else []
    if covered:
        for t in unique.values():
            if window_start<=t<window_end:
                subwindow_bytes[(t-window_start)//5_000_000_000]+=512
    window_bytes=sum(subwindow_bytes)
    complete_blocks,tail=divmod(offered_ordinals,32)
    all_offered=complete_blocks*20+min(20,tail)
    delivered_by_block=collections.Counter(block for block,_ in unique)
    missing_by_block={}
    for block in range(complete_blocks+bool(tail)):
        missing=offered_in_block(block)-delivered_by_block[block]
        if missing:missing_by_block[block]=missing
    source_bytes=512*len(unique);offered_duration=frames*.02
    result={'scope':__doc__,'batches':counts['batches'],'records':counts['records'],
            'counts':dict(counts),'first_arrival_ns':first,'last_arrival_ns':last,
            'observation_span_seconds':span,'SOURCE_unique':len(unique),
            'SOURCE_unique_bytes_exact':source_bytes,'SOURCE_restored_unique_bytes':restored,
            'direct_unique_SOURCE_by_TX':{'0':direct[0],'1':direct[1]},
            'offered_SOURCE_symbols':all_offered,'not_delivered_SOURCE_symbols':all_offered-len(unique),
            'missing_SOURCE_blocks':{'count':len(missing_by_block),
                'first_examples':dict(sorted(missing_by_block.items())[:16])},
            'FEC_terminal_observations':{'codes_in_offered_blocks':dict(sorted(terminal_codes.items())),
                'codes_outside_offered_blocks':dict(sorted(outside_offer_codes.items())),
                'deadline_reported_missing_offered_bits':deadline_offered_missing_bits,
                'nonzero_status_examples':terminal_examples},
            'record_crc32':f'{record_crc:08x}','bounded_error_examples':errors,
            'whole_observation_unique_Bps':source_bytes/span if span else None,
            'unique_bytes_divided_by_offered_duration_Bps':source_bytes/offered_duration,
            'offered_duration_rate_is_not_delivery_window_capacity':True,
            'fixed_steady_window':{'rule':'first valid SOURCE observation +5s, then30s; no best-window search',
                                  'covered_by_observation':covered,'begin_ns':window_start,'end_ns':window_end,
                                  'unique_bytes':window_bytes,'unique_Bps':window_bytes/30 if covered else None,
                                  'five_second_unique_Bps':[n/5 for n in subwindow_bytes],
                                  'minimum_five_second_unique_Bps':min(subwindow_bytes)/5 if covered else None},
            'rolling_service_in_fixed_steady_window':rolling_service(
                list(unique.values()),window_start,window_end) if covered else None,
            'required_reliable_SOURCE_Bps_for_current_video':149504/.7,
            'full_G0_RF_capacity_or_video_passed':False,
            'limits':['Host-side post-capture byte/identity verification only; no hardware/boot/TX/EOB proof is inferred.',
                      'A short capture or zero SOURCE cannot produce a30s capacity estimate.',
                      'CRC/FEC events are not global PHY PER. Consumer timestamps are not RF/source timestamps.',
                      'Terminal records describe upstream FEC, not DMA delivery. Deadline bits are observed reports, not a global loss census.']}

    if video_source is not None:
        result['non_SOURCE_record_crc32']=f'{non_source_crc:08x}'
        result['video_missing_SOURCE_ordinals']=[i for i in range(all_offered) if divmod(i,20) not in unique]
        result['required_reliable_SOURCE_Bps_for_current_video']=None
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--capture',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--frames',type=int,default=3000)
    p.add_argument('--session',type=lambda s:int(s,0),default=0x56334601)
    p.add_argument('--erase-sources',type=int,default=0);a=p.parse_args()
    if a.output.exists():raise FileExistsError(a.output)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    result=review(a.capture,a.frames,a.session,a.erase_sources)
    a.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
