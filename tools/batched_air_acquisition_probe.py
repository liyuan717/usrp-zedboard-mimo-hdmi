#!/usr/bin/env python3
"""Captured ADC -> fixed8 receiver/records -> bounded DMA batches (G0 candidate).
Original48RTL RF/FEC arithmetic is unchanged; SOURCE admission is explicitly
8192cycles after measured256/2048-cycle losses; worst-phase coupled wait4679cycles. This is not physical DMA/video.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from dma_batch_stream_probe import decode_batch,read_records,SOURCE
from record_air_acquisition_probe import validate as validate_records
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
TOP='batched_acquiring_frontend_cost_probe.sv'

MONITOR=r'''
integer batch_fd,admission_stall=0,max_admission_stall=0;reg batch_held=0;reg [72:0] batch_view;
initial batch_fd=$fopen("dma-batches.txt","w");
always @(negedge clk)batch_ready=cycle%19>4;
assign bridge_data=dut.record_data;assign bridge_keep=dut.record_keep;
assign bridge_valid=dut.record_valid;assign bridge_ready=dut.record_ready;assign bridge_last=dut.record_last;
always @(posedge clk)begin
 if(rst)batch_held=0;else begin
  if(fsv&&!fsr)begin admission_stall=admission_stall+1;if(admission_stall>max_admission_stall)max_admission_stall=admission_stall;if(admission_stall>8192)$fatal(1,"ADC batch admission not bounded");end else admission_stall=0;
  if(batch_held&&(!batch_valid||{batch_data,batch_keep,batch_last}!==batch_view))$fatal(1,"ADC batch held beat changed");
  batch_held=batch_valid&&!batch_ready;if(batch_held)batch_view={batch_data,batch_keep,batch_last};
  if(batch_valid&&batch_ready)begin
   if(batch_keep!==8'hff)$fatal(1,"ADC batch keep");$fwrite(batch_fd,"%016h %0d\n",batch_data,batch_last);
  end
 end
end
'''

def check_batches(out):
    records=[];data=bytearray();metadata=[];byte_count=0
    for row in (out/'dma-batches.txt').read_text().splitlines():
        word,last=row.split();data.extend(int(word,16).to_bytes(8,'little'));byte_count+=8
        if int(last):
            rs,meta=decode_batch(bytes(data),len(metadata));assert not meta['flags']&1,'actual ADC record malformed inside batch'
            records.extend(rs);metadata.append(meta);data.clear()
    assert not data
    upstream=read_records(out/'axis.txt');assert records==upstream,'record loss/duplication/change between receiver and committed DMA prefixes'
    return {'fixed_batches':len(metadata),'DMA_bytes_including_padding':byte_count,'valid_record_bytes':sum(m['valid_bytes'] for m in metadata),
            'whole_records':len(records),'empty_heartbeat_batches':sum(not m['records'] for m in metadata),
            'record_cross_batch':0,'exact_record_census_equal':True,'all_committed_record_bytes_equal':True,
            'first_batch':metadata[0],'last_batch':metadata[-1]}

def main(a):
    v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
    check=disk_check('before-ADC-DMA-batch');(out/'disk-before.json').write_text(json.dumps(check,indent=2)+'\n');assert check['disk']['available_bytes']>=5*GIB
    try:
        previous=json.loads((ref/'receipt.json').read_text());assert previous['integrated_wrapper'] and previous['simulation'].startswith('G0_AIR_RECORD_JOIN_PASS')
        names=[*previous['source_sha256'],SOURCE,TOP];assert len(names)==52
        for n in names:
            p=v/'sim/g0'/n
            if n in previous['source_sha256']:
                if n=='source_record_bridge_cost_probe.sv':
                    # Only the explicitly measured admission-bound change is
                    # allowed relative to the immutable previous50RTL probe.
                    expected=(ref/n).read_text().replace('at most 256 cycles','at most 8192 cycles').replace('ADMISSION_WAIT_CYCLES=256','ADMISSION_WAIT_CYCLES=8192')
                    assert p.read_text()==expected,n
                else:assert hashlib.sha256(p.read_bytes()).hexdigest()==previous['source_sha256'][n],n
            shutil.copyfile(p,out/n)
        for p in ref.glob('*.hex'):os.link(p,out/p.name)
        tb=(ref/'tb.sv').read_text().replace('dut.','dut.core.')
        replacements=[('reg bridge_ready=0;','wire bridge_ready;'),('always @(negedge clk)bridge_ready=cycle%19>4;',''),
                      ('record_acquiring_frontend_cost_probe dut(','batched_acquiring_frontend_cost_probe dut('),
                      ('.rearm(rearm)', '.rearm(rearm),.run(batch_run)'),
                      ('.m_axis_tdata(bridge_data)','.m_axis_tdata(batch_data)'),('.m_axis_tkeep(bridge_keep)','.m_axis_tkeep(batch_keep)'),
                      ('.m_axis_tvalid(bridge_valid)','.m_axis_tvalid(batch_valid)'),('.m_axis_tready(bridge_ready)','.m_axis_tready(batch_ready)'),('.m_axis_tlast(bridge_last)','.m_axis_tlast(batch_last)'),
                      ('.records_completed(br));','.records_completed(br),.batch_busy(batch_busy),.batches_completed(bt_completed),.batch_records(bt_records),.batch_rejected(bt_rejected),.batch_input_timeouts(bt_timeouts));')]
        for old,new in replacements:assert tb.count(old)==1,(old,tb.count(old));tb=tb.replace(old,new)
        marker='batched_acquiring_frontend_cost_probe dut(';assert tb.count(marker)==1
        tb=tb.replace(marker,'''wire [63:0] batch_data;wire [7:0] batch_keep;wire batch_valid,batch_last,batch_busy;
reg batch_run=1,batch_ready=0;wire [31:0] bt_completed,bt_records,bt_rejected,bt_timeouts;
'''+marker)
        marker='function automatic [7:0] expected';assert tb.count(marker)==1;tb=tb.replace(marker,MONITOR+'\n'+marker)
        marker=' if(bc!=records';assert tb.count(marker)==1
        tb=tb.replace(marker,''' batch_run=0;for(integer drain=0;batch_busy;drain=drain+1)begin @(negedge clk);if(drain>100000)$fatal(1,"ADC batch stop bound");end
 if(bt_records!=br||bt_rejected||bt_timeouts||!bt_completed)$fatal(1,"ADC batching altered full record census records%0d/%0d reject%0d timeout%0d",bt_records,br,bt_rejected,bt_timeouts);
 $fclose(batch_fd);
'''+marker)
        marker=' $display("G0_AIR_RECORD_JOIN_PASS';assert tb.count(marker)==1
        tb=tb.replace(marker,' $display("G0_ADC_DMA_BATCH_PASS whole_records=%0d batches=%0d rejected=%0d input_timeouts=%0d max_SOURCE_admission_stall=%0d",bt_records,bt_completed,bt_rejected,bt_timeouts,max_admission_stall);\n'+marker)
        (out/'tb.sv').write_text(tb)
        def run(label,args):
            with (out/(label+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
        result={'scope':__doc__,'source_sha256':{n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in names},'simulation':None,'routed_OOC':None,'physical_DMA_or_real_video_or_G0_passed':False}
        if not a.ooc_only:
            xpm=bp.parent/'data/ip/xpm'
            run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*names,'tb.sv'])
            run('elaborate',[str(bp/'xelab'),'tb','-s','adc_batch_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'adc_batch_tb','-runall'])
            log=(out/'simulate.log').read_text();assert 'G0_ADC_DMA_BATCH_PASS' in log and 'Fatal:' not in log,log[-6000:]
            result.update(simulation=next(s for s in log.splitlines() if s.startswith('G0_ADC_DMA_BATCH_PASS')),
                          upstream_simulation=next(s for s in log.splitlines() if s.startswith('G0_LIVE_AIR_FEC_PASS')),
                          independent_SOURCE_validation=validate_records(out,a.session),independent_batch_validation=check_batches(out))
        if a.timing_reference:
            timing=a.timing_reference.resolve();text=(timing/'ooc.tcl').read_text().replace(str(timing),str(out))
            marker='synth_design -top record_acquiring_frontend_cost_probe';assert text.count(marker)==1
            text=text.replace(marker,''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in [SOURCE,TOP])+'synth_design -top batched_acquiring_frontend_cost_probe')
            text=text.replace('NAME =~ delivery/*','NAME =~ *delivery/*').replace('close $fp','puts $fp "batch_BRAM [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ batch/*}]]"\nclose $fp')
            (out/'ooc.tcl').write_text(text)
            c=disk_check('before-ADC-DMA-batch-OOC');(out/'disk-before-ooc.json').write_text(json.dumps(c,indent=2)+'\n');assert c['disk']['available_bytes']>=5*GIB
            try:run('ooc',[str(bp/'vivado'),'-mode','batch','-source','ooc.tcl','-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out);result['memory_ownership_report']=(out/'memory-ownership.rpt').read_text()
            finally:(out/'disk-after-ooc.json').write_text(json.dumps(disk_check('after-ADC-DMA-batch-OOC'),indent=2)+'\n')
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-ADC-DMA-batch'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--timing-reference',type=Path);p.add_argument('--ooc-only',action='store_true');p.add_argument('--session',type=lambda s:int(s,0),default=0x56334601);main(p.parse_args())
