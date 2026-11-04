"""Read-only display register sampling; no RF/DMA controls are written."""
import mmap,os,struct,time,threading,_thread
MAPS={'encoder':(0x70e00000,{'reset':0x40,'status':0x5c}),'observer':(0x44a20000,{'id':0,'vsync':0x14,'eof_vsync':0x20,'beats':0x1c,'boundary_vsync':0x28,'error':0x38}),'HDMI_DMAC':(0x43000000,{'row_bytes_minus_1':0x418,'rows_minus_1':0x41c,'stride':0x424})}
class Sampler:
    def __init__(self):
        self.maps={};self.rows=[];self.errors=[];self.stop=threading.Event();self.fd=None
    def read(self):
        row={'monotonic_ns':time.monotonic_ns()}
        for name,(_,offsets) in MAPS.items():
            for key,off in offsets.items():row[name+'_'+key]=struct.unpack_from('<I',self.maps[name],off)[0]
        return row
    def start(self):
        self.fd=os.open('/dev/mem',os.O_RDONLY|os.O_SYNC)
        try:
            for name,(base,_) in MAPS.items():self.maps[name]=mmap.mmap(self.fd,4096,flags=mmap.MAP_SHARED,prot=mmap.PROT_READ,offset=base)
            assert self.read()['observer_id']==0x48504f31,'wrong display observer identity'
        except BaseException:self.close();raise
        def loop():
            try:
                while not self.stop.is_set():
                    row=self.read()
                    self.rows.append(row)
                    if row['observer_error']:
                        raise RuntimeError('display observer protocol/error flag')
                    if row['encoder_reset']&1 and row['observer_beats']==307200 and row['encoder_status']&0x18:
                        raise RuntimeError('display encoder FIFO underflow/overflow flag')
                    if len(self.rows)>10000:raise RuntimeError('display health bounded sample limit')
                    self.stop.wait(.010)
            except BaseException as error:
                self.errors.append(type(error).__name__+': '+str(error))
                _thread.interrupt_main()
        self.thread=threading.Thread(target=loop,daemon=True);self.thread.start()
    def close(self):
        self.stop.set()
        if hasattr(self,'thread'):self.thread.join(1)
        for m in self.maps.values():m.close()
        self.maps={}
        if self.fd is not None:os.close(self.fd);self.fd=None

def analyze(rows,errors=()):
    active=[x for x in rows if x['encoder_reset']&1 and x['observer_beats']==307200]
    changes=[];gaps=[];previous=None;intervals=[];max_between=0
    for row in active:
        if previous is not None:
            dt=row['monotonic_ns']-previous['monotonic_ns'];intervals.append(dt);max_between=max(max_between,dt)
        if not changes or row['observer_eof_vsync']!=changes[-1]['eof_vsync']:
            if changes:
                delta=(row['observer_eof_vsync']-changes[-1]['eof_vsync'])&0xffffffff
                if 1<delta<100:gaps.append({'from':changes[-1]['eof_vsync'],'to':row['observer_eof_vsync'],'missing_EOF_vsync_intervals':delta-1,'max_sample_gap_ns':max_between,'sampling_fast_enough':max_between<16000000})
            changes.append({'eof_vsync':row['observer_eof_vsync'],'at_ns':row['monotonic_ns']});max_between=0
        previous=row
    observed=[g for g in gaps if g['sampling_fast_enough']]
    return {'read_only':True,'RF_started':False,'samples':len(rows),'active_samples':len(active),'errors':list(errors),'distinct_memory_EOF_vsyncs_observed':len(changes),'observed_gaps':gaps,'confirmed_missing_EOF_vsync_intervals':sum(g['missing_EOF_vsync_intervals'] for g in observed),'maximum_active_sample_gap_ns':max(intervals,default=0),'encoder_status_OR':_or(x['encoder_status'] for x in active),'observer_error_OR':_or(x['observer_error'] for x in active),'inference_limit':'EOF/vsync gaps establish missing memory frame supply; not a pixel capture or proof of workqueue root cause'}
def _or(values):
    out=0
    for x in values:out|=x
    return out
