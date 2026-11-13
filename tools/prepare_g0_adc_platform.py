#!/usr/bin/env python3
"""Prepare an isolated G0 ADC recorder using locked r9 RF IO and HDMI.

This is measurement hardware, not a complete V3 receiver. No old RF solver,
acquisition, protocol parser or detector is compiled. Output is tagged raw ADC.
"""
import argparse
import json
from pathlib import Path
import shutil
import zipfile
import zlib


def between(text, first, last):
    if text.count(first) != 1 or text.count(last) != 1:
        raise ValueError("reference composition anchor is ambiguous")
    return text[text.index(first):text.index(last)]


def prepare(a):
    project=Path(__file__).resolve().parents[1]
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    sources=out/'sources';sources.mkdir()
    prefix='build/rfv2_hdmi_scoped_admission_final_r1/sources/'
    with zipfile.ZipFile(a.archive) as archive:
        for name in archive.namelist():
            if name.startswith(prefix) and not name.endswith('/'):
                relative=Path(name[len(prefix):])
                if relative.is_absolute() or '..' in relative.parts:raise ValueError("archive traversal")
                dest=sources/relative;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(archive.read(name))
        helper='build/rfv2_hdmi_scoped_admission_final_r1/attach_hdmi_axi_read_probe_debug.tcl'
        (out/'attach_hdmi_axi_read_probe_debug.tcl').write_bytes(archive.read(helper))
    needed=[
        'pl/rtl/rf_rx_elastic_fifo.v','pl/rtl/rf_lvds_forward_clock.v',
        'pl/receiver_v2/control/rfv2_rf_clock_monitor.v',
        'pl/receiver_v2/control/rfv2_capture_reset_guard.v',
        'pl/receiver_v2/control/rfv2_capture_axis_gate.v',
        'pl/rtl/hdmi_presentation_observer.v','pl/rtl/rf_axis_status_monitor.v',
        'pl/rtl/hdmi_axi_read_probe.v','pl/rtl/hdmi_axi_read_bus_monitor.v',
        'vendor/library/common/ad_iobuf.v']
    for name in needed:
        if not (sources/name).is_file():raise ValueError('locked source missing '+name)
    for name in ['g0_adc_counter.v','g0_adc_record.v']:
        target=sources/'pl/g0'/name;target.parent.mkdir(exist_ok=True)
        shutil.copyfile(project/'pl/g0'/name,target);needed.append('pl/g0/'+name)
    # Carry the ADC-domain tag through the SAME eight-lane WFIFO/CPACK beat.
    # Adapt only data width metadata in the locked generic elastic FIFO/gate.
    elastic=sources/'pl/rtl/rf_rx_elastic_fifo.v'
    elastic.write_text(elastic.read_text().replace('TDATA_NUM_BYTES 8','TDATA_NUM_BYTES 16'))
    gate=sources/'pl/receiver_v2/control/rfv2_capture_axis_gate.v'
    gate.write_text(gate.read_text().replace('[63:0]','[127:0]').replace('[7:0]','[15:0]'))
    # r9 fixes CPack's delayed-ready loss at valid gaps only for four full
    # channels. The same identity path is required for eight full channels
    # carrying four voltages + four tag lanes. Keep partial-channel routing
    # unchanged; the capture owner must verify all voltage lanes are enabled.
    pack_name='vendor/library/util_pack/util_cpack2/util_cpack2_impl.v'
    pack=sources/pack_name;pack_text=pack.read_text()
    anchor='NUM_OF_CHANNELS == 4 && SAMPLES_PER_CHANNEL == 1'
    if pack_text.count(anchor)!=1:raise ValueError('CPack identity-path anchor')
    pack_text=pack_text.replace(anchor,'(NUM_OF_CHANNELS == 4 || NUM_OF_CHANNELS == 8) && SAMPLES_PER_CHANNEL == 1')
    pack_text=pack_text.replace('direct_four_fifo','direct_full_fifo').replace('fixed full four-channel FIFO','fixed full four/eight-channel FIFO')
    pack.write_text(pack_text)
    reference=(project/'vendor/r9_io/create_zedboard_receiver_v2.tcl').read_text()
    helpers=between(reference,'proc rxv2_ip {','create_bd_design receiver_v2')
    body=between(reference,'create_bd_design receiver_v2','validate_bd_design\n')
    start=body.index('# Version the implementation policy')
    end=body.index('foreach pin {aclk aresetn',start)
    body=body[:start]+body[end:]
    body=body.replace('CONFIG.NUM_OF_CHANNELS 4','CONFIG.NUM_OF_CHANNELS 8')
    body=body.replace('create_bd_cell -type module -reference rf_rx_elastic_fifo rf_rx_elastic',
        'create_bd_cell -type module -reference rf_rx_elastic_fifo rf_rx_elastic\nset_property CONFIG.DATA_WIDTH 128 [get_bd_cells rf_rx_elastic]')
    body=body.replace('rf_receiver_v2_bd','g0_adc_record')
    marker='rxv2_net rf_rx_fifo/din_ovf axi_ad9361/adc_dovf\n'
    addition='''create_bd_cell -type module -reference g0_adc_counter adc_counter
rxv2_net axi_ad9361/l_clk adc_counter/clk
rxv2_net ps7/FCLK_RESET0_N adc_counter/por_n
rxv2_net rf_capture_guard/wfifo_din_rst adc_counter/source_reset
rxv2_net axi_ad9361/adc_valid_i0 adc_counter/sample_valid
for {set lane 4} {$lane<8} {incr lane} {
  set bit [expr {($lane-4)*16}]
  create_bd_cell -type ip -vlnv xilinx.com:ip:xlslice:1.0 adc_tag_$lane
  set_property -dict [list CONFIG.DIN_WIDTH 64 CONFIG.DIN_FROM [expr {$bit+15}] CONFIG.DIN_TO $bit CONFIG.DOUT_WIDTH 16] [get_bd_cells adc_tag_$lane]
  rxv2_net adc_counter/tag adc_tag_$lane/Din
  rxv2_net adc_tag_$lane/Dout rf_rx_fifo/din_data_$lane
  rxv2_net constant_one/dout rf_rx_fifo/din_enable_$lane
  rxv2_net rf_capture_valid_0/Res rf_rx_fifo/din_valid_$lane
  rxv2_net rf_rx_fifo/dout_enable_$lane rf_rx_pack/enable_$lane
  rxv2_net rf_rx_fifo/dout_data_$lane rf_rx_pack/fifo_wr_data_$lane
}
'''
    if body.count(marker)!=1:raise ValueError('ADC tag insertion anchor')
    body=body.replace(marker,addition+marker)
    # Apply the reviewed hold budget to the isolated archive copy. Keep
    # one min delay command so Vivado cannot resolve competing budgets.
    hdmi_xdc=sources/'pl/constraints/zedboard_hdmi_adv7511.xdc'
    hdmi=hdmi_xdc.read_text()
    inherited_hold=(
        'set_output_delay -clock hdmi_forwarded -min -0.500 '
        '[get_ports {hdmi_data[*] hdmi_vsync hdmi_hsync hdmi_data_e}]'
    )
    inherited_comment='# Candidate setup budget 2 ns; hold budget 0.5 ns. Display modes require'
    if hdmi.count(inherited_hold)!=1 or hdmi.count(inherited_comment)!=1:
        raise ValueError('locked HDMI hold constraint differs from the reviewed archive')
    hdmi=hdmi.replace(inherited_comment,'# Candidate setup budget 2 ns. Display modes require')
    hold=(project/'pl/g0/adv7511_input_hold_preboard.xdc').read_text().rstrip()
    hdmi_xdc.write_text(hdmi.replace(inherited_hold,hold))
    attrs=['pl/constraints/zedboard_fmcomms3_lvds_2v5.xdc','pl/constraints/zedboard_hdmi_adv7511.xdc',
           'pl/constraints/zedboard_rf_sync_attributes.xdc','pl/receiver_v2/integration/rf_clock_monitor_cdc.xdc']
    late=['pl/receiver_v2/integration/capture_reset_guard_cdc.xdc',
          'pl/receiver_v2/integration/release_assertion_exact_cdc.xdc',
          'pl/receiver_v2/integration/hdmi_release_assertion_exact_cdc.xdc',
          'pl/receiver_v2/integration/hdmi_gray_pointer_cdc.xdc',
          'pl/receiver_v2/integration/hdmi_mailbox_exact_cdc.xdc',
          'pl/receiver_v2/integration/hdmi_epoch_exact_cdc.xdc']
    attrs+=['external-budgets/MIMO_RF_EXTERNAL_BUDGET_XDC.xdc','external-budgets/MIMO_RF_SPI_BUDGET_XDC.xdc']
    for name in attrs+late:
        if not (sources/name).is_file():raise ValueError('constraint missing '+name)
    topology=project/'vendor/r9_io/receiver_v2_topology_contract.tcl'
    tcl=f'''# G0 tagged ADC recorder; not G3/G4 full V3 implementation.
set_param general.maxThreads 2
set rxv2_root {{{sources}}}
set source_root $rxv2_root
set rxv2_adi [file join $rxv2_root vendor]
set rxv2_out {{{out/'project'}}}
set rxv2_enable_hdmi 1
source {{{topology}}}
create_project receiver_v2 $rxv2_out -part xc7z020clg484-1
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]
set_property ip_repo_paths [list [file join $rxv2_adi library]] [current_project]
update_ip_catalog
set_property include_dirs [list $rxv2_root] [current_fileset]
'''
    for name in needed:tcl+=f'add_files -norecurse {{{sources/name}}}\n'
    tcl+=helpers+body
    tcl+=f'''source {{{out/'attach_hdmi_axi_read_probe_debug.tcl'}}}
attach_hdmi_axi_read_probe_debug
validate_bd_design
save_bd_design
set bd [get_files */receiver_v2.bd]
set_property synth_checkpoint_mode None $bd
generate_target synthesis $bd
add_files -norecurse [make_wrapper -files $bd -top]
set_property top receiver_v2_wrapper [current_fileset]
'''
    for name in attrs:tcl+=f'add_files -fileset constrs_1 -norecurse {{{sources/name}}}\n'
    for name in late:
        tcl+=f'''add_files -fileset constrs_1 -norecurse {{{sources/name}}}
set_property USED_IN_SYNTHESIS false [get_files {{{sources/name}}}]
set_property USED_IN_IMPLEMENTATION true [get_files {{{sources/name}}}]
set_property PROCESSING_ORDER LATE [get_files {{{sources/name}}}]
'''
    tcl+='''set_property PROCESSING_ORDER LATE [get_files *rf_clock_monitor_cdc.xdc]
update_compile_order -fileset sources_1
write_bd_tcl -force [file join $rxv2_out rebuild_g0_adc_bd.tcl]
write_hw_platform -fixed -force -file [file join $rxv2_out g0_adc_pre.xsa]
puts "G0_ADC_PLATFORM_VALIDATED"
launch_runs synth_1 -jobs 2
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "G0 ADC synthesis failed"}
open_run synth_1
if {[llength [get_cells -hier -quiet -filter {IS_BLACKBOX == 1}]]} {error "G0 contains black boxes"}
report_utilization -hierarchical -file [file join $rxv2_out synthesis_utilization.rpt]
report_timing_summary -report_unconstrained -file [file join $rxv2_out synthesis_timing.rpt]
report_drc -file [file join $rxv2_out synthesis_drc.rpt]
report_cdc -details -no_waiver -file [file join $rxv2_out synthesis_cdc.rpt]
puts "G0_ADC_SYNTHESIS_COMPLETE_REVIEW_CDC_BEFORE_ROUTE"
close_project
'''
    (out/'build.tcl').write_text(tcl)
    inventory=[]
    for name in needed+attrs+late+[pack_name]:
        data=(sources/name).read_bytes();inventory.append({'path':name,'bytes':len(data),'crc32':f'{zlib.crc32(data):08x}'})
    (out/'inputs.json').write_text(json.dumps({'scope':'G0 tagged ADC prototype only','archive':str(a.archive.resolve()),
        'compiled_sources_and_constraints':inventory,'legacy_RF_detector_compiled':False,'HDMI':'locked r9 IO/driver contract',
        'ADC_clock_hz':16000000,'sample_rate_hz':4000000,'record_bytes':16,
        'tag':'ADC-domain epoch16/index48 through WFIFO lanes 4..7','g0_passed':False},indent=2)+'\n')
    print(out/'build.tcl')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    prepare(p.parse_args())
