#!/usr/bin/env python3
"""Same captured ADC -> actual48RTL fixed8 FEC -> atomic record bridge.
No new RF emission. Captured input retains the existing bounded software
preprocessing. Delivery epoch/unknown time-coordinate flags are diagnostic;
physical DMA, PS ring ownership, production timebase and full G0 remain open.
"""
import argparse,collections,hashlib,json,os,shutil,struct,subprocess
from pathlib import Path
from fec_acquisition_probe import SOURCES
from source_record_bridge_probe import SOURCE
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
WRAPPER='record_acquiring_frontend_cost_probe.sv'

JOIN=r'''
reg [31:0] delivery_epoch=1;wire [63:0] bridge_data;wire [7:0] bridge_keep;wire bridge_valid,bridge_last;
reg bridge_ready=0;wire [31:0] bc,bd,bb,ba,bl,be,br;
source_record_bridge_cost_probe bridge(.clk(clk),.rst(rst),.fence(cancel),.epoch(delivery_epoch),
 .control_valid(1'b0),
 .source_valid(fsv),.source_ready(fsr),.source_data(fsd),.source_session(fss),.source_block(fsb),
 .source_index(fsi),.source_offset(fso),.source_last(fsl),.source_recovered(fsrec),.source_abort(fsab),
 .event_valid(fev),.event_ready(fer),.event_session(fes),.event_block(feb),.event_code(fec),.event_missing(fem),.event_delivered(fed),
 .m_axis_tdata(bridge_data),.m_axis_tkeep(bridge_keep),.m_axis_tvalid(bridge_valid),.m_axis_tready(bridge_ready),.m_axis_tlast(bridge_last),
 .sources_committed(bc),.sources_dropped(bd),.sources_bad(bb),.sources_aborted(ba),.sources_late_or_duplicate(bl),.events_dropped(be),.records_completed(br));
integer axis_fd,source_fd;reg bridge_held=0;reg [72:0] bridge_view;
initial begin axis_fd=$fopen("axis.txt","w");source_fd=$fopen("sources.txt","w");end
always @(negedge clk)bridge_ready=cycle%19>4;
always @(posedge clk)begin
 if(rst)begin delivery_epoch<=1;bridge_held=0;end
 else begin
  if(cancel||fsab)delivery_epoch<=delivery_epoch+1'b1;
  if(bridge_held&&(!bridge_valid||{bridge_data,bridge_keep,bridge_last}!==bridge_view))$fatal(1,"air record held AXIS changed");
  bridge_held=bridge_valid&&!bridge_ready;if(bridge_held)bridge_view={bridge_data,bridge_keep,bridge_last};
  if(bridge_valid&&bridge_ready)begin
   if(bridge_keep!==8'hff)$fatal(1,"air record keep");$fwrite(axis_fd,"%016h %0d\n",bridge_data,bridge_last);
  end
  if(fsv&&fsr&&fsl)$fwrite(source_fd,"%08h %08h %0d %0d\n",fss,fsb,fsi,fsrec);
 end
end
'''

