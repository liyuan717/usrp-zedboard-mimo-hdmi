#!/usr/bin/env python3
"""Actual48RTL ADC receiver with internal fixed8 FEC consumer.
Legacy real-capture regressions have no VRS3 descriptor and must drain without
opening blocks. New in-band RF fixtures are separately required for recovery.
"""
import argparse,hashlib,json,os,shutil,subprocess
from pathlib import Path
from link_acquisition_probe import SOURCES as BASE
from fec_inverse_cost_probe import routed_measurements
from workspace_storage_guard import check as disk_check,GIB
FEC=['fec_inverse_cost_probe.sv','fec_memory_cost_probe.sv','fec_stream_recovery_cost_probe.sv','link_fec_ingress_cost_probe.sv']
SOURCES=BASE+FEC
PORTS='''.fec_source_valid(fsv),.fec_source_ready(fsr),.fec_source_data(fsd),.fec_source_session(fss),.fec_source_block(fsb),.fec_source_index(fsi),.fec_source_offset(fso),.fec_source_last(fsl),.fec_source_recovered(fsrec),.fec_source_abort(fsab),.fec_event_valid(fev),.fec_event_ready(fer),.fec_event_session(fes),.fec_event_block(feb),.fec_event_code(fec),.fec_event_missing(fem),.fec_event_delivered(fed),.fec_packets_accepted(fpa),.fec_packets_erased(fpe),.fec_packets_unmapped(fpu),.fec_descriptors_accepted(fda),.fec_descriptors_rejected(fdr),.fec_range_busy(fbusy),.fec_events_dropped(fdrop),.fec_expired_blocks(fexp)'''
DECL='''
wire fsv,fsl,fsrec,fsab,fev;reg fsr=1,fer=1;wire [7:0] fsd;wire [31:0] fss,fsb,fes,feb,fpa,fpe,fpu,fda,fdr,fbusy,fdrop,fexp;
wire [4:0] fsi;wire [8:0] fso;wire [2:0] fec;wire [19:0] fem,fed;
'''

