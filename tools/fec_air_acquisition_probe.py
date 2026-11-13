#!/usr/bin/env python3
"""Fresh tagged RF ADC -> actual48RTL blind receiver -> in-band fixed8 FEC.
Only ADC/public constants/search arm enter DUT. TX source pattern is an OUTPUT
oracle. Software calibration/notch, R09/DMA/video and full G0 remain pending.
"""
import argparse,hashlib,json,os,shutil,subprocess,zlib
from pathlib import Path
import numpy as np
from g0_causal_acquisition import calibrated_input
from g0_fixed_differential_model import quantize_adc
from g0_packet_probe import PERIOD
from coherent_window_probe import pack
from fec_acquisition_probe import SOURCES,PORTS,DECL
from workspace_storage_guard import check as disk_check,GIB

TB=r'''`timescale 1ns/1ps
module tb;
reg clk=0;always #10 clk=~clk;reg rst=1,cancel=0,rearm=0,rv=0,search=0;
reg [63:0] tag=0,data=0;localparam [47:0] BASE=48'hfffffffeeaa8;
wire search_ready,running,frame_abort,packet_valid,packet_tx,packet_data_valid,packet_last;
wire [2:0] packet_error;wire [255:0] packet_header;wire [15:0] packet_bytes,packet_index;wire [7:0] packet_data;
@DECL@
frame_acquiring_frontend_cost_probe #(.ENABLE_FEC(1)) dut(
 .clk(clk),.rst(rst),.cancel(cancel),.rearm(rearm),.raw_valid(rv),.upstream_loss(1'b0),.raw_tag(tag),.raw_data(data),
 .search_valid(search),.search_ready(search_ready),.search_id(32'h80001000),.search_tag({16'h8011,BASE+48'd4}),.phase_origin(BASE-48'(@ORIGIN@)),
 .result_ready(1'b1),.normal_ready(1'b1),.header_ready(1'b0),.phase_ready(1'b0),.status_ready(1'b1),.payload_ready(1'b0),.diagnostic_ready(1'b0),.ingress_running(running),.frame_abort(frame_abort),
 .packet_valid(packet_valid),.packet_ready(1'b0),.packet_tx(packet_tx),.packet_error(packet_error),.packet_header(packet_header),.packet_payload_bytes(packet_bytes),
 .packet_data_valid(packet_data_valid),.packet_data_ready(1'b0),.packet_data(packet_data),.packet_data_index(packet_index),.packet_data_last(packet_last),
 @PORTS@);
reg [63:0] adc[0:@LAST@];integer cycle=0,k,checked=0,restored=0,records=0,events=0,full_blocks=0,CRC_good=0,CRC_bad=0,TX0=0,TX1=0,held=0;
integer positions[0:63][0:19];reg [19:0] seen[0:63],originals[0:63];reg terminal[0:63];integer max_age=0,opened[0:7];
reg source_held=0,event_held=0;reg [87:0] source_view;reg [106:0] event_view;
wire [87:0] current_source={fss,fsb,fsi,fso,fsl,fsrec,fsd};wire [106:0] current_event={fes,feb,fec,fem,fed};
function automatic [7:0] expected(input [31:0] b,input [4:0] s,input [8:0] o);
 reg [63:0] n;begin n={32'd0,b}*71+{59'd0,s}*29+{55'd0,o}*17+o/11+({32'd0,b}+1)*(o%13);expected=n[7:0];end
endfunction
always @(negedge clk)begin fsr=cycle%29>6;fer=cycle%17>3;end
always @(posedge clk)begin
 cycle=cycle+1;
 if(!rst)begin
  if(dut.receiver.reader_conflict)$fatal(1,"air FEC shared history owner conflict");
  if(source_held&&!fsab&&(!fsv||current_source!==source_view))$fatal(1,"air FEC held SOURCE changed");
  if(event_held&&(!fev||current_event!==event_view))$fatal(1,"air FEC held event changed");
  if(fsab)$fatal(1,"air FEC unexpected SOURCE abort under bounded output ready");
  if(packet_valid&&dut.packet_sink_ready)begin
   if(packet_tx)TX1=TX1+1;else TX0=TX0+1;
   if(packet_error==0)CRC_good=CRC_good+1;else CRC_bad=CRC_bad+1;
  end
  if(dut.block_fec.ingress.fec.open_valid&&dut.block_fec.ingress.fec.open_ready)
   opened[dut.block_fec.ingress.fec.open_bank]=cycle;
  if(dut.block_fec.ingress.fec.commit_packet&&dut.block_fec.ingress.fec.rx_index<20)begin
   if(dut.block_fec.ingress.fec.rx_job>=64)$fatal(1,"air FEC unexpected committed block");
   originals[dut.block_fec.ingress.fec.rx_job][dut.block_fec.ingress.fec.rx_index]=1;
  end
  if(fsv&&fsr)begin
   if(fss!=32'h@SESSION@||fsb>=64||fsi>=20||terminal[fsb]||seen[fsb][fsi]||fso!=positions[fsb][fsi]||fsd!==expected(fsb,fsi,fso)||fsl!==(fso==511)||fsrec!==!originals[fsb][fsi])$fatal(1,"air FEC source truth/identity oracle session%h block%0d row%0d byte%0d",fss,fsb,fsi,fso);
   checked=checked+1;positions[fsb][fsi]=positions[fsb][fsi]+1;if(fsrec)restored=restored+1;
   if(opened[fsb[2:0]]<0)$fatal(1,"air FEC source without an open owner");
   if(cycle-opened[fsb[2:0]]-1>max_age)max_age=cycle-opened[fsb[2:0]]-1;
   if(fsl)begin seen[fsb][fsi]=1;records=records+1;end
  end
  if(fev&&fer)begin
   if(fes!=32'h@SESSION@||feb>=64||terminal[feb]||fed!==seen[feb]||fem!==~seen[feb]||fec==4||fec==6)$fatal(1,"air FEC terminal ownership/missing oracle block%0d code%0d",feb,fec);
   terminal[feb]=1;events=events+1;if(fec==0)begin if(fed!=20'hfffff)$fatal(1,"air FEC incomplete success");full_blocks=full_blocks+1;end
   $display("AIR_FEC_BLOCK block=%0d code=%0d delivered=%h missing=%h",feb,fec,fed,fem);
  end
  source_held=fsv&&!fsr;if(source_held)begin source_view=current_source;held=held+1;end
  event_held=fev&&!fer;if(event_held)begin event_view=current_event;held=held+1;end
 end else begin
  source_held=0;event_held=0;
  for(integer b=0;b<8;b=b+1)opened[b]=-1;
 end
end
initial begin
 $readmemh("adc.hex",adc);for(integer b=0;b<64;b=b+1)begin seen[b]=0;originals[b]=0;terminal[b]=0;for(integer s=0;s<20;s=s+1)positions[b][s]=0;end
 repeat(5)@(negedge clk);rst=0;rearm=1;@(negedge clk);rearm=0;while(!running)@(negedge clk);
 search=1;@(posedge clk);while(!search_ready)@(posedge clk);@(negedge clk);search=0;
 for(k=0;k<=@LAST@;k=k+1)begin
  tag={16'h8011,BASE+48'(k)};data=adc[k];rv=1;@(negedge clk);rv=0;repeat(k%2?12:11)@(negedge clk);
 end
 repeat(1000000)@(negedge clk);
 if(checked<10240||restored<2048||full_blocks<1||!fda||!CRC_bad||!TX0||!TX1||fbusy||fdrop)$fatal(1,"air FEC insufficient real restored data bytes%0d restored%0d blocks%0d descriptors%0d TX%0d/%0d",checked,restored,full_blocks,fda,TX0,TX1);
 $display("G0_LIVE_AIR_FEC_PASS ADC=@RECORDS@ checked_source_bytes=%0d restored_bytes=%0d whole_SOURCE_records=%0d complete_blocks=%0d terminal_events=%0d CRC_good=%0d CRC_erased=%0d TX0_packets=%0d TX1_packets=%0d descriptions=%0d max_SOURCE_bank_age_cycles=%0d held_checks=%0d",checked,restored,records,full_blocks,events,CRC_good,CRC_bad,TX0,TX1,fda,max_age,held);$finish;
end
initial begin #2000000000;$fatal(1,"air FEC global timeout input%0d cycle%0d",k,cycle);end
endmodule
'''

