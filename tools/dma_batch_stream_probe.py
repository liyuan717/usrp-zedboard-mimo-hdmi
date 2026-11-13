#!/usr/bin/env python3
"""Validate bounded whole-record DMA batches using actual receiver output plus faults."""
import argparse,hashlib,json,shutil,struct,subprocess
from pathlib import Path
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
SOURCE='dma_batch_stream_cost_probe.sv'

def validate_dma_replacement(baseline_sha256,current,proof_path):
    proof=json.loads(proof_path.read_text())
    actual=hashlib.sha256(current.read_bytes()).hexdigest()
    assert actual!=baseline_sha256 and actual==proof['source_sha256']
    controls=proof['independent_validation']
    assert controls['all_record_bytes_equal'] and controls['record_cross_batch']==0
    assert controls['maximum_511_record_prefix_passed'] and controls['malformed_drop_timeout_then_reentry']
    assert controls['STOP_carry_dropped_without_extra_DMA_batch']
    return dict(name=SOURCE,baseline_sha256=baseline_sha256,compiled_sha256=actual,
                proof=str(proof_path.resolve()),bounded_dma_encoding_unchanged=True)

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,run=0,iv=0,il=0,ready=0;
reg [63:0] idata=0;reg [7:0] ik=8'hff;
wire ir,ov,ol,busy;wire [63:0] od;wire [7:0] ok;
wire [31:0] batches,records,rejected,timeouts;
dma_batch_stream_cost_probe dut(clk,rst,run,idata,ik,iv,ir,il,od,ok,ov,ready,ol,busy,batches,records,rejected,timeouts);
reg [63:0] words[0:@LAST@];integer offsets[0:@RECORDS@],sources[0:56];
integer cycle=0,phase=1,fd,held=0,which,start,before_batches,ready_mode=1;
reg hold_valid=0;reg [72:0] hold_value;
always @(negedge clk)ready=ready_mode&&(cycle%19>4);
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst)begin
  if(hold_valid&&(!ov||{od,ok,ol}!==hold_value))$fatal(1,"batch held AXIS changed");
  if(ov&&ready)begin
   if(ok!==8'hff)$fatal(1,"batch keep");$fwrite(fd,"%0d %016h %0d\n",phase,od,ol);
  end
  hold_valid=ov&&!ready;if(hold_valid)begin hold_value={od,ok,ol};held=held+1;end
 end else hold_valid=0;
end
task send_record(input integer record_id,mode);
 integer count;begin
  count=offsets[record_id+1]-offsets[record_id];if(mode>=2)count=11;
  for(integer p=0;p<count;p=p+1)begin
   idata=words[offsets[record_id]+p];if((mode==1||mode==4)&&p==0)idata=idata^64'd1;
   il=(p==count-1)&&(mode<3);iv=1;
   @(posedge clk);while(!ir)@(posedge clk);@(negedge clk);iv=0;
  end
 end
endtask
task drain;
 begin start=cycle;while(busy)begin @(negedge clk);if(cycle-start>200000)$fatal(1,"batch drain exceeded bound");end repeat(2)@(negedge clk);end
endtask
task await_batch;
 begin start=cycle;while(batches==before_batches)begin @(negedge clk);if(cycle-start>100000)$fatal(1,"low-rate batch timeout");end run=0;drain();end
endtask
initial begin
 $readmemh("words.hex",words);$readmemh("offsets.hex",offsets);$readmemh("sources.hex",sources);fd=$fopen("batches.txt","w");
 repeat(5)@(negedge clk);rst=0;run=1;
 for(which=0;which<@RECORDS@;which=which+1)send_record(which,0);run=0;drain();
 phase=2;before_batches=batches;run=1;await_batch();
 phase=3;before_batches=batches;run=1;send_record(0,0);await_batch();
 phase=4;run=1;send_record(@ERASURE@,0);send_record(0,1);run=0;drain();
 phase=5;run=1;send_record(1,0);send_record(0,2);run=0;drain();
 phase=6;before_batches=batches;run=1;send_record(0,3);await_batch();
 phase=7;run=1;
 fork
  begin for(integer p=0;p<57;p=p+1)send_record(sources[p],0);end
  begin start=cycle;while(!dut.carry_record)begin @(negedge clk);if(cycle-start>30000)$fatal(1,"carry not exercised");end
   ready_mode=0;repeat(3)@(negedge clk);run=0;repeat(3)@(negedge clk);ready_mode=1;end
 join
 drain();
 phase=8;run=1;send_record(2,0);run=0;drain();
 phase=9;run=1;for(which=0;which<511;which=which+1)send_record(@ERASURE@,0);run=0;drain();
 phase=10;run=1;send_record(0,4);run=0;drain();
 phase=11;run=1;send_record(2,0);run=0;drain();
 if(records!=@EXPECTED_RECORDS@||rejected!=5||timeouts!=2||batches!=13||held==0)$fatal(1,"batch final census records%0d rejected%0d timeouts%0d batches%0d",records,rejected,timeouts,batches);
 $fclose(fd);$display("G0_DMA_BATCH_STREAM_PASS records=%0d rejected=%0d input_timeouts=%0d batches=%0d held_checks=%0d",records,rejected,timeouts,batches,held);$finish;
end
initial begin #20000000;$fatal(1,"batch global timeout state%0d phase%0d",dut.state,phase);end
endmodule
'''

def read_records(path):
    records=[];data=bytearray()
    for line in path.read_text().splitlines():
        word,last=line.split();data.extend(int(word,16).to_bytes(8,'little'))
        if int(last):records.append(bytes(data));data.clear()
    assert not data
    return records

def decode_batch(data,sequence):
    assert len(data)==32768,'batch length differs from descriptor'
    assert struct.unpack_from('<4sHHIIQ',data)==(b'V3DB',1,32,32768,0,sequence)
    assert data[24:32]==bytes(8)
    magic,valid,count,flags,seq,reserved,rejected=struct.unpack_from('<4sIIIQII',data,32736)
    assert magic==b'V3DE' and seq==sequence and reserved==0
    assert valid<=32704 and valid%8==0
    records=[];position=32;end=32+valid
    while position<end:
        assert position+64<=end and data[position:position+4]==b'V3RX','invalid/truncated record in committed prefix'
        major,header,size=struct.unpack_from('<HHI',data,position+4)
        assert major==1 and header==64 and 64<=size<=576 and size%8==0
        assert position+size<=end,'SOURCE crossed DMA batch boundary'
        records.append(data[position:position+size]);position+=size
    assert position==end and len(records)==count
    if not flags&1:assert data[end:32736]==bytes(32736-end),'uncommitted padding is nonzero'
    return records,{'valid_bytes':valid,'records':count,'flags':flags,'total_rejected':rejected}

def inspect(out,original):
    batch=[];all_batches=[];phase_batches={};phase=None
    for line in (out/'batches.txt').read_text().splitlines():
        p,word,last=line.split();p=int(p)
        if phase is None:phase=p
        assert phase==p,'test phase changed inside a batch'
        batch.extend(int(word,16).to_bytes(8,'little'))
        if int(last):
            records,meta=decode_batch(bytes(batch),len(all_batches));all_batches.append(meta)
            phase_batches.setdefault(p,[]).append((records,meta));batch=[];phase=None
    assert not batch
    source_indices=[i for i,r in enumerate(original) if struct.unpack_from('<H',r,12)[0]==1]
    erasure=next(r for r in original if struct.unpack_from('<H',r,12)[0]==2)
    expected={1:original,2:[],3:[original[0]],4:[erasure],5:[original[1]],6:[],
              7:[original[i] for i in source_indices[:56]],8:[original[2]],
              9:[erasure]*511,10:[],11:[original[2]]}
    for p,records in expected.items():assert [r for rs,m in phase_batches[p] for r in rs]==records,('batch record oracle',p)
    assert len(phase_batches[1])==3 and all(len(phase_batches[p])==1 for p in range(2,12))
    assert phase_batches[2][0][1]['flags']&2 and phase_batches[3][0][1]['flags']&2
    assert all(phase_batches[p][0][1]['flags']&1 for p in [4,5,6])
    assert phase_batches[6][0][1]['flags']&2
    assert phase_batches[9][0][1]['valid_bytes']==32704
    assert phase_batches[10][0][1]['flags']&3==3
    return {'batches':len(all_batches),'whole_records':sum(len(x) for x in expected.values()),
            'original_receiver_records_exact':len(original),'all_record_bytes_equal':True,'record_cross_batch':0,
            'empty_and_low_rate_timeout_closed':True,'bad_record_preserves_previous_valid_prefix':True,
            'truncated_record_timeout_then_reentry':True,'STOP_carry_dropped_without_extra_DMA_batch':True,
            'maximum_511_record_prefix_passed':True,'malformed_drop_timeout_then_reentry':True,'batch_metadata':all_batches}

def main(a):
    v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();bp=a.vivado_bin.resolve()
    before=disk_check('before-DMA-batch-stream');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
    try:
        receipt=json.loads((ref/'receipt.json').read_text());assert receipt['independent_AXIS_validation']['upstream_complete_SOURCE_census_equal']
        original=read_records(ref/'axis.txt');assert len(original)==142
        flat=b''.join(original);offsets=[0]
        for record in original:offsets.append(offsets[-1]+len(record)//8)
        (out/'words.hex').write_text(''.join(f'{int.from_bytes(flat[p:p+8],"little"):016x}\n' for p in range(0,len(flat),8)))
        (out/'offsets.hex').write_text(''.join(f'{x:08x}\n' for x in offsets))
        indices=[i for i,r in enumerate(original) if struct.unpack_from('<H',r,12)[0]==1][:57]
        (out/'sources.hex').write_text(''.join(f'{i:08x}\n' for i in indices))
        tb=TB.replace('@LAST@',str(len(flat)//8-1)).replace('@RECORDS@',str(len(original))).replace('@ERASURE@',str(next(i for i,r in enumerate(original) if struct.unpack_from('<H',r,12)[0]==2))).replace('@EXPECTED_RECORDS@',str(len(original)+60+511+1))
        (out/'tb.sv').write_text(tb);shutil.copyfile(v/'sim/g0'/SOURCE,out/SOURCE)
        def run(label,args):
            with (out/(label+'.log')).open('w') as log:subprocess.run(args,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
        run('compile',[str(bp/'xvlog'),'--sv',SOURCE,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','batch_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'batch_tb','-runall'])
        log=(out/'simulate.log').read_text();assert 'G0_DMA_BATCH_STREAM_PASS' in log and 'Fatal:' not in log,log[-4000:]
        result={'scope':__doc__,'reference':str(ref.relative_to(v)),'source_sha256':hashlib.sha256((out/SOURCE).read_bytes()).hexdigest(),'simulation':next(s for s in log.splitlines() if s.startswith('G0_DMA_BATCH_STREAM_PASS')),'independent_validation':inspect(out,original),'routed_OOC':None,'physical_DMA_or_G0_passed':False}
        if not a.sim_only:
            c=disk_check('before-DMA-batch-stream-OOC');(out/'disk-before-ooc.json').write_text(json.dumps(c,indent=2)+'\n');assert c['disk']['available_bytes']>=5*GIB
            try:
                (out/'ooc.tcl').write_text('set_param general.maxThreads 4\nread_verilog -sv {'+str(out/SOURCE)+'}\n'+'''synth_design -top dma_batch_stream_cost_probe -part xc7z020clg484-1 -mode out_of_context
create_clock -name processing -period 20 [get_ports clk]
set_input_delay 2 -clock processing [get_ports -filter {DIRECTION == IN && NAME != clk}]
set_output_delay 2 -clock processing [get_ports -filter {DIRECTION == OUT}]
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_utilization -hierarchical -file hierarchy.rpt
report_timing_summary -file timing.rpt -report_unconstrained
report_drc -file drc.rpt
write_checkpoint -force batch.dcp
''')
                run('ooc',[str(bp/'vivado'),'-mode','batch','-source','ooc.tcl','-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out)
            finally:(out/'disk-after-ooc.json').write_text(json.dumps(disk_check('after-DMA-batch-stream-OOC'),indent=2)+'\n')
        (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-DMA-batch-stream'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for n in ['output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
    p.add_argument('--sim-only',action='store_true');main(p.parse_args())
