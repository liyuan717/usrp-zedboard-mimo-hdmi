"""Bounded fixed-profile stdin feeder; arm RX independently of the full IQ write.

The existing TX reader starts after streamer setup and pre-fills four IQB
records. Writing five complete records through the bounded stdin pipe proves
the reader is active, without waiting for the entire longer clip to drain.
No worker thread, shell input, RF parameter edits or receiver data fallback.
"""
import json,math,os,select,struct,time


def validate_actual(value):
    if value.get('serial')!='3095BA5' or value.get('configuration_matches_requested') is not True:
        raise ValueError('USRP identity/configuration mismatch')
    if value.get('clock_source')!='internal' or value.get('time_source')!='internal':
        raise ValueError('USRP clock/time mismatch')
    channels=value.get('channels',[])
    if len(channels)!=2 or [x.get('channel') for x in channels]!=[0,1]:
        raise ValueError('USRP dual-channel mapping mismatch')
    for channel in channels:
        for key,want,tolerance in [('sample_rate_hz',4000000,1),('frequency_hz',5500000000,1),
                                    ('bandwidth_hz',40000000,1),('gain_db',18,.01)]:
            got=channel.get(key)
            if type(got) not in (int,float) or not math.isfinite(got) or abs(got-want)>tolerance:
                raise ValueError('USRP actual '+key+' mismatch')
        if channel.get('lo_locked') is not True or channel.get('antenna')!='TX/RX':
            raise ValueError('USRP LO/antenna mismatch')


def prefill_watermark(stream):
    at=0
    for sequence in range(5):
        if at+16>len(stream):raise ValueError('IQB shorter than fixed prefill watermark')
        magic,seq,count=struct.unpack_from('<4sQI',stream,at)
        if magic!=b'IQB1' or seq!=sequence or count!=16384:
            raise ValueError('fixed prefill IQB record mismatch')
        at+=16+count*16
        if at>len(stream):raise ValueError('truncated prefill IQB data')
    return at


def feed_and_trigger(proc,stream,load_actual,trigger,check_stop,*,clock=time.monotonic,
                     sleep=time.sleep,writable=None,write=os.write):
    watermark=prefill_watermark(stream)
    if writable is None:writable=lambda f:bool(select.select([], [f], [], .005)[1])
    pending=memoryview(stream);start=clock();anchor=None;actual_seen=None;sent=0
    fired=None;input_done=None;closed=False
    try:
        while True:
            check_stop()
            now=clock()
            if now-start>40:raise TimeoutError('finite TX input/config/trigger deadline')
            if proc.poll() is not None:
                if pending or fired is None:raise RuntimeError('TX ended before complete input and RX trigger')
                break
            if actual_seen is None:
                try:value=load_actual()
                except (OSError,json.JSONDecodeError):value=None
                if value is not None:validate_actual(value);actual_seen=clock()
            if anchor is None and actual_seen is not None and sent>=watermark:anchor=clock()
            if anchor is not None and fired is None and clock()>=anchor+.65:
                trigger();fired=clock()
            if pending:
                if clock()-start>15:raise TimeoutError('finite IQ stdin deadline')
                if writable(proc.stdin):
                    chunk=pending[:65536]
                    try:count=write(proc.stdin.fileno(),chunk)
                    except BlockingIOError:count=None
                    if count is not None:
                        if not 0<count<=len(chunk):raise RuntimeError('invalid finite IQ write progress')
                        sent+=count;pending=pending[count:]
                        if not pending:
                            proc.stdin.close();closed=True;input_done=clock()
                else:sleep(.005)
            else:
                if not closed:proc.stdin.close();closed=True;input_done=clock()
                sleep(.005)
        return dict(configured_host_monotonic=actual_seen,prefill_written_host_monotonic=anchor,
                    prefill_watermark_bytes=watermark,triggered_host_monotonic=fired,
                    input_complete_host_monotonic=input_done,input_bytes=sent,
                    delay_seconds=.65,delay_anchor='five complete fixed IQB records + validated actual config',
                    RX_triggered_before_input_complete=fired<input_done)
    finally:
        if not closed:proc.stdin.close()