def main(a):
 v=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);cap=a.capture_directory.resolve();ref=a.reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-air-FEC');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 try:
  receipt=json.loads((cap/'receipt.json').read_text());assert receipt['completed'] and not receipt['FMCOMMS3_TX_enabled'];path=cap/'adc.raw';assert f'{zlib.crc32(path.read_bytes()):08x}'==receipt['capture']['crc32']
  raw=np.memmap(path,dtype=np.dtype([('tag','<u8'),('iq','<i2',(4,))]),mode='r');assert np.all(np.diff(raw['tag'])==1)
  iq=np.asarray(raw['iq'],dtype=np.float64);y,calibration=calibrated_input(iq[:,0::2]+1j*iq[:,1::2],PERIOD);origin=PERIOD*4+2048+1600-4;adc,quant=quantize_adc(y[origin:])
  (out/'adc.hex').write_text(''.join(f'{pack(row,16):016x}\n' for row in adc))
  positive=json.loads((ref/'receipt.json').read_text());hashes=positive['source_sha256'];assert len(hashes)==48
  for n in SOURCES:assert hashlib.sha256((v/'sim/g0'/n).read_bytes()).hexdigest()==hashes[n];shutil.copyfile(ref/n,out/n)
  for n in ['coherent_signs.hex','generator.hex','inverse.hex','rrc.hex','sine.hex','training_signs.hex']:os.link(ref/n,out/n)
  text=TB.replace('@DECL@',DECL).replace('@PORTS@',PORTS).replace('@ORIGIN@',str(origin)).replace('@LAST@',str(len(adc)-1)).replace('@SESSION@',f'{a.session:08x}').replace('@RECORDS@',str(len(adc)));(out/'tb.sv').write_text(text)
  meta={'scope':__doc__,'capture':str(path.relative_to(v)),'capture_crc32':receipt['capture']['crc32'],'capture_records':len(raw),'input_slice':[origin,len(raw)],'input_ADC_records':len(adc),'calibration':calibration,'quantization_error':float(quant),'not_unchanged_raw_ADC':True,'only_ADC_public_constants_and_search_arm_enter_DUT':True,'source_gold_only_in_output_monitor':True,'source_sha256':hashes,'g0_passed':False};(out/'preparation.json').write_text(json.dumps(meta,indent=2)+'\n')
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  xpm=bp.parent/'data/ip/xpm';run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*SOURCES,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','live_air_fec_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'live_air_fec_tb','-runall'])
  log=(out/'simulate.log').read_text();assert 'G0_LIVE_AIR_FEC_PASS' in log and 'Fatal:' not in log,log[-6000:];meta['simulation']=next(x for x in log.splitlines() if x.startswith('G0_LIVE_AIR_FEC_PASS'));meta['block_results']=[x for x in log.splitlines() if x.startswith('AIR_FEC_BLOCK')];(out/'receipt.json').write_text(json.dumps(meta,indent=2)+'\n');print(json.dumps(meta),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-air-FEC'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','capture-directory','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--session',type=lambda x:int(x,0),default=0x56334601);main(p.parse_args())
