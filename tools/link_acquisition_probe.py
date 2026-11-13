#!/usr/bin/env python3
"""Actual ADC through44RTL -> dual CRC-validated packets, bounded payload bytes.
Retains all old numeric oracles/deadlines; replaces diagnostic header/ML consumers
with actual PL link consumers. Full FEC/DMA/board/video/G0 remain unproved.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from payload_acquisition_probe import SOURCES as BASE
from payload_link_probe import SOURCES as LINK
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
SOURCES=BASE+LINK
PORTS='''.packet_valid(lv),.packet_ready(lready),.packet_id(lid),.packet_context(lctx),.packet_generation(lgen),.packet_epoch(lep),.packet_tx(ltx),.packet_slot(lslot),.packet_error(lerror),.packet_header(lheader),.packet_payload_bytes(lsize),.packet_data_valid(ldv),.packet_data_ready(ldready),.packet_data(ld),.packet_data_index(ldi),.packet_data_last(ldlast)'''
DECL=r'''
reg lready=0,ldready=0;wire lv,ldv,ltx,ldlast;wire [31:0] lid,lctx,lgen;wire [15:0] lep,lsize,ldi;wire [2:0] lslot,lerror;wire [255:0] lheader;wire [7:0] ld;
reg [7:0] link_bytes[0:184127];reg [255:0] link_headers[0:335];reg [2:0] link_errors[0:335];
integer link_packets=0,link_good=0,link_bad=0,link_checked=0,link_holds=0,link_frame_packets=0,link_index=0,link_di=0,link_ref=0;
reg [15:0] link_seen;reg link_metadata=0,link_rheld=0,link_dheld=0,link_status_seen=0;reg [390:0] link_rview;reg [24:0] link_dview;
wire [390:0] link_view={lid,lctx,lgen,lep,ltx,lslot,lerror,lheader,lsize};
'''
LOGIC=r'''
always @(negedge clk)begin lready=cycle%31>6;ldready=cycle%19>5;end
always @(posedge clk)begin
 if(rst||cancel||fence||frame_abort)begin link_metadata=0;link_rheld=0;link_dheld=0;link_status_seen=0;end
 else begin
  if(dut.frame.cfg_valid&&dut.frame.cfg_ready)begin link_seen=0;link_frame_packets=0;link_metadata=0;end
  if(link_rheld&&(!lv||link_view!==link_rview))$fatal(1,"live link held packet changed");
  if(link_dheld&&(!ldv||{ldlast,ldi,ld}!==link_dview))$fatal(1,"live link held byte changed");
  if(lv)begin
   link_ref=(data_model*3+data_window-1)*16+ltx*8+lslot;
   if(data_window==4||{lid,lctx,lgen,lep}!=={data_owner,data_context,data_generation,data_epoch}||lheader!==link_headers[link_ref]||lerror!==link_errors[link_ref]||lsize!=(lerror==0?512:0))$fatal(1,"live link packet CRC/identity oracle model%0d window%0d TX%0d slot%0d",data_model,data_window,ltx,lslot);
   if(lready)begin
    if(link_metadata||link_seen[ltx*8+lslot])$fatal(1,"live link overlapping/duplicate packet");link_seen[ltx*8+lslot]=1;link_frame_packets=link_frame_packets+1;link_packets=link_packets+1;
    link_index=link_ref;link_di=0;link_metadata=lerror==0;
    if(lerror==0)link_good=link_good+1;else link_bad=link_bad+1;
   end
  end
  if(ldv)begin
   if(!link_metadata||ldi!=link_di||ld!==link_bytes[link_index*548+32+link_di]||ldlast!==(link_di==511))$fatal(1,"live link exact payload byte oracle packet%0d byte%0d",link_index,link_di);
   if(ldready)begin link_di=link_di+1;link_checked=link_checked+1;if(ldlast)link_metadata=0;end
  end
  if(svalid&&!link_status_seen)begin
   if(sterror==0&&(link_frame_packets!=16||link_seen!=16'hffff||link_metadata||!dut.link_done))$fatal(1,"live link early frame success");link_status_seen=1;
  end
  if(svalid&&sready)link_status_seen=0;
  link_rheld=lv&&!lready;if(link_rheld)begin link_rview=link_view;link_holds=link_holds+1;end
  link_dheld=ldv&&!ldready;if(link_dheld)begin link_dview={ldlast,ldi,ld};link_holds=link_holds+1;end
 end
end
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();numeric=a.link_reference.resolve();timing=a.timing_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-link');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
 if before['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage first.')
 try:
  old=json.loads((ref/'receipt.json').read_text());assert old['payload_simulation'].startswith('G0_LIVE_PAYLOAD_ACQUISITION_PASS')
  positive=json.loads((numeric/'receipt.json').read_text());assert positive['simulation'].startswith('G0_PAYLOAD_LINK_PASS')
  for n,h in positive['source_sha256'].items():assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==h
  for p in ref.glob('*.hex'):os.link(p,out/p.name)
  for name,target in [('packets.hex','link_bytes.hex'),('headers.hex','link_headers.hex'),('errors.hex','link_errors.hex')]:os.link(numeric/name,out/target)
  text=(ref/'tb.sv').read_text()
  old_hr='''always @(negedge clk)begin
 if(rst||cancel||header_fence_q||!header_valid_q)begin hr=0;header_wait=0;end
 else if(header_wait==37)hr=1;else begin hr=0;header_wait=header_wait+1;end
end
'''
  for src,dst in [('reg hr=0;','wire hr=dut.header_sink_ready;'),('reg dready=0;','wire dready=dut.payload_sink_ready;'),('always @(negedge clk)dready=cycle%23>4;',''),(old_hr,''),('#(.ENABLE_PAYLOAD(1)) dut(', '#(.ENABLE_PAYLOAD(1),.ENABLE_LINK(1)) dut('),('.header_ready(hr)',".header_ready(1'b0)"),('.payload_ready(dready)',".payload_ready(1'b0)"),('frame_acquiring_frontend_cost_probe #',DECL+'\nframe_acquiring_frontend_cost_probe #'),('.payload_metric(dmetric));','.payload_metric(dmetric),'+PORTS+');'),('task run_samples',LOGIC+'\ntask run_samples'),(' $readmemh("business_samples.hex",business_samples);',' $readmemh("link_bytes.hex",link_bytes);$readmemh("link_headers.hex",link_headers);$readmemh("link_errors.hex",link_errors);\n $readmemh("business_samples.hex",business_samples);')]:
   assert text.count(src)==1,(src,text.count(src));text=text.replace(src,dst)
  marker=' $display("G0_LIVE_PAYLOAD_ACQUISITION_PASS';assert text.count(marker)==1
  text=text.replace(marker,''' if(link_packets!=432||link_good!=402||link_bad!=30||link_checked!=402*512)$fatal(1,"live link global census %0d/%0d/%0d/%0d",link_packets,link_good,link_bad,link_checked);
 $display("G0_LIVE_LINK_ACQUISITION_PASS complete_frames=27 accepted_packets=%0d erased_packets=%0d exact_payload_bytes=%0d held_checks=%0d",link_good,link_bad,link_checked,link_holds);
'''+marker)
  (out/'tb.sv').write_text(text)
  for n in SOURCES:shutil.copyfile(project/'sim/g0'/n,out/n)
  hashes={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES}
  if a.prepare_only:
   (out/'preparation.json').write_text(json.dumps({'scope':'Prepared44RTL/input/TB ONLY; no functional or timing pass.','source_sha256':hashes,'g0_passed':False},indent=2)+'\n');print('Prepared44RTL only',flush=True);return
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  result={'scope':__doc__,'source_sha256':hashes,'routed_OOC':None,'g0_passed':False}
  if not a.ooc_only:
   xpm=bp.parent/'data/ip/xpm';run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*SOURCES,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','live_link_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'live_link_tb','-runall'])
   log=(out/'simulate.log').read_text();assert 'G0_LIVE_LINK_ACQUISITION_PASS' in log and 'Fatal:' not in log,log[-6000:]
   for k,v in [('link_simulation','G0_LIVE_LINK_ACQUISITION_PASS'),('payload_simulation','G0_LIVE_PAYLOAD_ACQUISITION_PASS'),('frame_simulation','G0_LIVE_FRAME_ACQUISITION_PASS'),('header_simulation','G0_LIVE_HEADER_ACQUISITION_PASS'),('acquisition_simulation','G0_LIVE_TRACKING_ACQUISITION_PASS')]:result[k]=next(s for s in log.splitlines() if s.startswith(v))
   (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  if not a.sim_only:
   before_ooc=disk_check('before-live-link-OOC');(out/'disk-before-ooc.json').write_text(json.dumps(before_ooc,indent=2)+'\n')
   if before_ooc['disk']['available_bytes']<5*GIB:raise SystemExit('Under5GiB: handle storage before OOC.')
   try:
    tcl=(timing/'ooc.tcl').read_text().replace(str(timing),str(out));marker='synth_design -top';assert tcl.count(marker)==1
    tcl=tcl.replace(marker,''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in LINK)+marker).replace('-generic ENABLE_PAYLOAD=1','-generic {ENABLE_PAYLOAD=1 ENABLE_LINK=1}')
    tcl=tcl.replace('close $fp','puts $fp "packet_payload_BRAM [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *link.packets/*}]]"\nclose $fp')
    (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source',str(out/'ooc.tcl'),'-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out);result['memory_ownership_report']=(out/'memory-ownership.rpt').read_text()
   finally:(out/'disk-after-ooc.json').write_text(json.dumps(disk_check('after-live-link-OOC'),indent=2)+'\n')
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-link'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for name in ('output','reference','link-reference','timing-reference','vivado-bin'):p.add_argument('--'+name,type=Path,required=True)
 mode=p.add_mutually_exclusive_group();mode.add_argument('--sim-only',action='store_true');mode.add_argument('--ooc-only',action='store_true');mode.add_argument('--prepare-only',action='store_true');main(p.parse_args())
