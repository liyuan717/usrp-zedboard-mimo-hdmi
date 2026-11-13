#!/usr/bin/env python3
"""Prepare isolated fixed8 raw ADC/PL DC/SOURCE platform, replacing raw recorder.

Retains physical tagged WFIFO/CPACK, guard, AD9361 and locked HDMI. Not a
production profile, SD installation, fullG0 or physical RF acceptance.
"""
import argparse,hashlib,json,re,shutil,subprocess
from pathlib import Path
from prepare_g0_adc_platform import prepare
from workspace_storage_guard import check,GIB
from correlation_implementation_probe import validate_replacement
from redundancy_diagnostic_audit import trimmed
from dma_batch_stream_probe import SOURCE as DMA_BATCH_SOURCE,validate_dma_replacement
from fec_deadline_proof import NAME as FEC_STREAM_SOURCE,validate_deadline_replacement

FORWARD_CONTROL_SOURCES = {
 'frame_acquiring_frontend_cost_probe.sv',
 'link_fec_ingress_cost_probe.sv',
 'record_acquiring_frontend_cost_probe.sv',
 'source_record_bridge_cost_probe.sv',
}
BRIDGE_NAME = 'g0_source_receiver.v'

def review(out):
 info=json.loads((out/'source-inputs.json').read_text())
 log=(out/'build.log').read_text()
 assert 'G0_SOURCE_SYNTHESIS_COMPLETE_REVIEW_CDC_BEFORE_ROUTE' in log
 synth=(out/'project/receiver_v2.runs/synth_1/runme.log').read_text()
 critical=[s for s in synth.splitlines() if 'CRITICAL WARNING:' in s or 'could not open $readmem' in s]
 assert not critical,critical
 for name,h in info['public_ROM_sha256'].items():assert hashlib.sha256((out/'rom'/name).read_bytes()).hexdigest()==h
 for row in info['sources']:assert hashlib.sha256((out/'sources/pl/v3'/row['name']).read_bytes()).hexdigest()==row['compiled_sha256']
 assert hashlib.sha256((out/'sources/pl/g0/g0_source_receiver.v').read_bytes()).hexdigest()==info['bridge_sha256']
 if 'HDMI_detail_trace' in info:
  trace=info['HDMI_detail_trace'];assert trace['enabled'] is False
  assert hashlib.sha256((out/'sources/pl/rtl/hdmi_presentation_observer.v').read_bytes()).hexdigest()==trace['compiled_observer_sha256']
 if 'final_build_TCL_sha256' in info:assert hashlib.sha256((out/'build.tcl').read_bytes()).hexdigest()==info['final_build_TCL_sha256']
 if 'ADI_static_DC_bypass' in info:
  dc=info['ADI_static_DC_bypass'];proof=json.loads(Path(dc['proof']).read_text())
  assert proof['completed'] and proof['zero_offset_filter_off_all_cycles_data_valid_equal_original']
  assert dc['ADC_DCFILTER_DISABLE']==1 and dc['retained_latency_clocks']==3 and dc['IQ_correction_and_PL_startup_DC_retained']
  assert hashlib.sha256((out/dc['source']).read_bytes()).hexdigest()==dc['compiled_vendor_sha256']==proof['candidate_sha256']
  assert 'CONFIG.ADC_DCFILTER_DISABLE 1' in (out/'build.tcl').read_text()
 if 'forward_PHY_proof' in info:
  forward=info['forward_PHY_proof']
  assert hashlib.sha256(Path(forward['receipt']).read_bytes()).hexdigest()==forward['sha256']
 info['synthesis_passed']=True
 info['synthesis_run_critical_warnings']=[]
 info['early_BD_warnings_not_used_as_final_design_status']=True
 (out/'source-inputs.json').write_text(json.dumps(info,indent=2)+'\n')
 return info

