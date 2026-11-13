#!/usr/bin/env python3
"""Simulate G0 raw-record ownership/serialization independently of RF algorithms."""
import argparse
import json
import re
from pathlib import Path
import subprocess

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vivado-bin',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--platform-sources',type=Path,help='Prepared, locked r9 source tree for full measurement-pipeline test')
    a=p.parse_args();root=Path(__file__).resolve().parents[1]
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    calls=[['xvlog','--sv',str(root/'pl/g0/g0_adc_record.v'),str(root/'sim/g0/adc_record_tb.sv')],
           ['xelab','adc_record_tb','-s','adc_record_tb','-mt','off'],
           ['xsim','adc_record_tb','-runall']]
    for i,call in enumerate(calls):
        call[0]=str(a.vivado_bin.resolve()/call[0])
        with (out/f'{i}.log').open('w') as log:subprocess.run(call,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
    if 'G0_ADC_RECORD_PASS records=1300' not in (out/'2.log').read_text():raise ValueError('pass marker absent')
    pipeline=None
    if a.platform_sources:
        src=a.platform_sources.resolve();vendor=src/'vendor/library'
        wf=[];cp=[]
        for lane in range(8):
            wf.extend([f'.din_enable_{lane}(1\'b1)',f'.din_valid_{lane}(vin)',
                f'.din_data_{lane}(din[{16*lane}+:16])',f'.dout_enable_{lane}(wfenable[{lane}])',
                f'.dout_valid_{lane}(wfvalid[{lane}])',f'.dout_data_{lane}(wfdata[{16*lane}+:16])'])
            cp.extend([f'.enable_{lane}(wfenable[{lane}])',f'.fifo_wr_data_{lane}(wfdata[{16*lane}+:16])'])
        (out/'g0_wfifo_ports.vh').write_text(',\n'.join(wf)+'\n')
        (out/'g0_cpack_ports.vh').write_text(',\n'.join(cp)+'\n')
        files=[root/'pl/g0/g0_adc_counter.v',root/'pl/g0/g0_adc_record.v',
            src/'pl/receiver_v2/control/rfv2_capture_reset_guard.v',src/'pl/receiver_v2/control/rfv2_capture_axis_gate.v',
            src/'pl/rtl/rf_rx_elastic_fifo.v',vendor/'util_wfifo/util_wfifo.v',
            vendor/'common/ad_mem.v',vendor/'common/ad_mem_asym.v',vendor/'common/ad_perfect_shuffle.v',
            vendor/'util_pack/util_cpack2/util_cpack2.v',vendor/'util_pack/util_cpack2/util_cpack2_impl.v']
        files+=sorted((vendor/'util_pack/util_pack_common').glob('*.v'))
        xpm=a.vivado_bin.resolve().parents[0]/'data/ip/xpm'
        calls=[['xvlog',*[str(f) for f in files]],
            ['xvlog','--sv','-i',str(out),str(xpm/'xpm_cdc/hdl/xpm_cdc.sv'),str(xpm/'xpm_memory/hdl/xpm_memory.sv'),str(root/'sim/g0/adc_pipeline_tb.sv')],
            ['xelab','adc_pipeline_tb','-s','adc_pipeline_tb','-mt','off'],['xsim','adc_pipeline_tb','-runall']]
        for i,call in enumerate(calls,3):
            call[0]=str(a.vivado_bin.resolve()/call[0])
            with (out/f'{i}.log').open('w') as log:subprocess.run(call,cwd=out,stdout=log,stderr=subprocess.STDOUT,check=True)
        if 'G0_ADC_PIPELINE_PASS' not in (out/'6.log').read_text():raise ValueError('pipeline pass marker absent')
        match=re.search(r'G0_ADC_PIPELINE_PASS received=(\d+) injected=(\d+) overflow_events=(\d+) gaps=(\d+) fences=(\d+)',(out/'6.log').read_text())
        if not match:raise ValueError('pipeline result fields absent')
        pipeline=dict(zip(['records_compared','source_scans','overflow_events','observed_gaps','epoch_fences'],map(int,match.groups())))
        pipeline.update({'real_vendor_wfifo_cpack':True,'xpm_elastic_width_bits':128,
            'clock_stop_recovery':True,'finite_capture_records_per_run':1000,'finite_runs':2})
    (out/'receipt.json').write_text(json.dumps({'scope':'G0 raw serializer only, not complete ADC/CDC pipeline',
        'records_compared':1300,'aw_w_independent':True,'output_backpressure':True,
        'pipeline_simulation':pipeline,'RF_hardware_test':False,'g0_passed':False},indent=2)+'\n')
    print('G0 raw-record RTL comparison passed')