def main(a):
 project=Path(__file__).resolve().parents[1];out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);ref=a.reference.resolve();core=a.core_reference.resolve();timing=a.timing_reference.resolve();bp=a.vivado_bin.resolve()
 before=disk_check('before-live-fec');(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n');assert before['disk']['available_bytes']>=5*GIB
 try:
  previous=json.loads((ref/'receipt.json').read_text());assert previous['link_simulation'].startswith('G0_LIVE_LINK_ACQUISITION_PASS')
  for n,h in previous['source_sha256'].items():
   if n!='frame_acquiring_frontend_cost_probe.sv':assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==h,n
  current_core=json.loads((core/'receipt.json').read_text())
  for n in FEC:assert hashlib.sha256((project/'sim/g0'/n).read_bytes()).hexdigest()==current_core['source_sha256'][n]
  for p in ref.glob('*.hex'):os.link(p,out/p.name)
  for n in ['generator.hex','inverse.hex']:os.link(core/n,out/n)
  tb=(ref/'tb.sv').read_text()
  changes=[('reg lready=0,ldready=0;','wire lready=dut.packet_sink_ready,ldready=dut.packet_data_sink_ready;'),
   ('always @(negedge clk)begin lready=cycle%31>6;ldready=cycle%19>5;end',''),
   ('#(.ENABLE_PAYLOAD(1),.ENABLE_LINK(1)) dut(','#(.ENABLE_FEC(1)) dut('),
   ('.packet_ready(lready)',".packet_ready(1'b0)"),('.packet_data_ready(ldready)',".packet_data_ready(1'b0)"),
   ('frame_acquiring_frontend_cost_probe #',DECL+'\nframe_acquiring_frontend_cost_probe #'),
   ('.packet_data_last(ldlast));','.packet_data_last(ldlast),'+PORTS+');')]
  for old,new in changes:assert tb.count(old)==1,(old,tb.count(old));tb=tb.replace(old,new)
  marker='task run_samples';assert tb.count(marker)==1
  tb=tb.replace(marker,'''always @(posedge clk)if(!rst&&!cancel)begin
 if(fsv||fev||fda||fdr||fpa||fbusy||fdrop||fexp||dut.block_fec.ingress.bank_live)$fatal(1,"live FEC legacy fixture invented declared data");
end
'''+marker)
  marker=' $display("G0_LIVE_LINK_ACQUISITION_PASS';assert tb.count(marker)==1
  tb=tb.replace(marker,''' $display("G0_LIVE_FEC_LEGACY_PASS complete_frames=27 accepted_unmapped_packets=%0d crc_erasures=%0d exact_packet_payload_bytes=%0d source_bytes=0 undeclared_blocks=0",link_good,link_bad,link_checked);
'''+marker);(out/'tb.sv').write_text(tb)
  for n in SOURCES:shutil.copyfile(project/'sim/g0'/n,out/n)
  hashes={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in SOURCES}
  def run(label,args):
   with (out/(label+'.log')).open('w') as f:subprocess.run(args,cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
  result={'scope':__doc__,'source_sha256':hashes,'routed_OOC':None,'g0_passed':False}
  if not a.ooc_only:
   xpm=bp.parent/'data/ip/xpm';run('compile',[str(bp/'xvlog'),'--sv',str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),*SOURCES,'tb.sv']);run('elaborate',[str(bp/'xelab'),'tb','-s','live_fec_tb','-mt','off']);run('simulate',[str(bp/'xsim'),'live_fec_tb','-runall'])
   log=(out/'simulate.log').read_text();assert 'G0_LIVE_FEC_LEGACY_PASS' in log and 'Fatal:' not in log,log[-6000:]
   for k,v in [('fec_legacy_simulation','G0_LIVE_FEC_LEGACY_PASS'),('link_simulation','G0_LIVE_LINK_ACQUISITION_PASS'),('payload_simulation','G0_LIVE_PAYLOAD_ACQUISITION_PASS'),('frame_simulation','G0_LIVE_FRAME_ACQUISITION_PASS'),('header_simulation','G0_LIVE_HEADER_ACQUISITION_PASS'),('acquisition_simulation','G0_LIVE_TRACKING_ACQUISITION_PASS')]:result[k]=next(s for s in log.splitlines() if s.startswith(v))
   (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n')
  if not a.sim_only:
   before_ooc=disk_check('before-live-fec-OOC');(out/'disk-before-ooc.json').write_text(json.dumps(before_ooc,indent=2)+'\n');assert before_ooc['disk']['available_bytes']>=5*GIB
   try:
    tcl=(timing/'ooc.tcl').read_text().replace(str(timing),str(out));marker='synth_design -top';assert tcl.count(marker)==1
    tcl=tcl.replace(marker,''.join('read_verilog -sv {'+str(out/n)+'}\n' for n in FEC)+marker).replace('-generic {ENABLE_PAYLOAD=1 ENABLE_LINK=1}','-generic ENABLE_FEC=1')
    assert '-generic ENABLE_FEC=1' in tcl
    tcl=tcl.replace('close $fp','puts $fp "FEC_BRAM [llength [get_cells -hier -filter {REF_NAME =~ RAMB* && NAME =~ *block_fec*}]]"\nclose $fp')
    (out/'ooc.tcl').write_text(tcl);run('ooc',[str(bp/'vivado'),'-mode','batch','-source','ooc.tcl','-nolog','-nojournal']);result['routed_OOC']=routed_measurements(out);result['memory_ownership_report']=(out/'memory-ownership.rpt').read_text()
   finally:(out/'disk-after-ooc.json').write_text(json.dumps(disk_check('after-live-fec-OOC'),indent=2)+'\n')
  (out/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}),flush=True)
 finally:(out/'disk-after.json').write_text(json.dumps(disk_check('after-live-fec'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['output','reference','core-reference','timing-reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 m=p.add_mutually_exclusive_group();m.add_argument('--sim-only',action='store_true');m.add_argument('--ooc-only',action='store_true');main(p.parse_args())