def main(a):
 r=Path(__file__).resolve().parents[1];out=a.output.resolve();ref=a.reference.resolve()
 if a.review_only:
  info=review(out);print(json.dumps({'output':str(out),'synthesis_passed':info['synthesis_passed'],'G0_passed':False}));return
 before=check('before-G0-SOURCE-platform');assert before['disk']['available_bytes']>=5*GIB
 try:
  prepare(argparse.Namespace(archive=a.archive,output=out));(out/'disk-before.json').write_text(json.dumps(before,indent=2)+'\n')
  # User-prioritized diagnostic removal. Keep the complete physical EOF/token
  # observer; only the passive read trace and its optional CSR window go away.
  observer=out/'sources/pl/rtl/hdmi_presentation_observer.v'
  observer_original_sha256=hashlib.sha256(observer.read_bytes()).hexdigest()
  observer.write_text(trimmed(observer.read_text()))
  observer_compiled_sha256=hashlib.sha256(observer.read_bytes()).hexdigest()
  proof=json.loads((ref/'receipt.json').read_text());assert proof['original52RTL_sources_unchanged'] and proof['routed_OOC']['LUT']==42190
  dst=out/'sources/pl/v3';dst.mkdir();rom=out/'rom';rom.mkdir();rom_hash={}
  for p in ref.glob('*.hex'):
   if p.name=='adc.hex':continue
   shutil.copyfile(p,rom/p.name);rom_hash[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
  forward = None
  if a.forward_phy_proof:
   forward = json.loads(a.forward_phy_proof.read_text())
   assert forward['completed'] and forward['synthetic_channel']
   assert forward['sources'] >= 40 and forward['restored'] >= 4
   assert {row['kind'] for row in forward['controls']} == {'VRS3','VRT3'}
   assert forward['public_ROM_sha256'] == rom_hash
   assert set(forward['source_sha256']) == {*proof['source_sha256'], BRIDGE_NAME}
  files=[];inventory=[];replacements=[]
  for n,h in proof['source_sha256'].items():
   p=r/'sim/g0'/n;actual=hashlib.sha256(p.read_bytes()).hexdigest()
   if forward:
    assert actual==forward['source_sha256'][n], n
   if forward and n in FORWARD_CONTROL_SOURCES:
    previous = h
    if n == 'link_fec_ingress_cost_probe.sv':
     # Keep the earlier fixed-8 window proof in the provenance chain.
     assert a.window_proof is not None, 'fixed-8 window proof required'
     window_source=a.window_proof.parent/'optimized'/n
     window=validate_replacement(n,h,window_source,a.correlation_proof,a.rrc_proof,a.window_proof,a.coarse_proof)
     assert window
     previous=window['compiled_sha256']
    assert actual!=previous, n
    replacement={'name':n,'baseline_sha256':h,'pre_timebase_sha256':previous,
                 'compiled_sha256':actual,'proof':str(a.forward_phy_proof.resolve()),
                 'new_forward_control_metadata':True,'same_logic_transform_claimed':False}
    if n == 'link_fec_ingress_cost_probe.sv':
     replacement['previous_window_replacement']=window
   elif n==DMA_BATCH_SOURCE and a.dma_batch_proof:
    replacement=validate_dma_replacement(h,p,a.dma_batch_proof)
   elif n==FEC_STREAM_SOURCE and getattr(a,'fec_deadline_proof',None):
    assert forward, 'fixed-8 FEC change also requires a new complete forward PHY replay'
    replacement=validate_deadline_replacement(h,p,a.fec_deadline_proof)
   else:
    replacement=validate_replacement(n,h,p,a.correlation_proof,a.rrc_proof,a.window_proof,a.coarse_proof)
   if replacement:replacements.append(replacement)
   s=p.read_text()
   # Includes parameter defaults/overrides used by $readmemh(FILE,...),
   # not only the direct $readmemh("literal",...) spelling.
   for name in rom_hash:s=s.replace('"'+name+'"','"'+str(rom/name)+'"')
   restored=s
   for name in rom_hash:restored=restored.replace('"'+str(rom/name)+'"','"'+name+'"')
   assert restored==p.read_text(),n
   target=dst/n;target.write_text(s);files.append(target);inventory.append({'name':n,'reference_sha256':h,'current_source_sha256':actual,'reference_source_replaced':bool(replacement),'compiled_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'only_ROM_paths_relocated':True})
  if any(x['name']=='rrc_cost_probe.sv' for x in replacements):assert rom_hash['rrc.hex']==json.loads(a.rrc_proof.read_text())['coefficient_ROM_sha256']
  bridge=out/'sources/pl/g0'/BRIDGE_NAME;shutil.copyfile(r/'pl/g0'/BRIDGE_NAME,bridge)
  bridge_hash=hashlib.sha256(bridge.read_bytes()).hexdigest()
  if forward:
   assert bridge_hash==forward['source_sha256'][BRIDGE_NAME]
  t=(out/'build.tcl').read_text()
  for n in ['pl/rtl/rf_rx_elastic_fifo.v','pl/receiver_v2/control/rfv2_capture_axis_gate.v','pl/g0/g0_adc_record.v']:
   line='add_files -norecurse {'+str(out/'sources'/n)+'}\n';assert t.count(line)==1;t=t.replace(line,'')
  # Module-reference discovery requires all dependencies before BD creation.
  marker='create_bd_design receiver_v2';assert t.count(marker)==1
  additions=''.join('add_files -norecurse {'+str(p)+'}\n' for p in [*files,bridge]);t=t.replace(marker,additions+marker)
  t=t.replace('g0_adc_record','g0_source_receiver')
  lines=[]
  for line in t.splitlines():
   if any(s in line for s in ['create_bd_cell -type module -reference rf_rx_elastic_fifo','CONFIG.DATA_WIDTH 128 [get_bd_cells rf_rx_elastic]','create_bd_cell -type module -reference rfv2_capture_axis_gate','rf_capture_gate/','g0_source_receiver/ingress_overflow','g0_source_receiver/ingress_flush']):
    if 'rxv2_net ps7/FCLK_CLK0 rf_rx_elastic/clk' in line:lines.append('rxv2_net ps7/FCLK_CLK0 rf_rx_pack/clk')
    elif 'reader_data_enable' in line:lines.append('rxv2_net rf_capture_guard/reader_data_enable g0_source_receiver/capture_enable')
    continue
   line=line.replace('-reference g0_source_receiver g0_source_receiver','-reference g0_source_receiver g0_source_receiver')
   line=line.replace('foreach pin {aclk aresetn ingress_overflow ingress_flush rf_path_ready capture_epoch_fence}', 'foreach pin {aclk aresetn packed_data packed_valid boundary_resetn capture_enable rf_path_ready capture_epoch_fence source_overflow flush_ack}')
   line=line.replace('foreach bus {s_axi s_axis m_axis}', 'foreach bus {s_axi m_axis}')
   line=line.replace('rf_rx_elastic/flush_ack','g0_source_receiver/flush_ack').replace('rf_rx_elastic/resetn','g0_source_receiver/boundary_resetn')
   line=line.replace('rf_rx_elastic/packed_fifo_wr_data','g0_source_receiver/packed_data').replace('rf_rx_elastic/packed_fifo_wr_en','g0_source_receiver/packed_valid').replace('rf_rx_elastic/packed_fifo_wr_overflow','g0_source_receiver/source_overflow')
   lines.append(line)
  t='\n'.join(lines)+'\n';assert 'rf_rx_elastic/' not in t and 'rf_capture_gate/' not in t
  t=t.replace('G0_ADC_','G0_SOURCE_').replace('g0_adc_pre.xsa','g0_source_pre.xsa')
  if a.control_set_threshold is not None:
   assert a.control_set_threshold in (8,16)
   t=t.replace('launch_runs synth_1 -jobs 2','set_property STEPS.SYNTH_DESIGN.ARGS.CONTROL_SET_OPT_THRESHOLD '+str(a.control_set_threshold)+' [get_runs synth_1]\nlaunch_runs synth_1 -jobs 2')
  (out/'build.tcl').write_text(t)
  info={'scope':__doc__,'original54RTL_logic_unchanged_except_absolute_ROM_paths':not replacements,'verified_source_replacements':replacements,'sources':inventory,'public_ROM_sha256':rom_hash,'ADC_fixture_ROM_compiled':False,'raw_ADC_serializer_instantiated':False,'external1024word_elastic_FIFO_instantiated':False,'physical_WFIFO_CPACK_guard_HDMI_retained':True,'bridge_sha256':bridge_hash,'CSR_magic':'V3SP/0x50533356','CSR_limit_unit':'32KiB DMA batches, not16B raw ADC records','synthesis_passed':False,'G0_or_production_passed':False}
  if forward:
   info['forward_PHY_proof']={'receipt':str(a.forward_phy_proof.resolve()),
                              'sha256':hashlib.sha256(a.forward_phy_proof.read_bytes()).hexdigest(),
                              'synthetic_channel':True}
  info['synthesis_control_set_threshold']=a.control_set_threshold
  info['HDMI_detail_trace']={'enabled':False,'original_observer_sha256':observer_original_sha256,'compiled_observer_sha256':observer_compiled_sha256,'core_EOF_token_IRQ_logic_unchanged':True,'disabled_window':'0x80..0xac SLVERR/zero','paired_OOC_net_saving_reference':'docs/evidence/production-diagnostic-redundancy-20261103.json','whole_board_net_saving_not_assumed':True}
  info['base_inputs_inventory_scope']='inputs.json describes original prepared ADC platform/archive provenance. Final SOURCE compile removes raw recorder/externalFIFO/gate, adds55RTL, and patches only HDMI detail trace. Use final build.tcl plus this source inventory for compiled-source provenance.'
  info['final_build_TCL_sha256']=hashlib.sha256((out/'build.tcl').read_bytes()).hexdigest()
  (out/'source-inputs.json').write_text(json.dumps(info,indent=2)+'\n')
  if a.synthesize:
   with (out/'build.log').open('w') as f:subprocess.run([str(a.vivado_bin.resolve()/'vivado'),'-mode','batch','-source',str(out/'build.tcl'),'-nolog','-nojournal'],cwd=out,stdout=f,stderr=subprocess.STDOUT,check=True)
   info=review(out)
  print(json.dumps({'output':str(out),'synthesis_passed':info['synthesis_passed'],'G0_passed':False}),flush=True)
 finally:
  if out.exists():(out/'disk-after.json').write_text(json.dumps(check('after-G0-SOURCE-platform'),indent=2)+'\n')

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['archive','output','reference','vivado-bin']:p.add_argument('--'+n,type=Path,required=True)
 p.add_argument('--coarse-proof',type=Path);p.add_argument('--window-proof',type=Path);p.add_argument('--rrc-proof',type=Path);p.add_argument('--correlation-proof',type=Path);p.add_argument('--forward-phy-proof',type=Path);p.add_argument('--dma-batch-proof',type=Path);p.add_argument('--fec-deadline-proof',type=Path);p.add_argument('--synthesize',action='store_true');p.add_argument('--review-only',action='store_true');p.add_argument('--control-set-threshold',type=int);main(p.parse_args())