def validate(out,session):
    records=[];data=bytearray()
    for line in (out/'axis.txt').read_text().splitlines():
        word,last=line.split();data.extend(int(word,16).to_bytes(8,'little'))
        if int(last):records.append(bytes(data));data.clear()
    assert not data,'partial AXIS record after finite drain'
    sources=[];restored=0;late=0;terminal=0
    for seq,record in enumerate(records):
        assert record[:4]==b'V3RX' and struct.unpack_from('<HH',record,4)==(1,64)
        size,kind,flags,sid,epoch=struct.unpack_from('<IHHII',record,8)
        rs,sample,capture=struct.unpack_from('<QQQ',record,24)
        block,index,payload,status,reserved=struct.unpack_from('<IHHII',record,48)
        assert size==len(record)==64+payload and rs==seq and sample==capture==reserved==0 and epoch==1
        assert sid==session and flags&6==6
        if kind==1:
            assert payload==512 and index<20 and status==0 and flags&~15==0
            expected=bytes((block*71+index*29+o*17+o//11+(block+1)*(o%13))&255 for o in range(512))
            assert record[64:]==expected,(block,index)
            recovered=flags&1;sources.append((sid,block,index,recovered));restored+=512*recovered;late+=bool(flags&8)
        else:
            assert kind==2 and payload==0 and index==65535
            terminal+=1
    upstream=[(int(s,16),int(b,16),int(i),int(r)) for s,b,i,r in (row.split() for row in (out/'sources.txt').read_text().splitlines())]
    assert collections.Counter(sources)==collections.Counter(upstream),'SOURCE loss/duplication across complete-record boundary'
    assert len(set((s,b,i) for s,b,i,r in sources))==len(sources)
    assert len(sources)>=136 and restored>=13312
    return {'AXIS_records':len(records),'SOURCE_records':len(sources),'exact_source_bytes':len(sources)*512,
            'restored_bytes':restored,'valid_sources_outside_reorder_window_retained':late,'terminal_ERASURE_records':terminal,
            'upstream_complete_SOURCE_census_equal':True,'no_partial_or_duplicate_SOURCE_records':True}

def main(a):
    v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
    before=disk_check('before-air-record-join');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
    try:
        previous=json.loads((ref/'receipt.json').read_text());assert previous['simulation'].startswith('G0_LIVE_AIR_FEC_PASS')
        for n in SOURCES:
            assert hashlib.sha256((v/'sim/g0'/n).read_bytes()).hexdigest()==previous['source_sha256'][n],n
            shutil.copyfile(v/'sim/g0'/n,out/n)
        shutil.copyfile(v/'sim/g0'/SOURCE,out/SOURCE)
        all_sources=[*SOURCES,SOURCE]
        if a.integrated_wrapper:
            shutil.copyfile(v/'sim/g0'/WRAPPER,out/WRAPPER);all_sources.append(WRAPPER)
        for path in ref.glob('*.hex'):os.link(path,out/path.name)
        tb=(ref/'tb.sv').read_text()
        old='reg fsr=1,fer=1;';assert tb.count(old)==1;tb=tb.replace(old,'wire fsr,fer;')
        join=JOIN
        if a.integrated_wrapper:
            tb=tb.replace('dut.','dut.core.')
            begin=tb.index('frame_acquiring_frontend_cost_probe #');end=tb.index(');',begin)+2
            tb=tb[:begin]+'''record_acquiring_frontend_cost_probe dut(
 .clk(clk),.rst(rst),.cancel(cancel),.rearm(rearm),.raw_valid(rv),.upstream_loss(1'b0),.raw_tag(tag),.raw_data(data),
 .search_valid(search),.search_ready(search_ready),.search_id(32'h80001000),
 .search_tag({16'h8011,BASE+48'd4}),.phase_origin(BASE-48'd@ORIGIN@),.ingress_running(running),
 .m_axis_tdata(bridge_data),.m_axis_tkeep(bridge_keep),.m_axis_tvalid(bridge_valid),.m_axis_tready(bridge_ready),.m_axis_tlast(bridge_last),
 .sources_committed(bc),.sources_dropped(bd),.sources_bad(bb),.sources_aborted(ba),.sources_late_or_duplicate(bl),.events_dropped(be),.records_completed(br));
'''.replace('@ORIGIN@',str(previous['input_slice'][0]))+tb[end:]
            begin=join.index('source_record_bridge_cost_probe bridge(');end=join.index(');',begin)+2
            join=join[:begin]+join[end:]
            join=join.replace('reg [31:0] delivery_epoch=1;','wire [31:0] delivery_epoch=dut.delivery_epoch;')
            join=join.replace('delivery_epoch<=1;bridge_held=0;','bridge_held=0;').replace("  if(cancel||fsab)delivery_epoch<=delivery_epoch+1'b1;",'')
            aliases={'fsv':'sv','fsr':'sr','fsd':'sd','fss':'ss','fsb':'sb','fsi':'si','fso':'so','fsl':'sl','fsrec':'srec','fsab':'sa','fev':'ev','fer':'er','fes':'es','feb':'eb','fec':'ec','fem':'em','fed':'ed'}
            join+='\n'+''.join(f'assign {alias}=dut.{name};\n' for alias,name in aliases.items())
            aliases={'packet_valid':'packet_valid','packet_tx':'packet_tx','packet_error':'packet_error','packet_header':'packet_header','packet_bytes':'packet_payload_bytes',
                     'packet_data_valid':'packet_data_valid','packet_last':'packet_data_last','packet_data':'packet_data','packet_index':'packet_data_index','frame_abort':'frame_abort',
                     'fpa':'fec_packets_accepted','fpe':'fec_packets_erased','fpu':'fec_packets_unmapped','fda':'fec_descriptors_accepted','fdr':'fec_descriptors_rejected','fbusy':'fec_range_busy','fdrop':'fec_events_dropped','fexp':'fec_expired_blocks'}
            join+=''.join(f'assign {alias}=dut.core.{name};\n' for alias,name in aliases.items())
            # Declare wide AXIS nets before the wrapper port connections;
            # otherwise Verilog first creates implicit one-bit wires there.
            split=join.index('integer axis_fd');declarations=join[:split];join=join[split:]
            tb=tb.replace('record_acquiring_frontend_cost_probe dut(',declarations+'\nrecord_acquiring_frontend_cost_probe dut(')
        old='always @(negedge clk)begin fsr=cycle%29>6;fer=cycle%17>3;end';assert tb.count(old)==1;tb=tb.replace(old,join)
        old=' $display("G0_LIVE_AIR_FEC_PASS';assert tb.count(old)==1
        end_check=''' if(bc!=records||bd||bb||ba||be||bridge.slot_state[0]!=0||bridge.slot_state[1]!=0||bridge.slot_state[2]!=0||bridge.state!=0)$fatal(1,"air record complete-commit census accepted%0d records%0d drops%0d bad%0d aborted%0d events%0d",bc,records,bd,bb,ba,be);
 $fclose(axis_fd);$fclose(source_fd);
 $display("G0_AIR_RECORD_JOIN_PASS complete_SOURCE_records=%0d source_bytes=%0d late_or_duplicate=%0d AXIS_records=%0d",bc,bc*512,bl,br);
'''
        if a.integrated_wrapper:end_check=end_check.replace('bridge.','dut.delivery.')
        tb=tb.replace(old,end_check+old)
        (out/'tb.sv').write_text(tb)
        def run(label,args):
            with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
        xpm=bp.parent/'data/ip/xpm'
        result={'scope':__doc__,'reference':str(ref.relative_to(v)),'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in all_sources},
                'integrated_wrapper':a.integrated_wrapper,'simulation':None,'routed_OOC':None,'g0_passed':False,'physical_DMA_or_full_board_video_passed':False}
        if not a.ooc_only:
            run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*all_sources,'tb.sv'])
            run('elaborate',[str(bp/'xelab'),'tb','-s','air_record_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'air_record_tb','-runall'])
            log=(out/'simulate.log').read_text();assert 'G0_AIR_RECORD_JOIN_PASS' in log and 'Fatal:' not in log,log[-6000:]
            result.update(simulation=next(s for s in log.splitlines() if s.startswith('G0_AIR_RECORD_JOIN_PASS')),
                          upstream_simulation=next(s for s in log.splitlines() if s.startswith('G0_LIVE_AIR_FEC_PASS')),independent_AXIS_validation=validate(out,a.session))
        if a.timing_reference:
            assert a.integrated_wrapper
            timing=a.timing_reference.resolve();text=(timing/'ooc.tcl').read_text().replace(str(timing),str(out))
            marker='synth_design -top frame_acquiring_frontend_cost_probe';assert text.count(marker)==1
            text=text.replace(marker,''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in [SOURCE,WRAPPER])+'synth_design -top record_acquiring_frontend_cost_probe')
            text=text.replace(' -generic ENABLE_FEC=1','').replace('NAME =~ frame/','NAME =~ *frame/')
            text=text.replace('close $fp','puts $fp "record_bridge_BRAM [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ delivery/*}]]"\nclose $fp')
            (out/'ooc.tcl').write_text(text)
            check=disk_check('before-air-record-wrapper-OOC');(out/'disk-before-ooc.json').write_text(json.dumps(check,indent=2)+'\n');assert check['disk']['available_bytes']>=5*GIB
            try:run('ooc',[str(bp/'vivado'),'-mode','batch','-source','ooc.tcl','-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out);result['memory_ownership_report']=(out/'memory-ownership.rpt').read_text()
            finally:(out/'disk-after-ooc.json').write_text(json.dumps(disk_check('after-air-record-wrapper-OOC'),indent=2)+'\n')
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-air-record-join'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--integrated-wrapper',action='store_true');p.add_argument('--ooc-only',action='store_true');p.add_argument('--timing-reference',type=Path)
    p.add_argument('--session',type=lambda x:int(x,0),default=0x56334601);main(p.parse_args())
