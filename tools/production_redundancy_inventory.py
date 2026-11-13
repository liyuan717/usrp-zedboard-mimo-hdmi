#!/usr/bin/env python3
"""Annotate every row of the actual integrated synthesis hierarchy, without summing parents twice."""
import argparse,hashlib,json,re
from pathlib import Path

PURPOSE={
'adc_dc_calibration':'四路DC启动估计/量化；断续失效后重新校准',
'calibrated_batched_frontend':'原始双RX到完整SOURCE批次总链',
'batched_acquiring_frontend':'接收及固定32KiB批次组合',
'dma_batch_stream':'流式封装完整记录/填充/有界低流量截止；无整批PL RAM',
'record_acquiring_frontend':'SOURCE记录交付及主链组合',
'source_record_bridge':'SOURCE完整提交/有界排队/去重/停止排空',
'frame_acquiring_frontend':'2×2检测/链路CRC/固定8-bank FEC总链',
'acquiring_continuous_frontend':'有界ADC入口/自主捕获/信道训练/调度组合',
'tracking_acquiring_frontend':'捕获与公共历史/定时SFO跟踪组合',
'differential_acquisition':'粗捕获能量/相关/频偏组合',
'differential_frontend':'粗捕获输入与相关历史组合',
'differential_corr':'差分粗捕获相关及四路历史',
'differential_input':'差分样本/能量历史与量化',
'differential_score':'粗捕获能量评分/有效性判定',
'phase_angle':'相位角计算；按所在捕获/训练/导频生命周期使用',
'fine_nco_config':'频偏到NCO初相/步进配置',
'frontend_chain':'训练前端分数采样/NCO/RRC组合',
'nco':'双RX载波复数校正',
'rrc':'双RX匹配滤波',
'interpolation':'分数采样/坐标插值；所在训练或业务调度各自持有状态',
'timing_request':'分数采样请求/坐标有界检查',
'training_channel':'双TX正交训练和四复信道估计',
'continuous_adc_ingress':'无READY入口队列/tag连续性/丢样与代际隔离',
'compact_stream_fifo':'唯一保留的128拍同步入口缓存',
'ingress_recovery_control':'有界撤销/过期释放/自主重捕获',
'coarse_startup_schedule':'粗捕获到业务的有界启动调度',
'training_frontend':'训练校正/匹配滤波/信道计算组合',
'tracking_job_dispatch':'公共历史请求/业务时隙仲裁',
'causal_tracking_timing':'共享4096采样历史/定时/SFO组合',
'coherent_timing':'公共相关窗口与峰值搜索组合',
'coherent_window':'公共相关采样窗口；已共享，非额外诊断副本',
'coherent_peak':'相关峰搜索/定时置信度',
'causal_sfo':'采样频偏跟踪/后续坐标修正',
'link_fec_ingress':'CRC后固定8-bank准入/身份/期限/尽力恢复/提前交付',
'fec_stream_recovery':'RS20/32/S512有界队列和恢复/释放',
'fec_inverse':'RS系数求逆/有限scratch',
'fec_memory':'8-bank编码符号存储；块寿命至交付/截止',
'payload_schedule':'双流业务调度和共享采样池请求',
'payload_datapath':'双流相位/旋转/联合检测组合',
'payload_phase':'业务符号相位生成',
'payload_math':'双RX旋转和2×2联合检测',
'payload_rotate':'双RX复数旋转',
'payload_ml':'2×2独立双流联合ML检测',
'frame_replay':'受保护头/导频读取与业务帧调度',
'pilot_tracking':'头部和导频相位跟踪组合',
'header_pilot_math':'受保护头部/导频估计组合',
'pilot_angle':'导频观测与相位角估计',
'bpsk_header':'受保护头检测/字段验证',
'pilot_smoothing':'导频相位平滑与有效性',
'frame_domain':'坐标域/块身份交接',
'payload_link':'双流独立包校验和CRC提交组合',
'link_packet':'单独一条空间流的包头/长度/CRC/原子载荷缓存',
}
TOP={
'rf_spi':'AD9361 SPI配置', 'adc_counter':'真实采样坐标/epoch及复位释放',
'axi_ad9361':'双RX物理接口/芯片配置/校准；板端TX保持关闭',
'axi_hdmi_clkgen':'HDMI时钟生成', 'axi_hdmi_core':'HDMI物理扫描/帧边界/mailbox',
'axi_hdmi_dma':'显示DMA及scanout所有权', 'axi_iic_main':'HDMI发射芯片I2C配置',
'control_xbar':'低速CSR互联；复用已有地址，不另建控制总线',
'g0_source_receiver':'SOURCE接收板桥/CSR/自动搜索及完整接收链',
'hdmi_memory_xbar':'显示DDR互联', 'hdmi_presentation_observer':'物理EOF/显示确认/身份事件/IRQ；详细读追踪可分离',
'rf_gpio':'RF复位/电气状态控制', 'rf_memory_xbar':'RF到DDR互联，复用给SOURCE',
'rf_output_dmac':'SOURCE生产DMA；复用原raw的DMA',
'rf_rx_fifo':'真实异步WFIFO跨域/tag与IQ成组',
'rf_rx_pack':'四路IQ及tag原子CPACK',
'rf_capture_guard':'跨时钟复位/flush真实ACK/入口保护',
'rf_lclk_monitor':'RF时钟稳定监测',
}

def read_rows(path):
    rows=[];stack=[]
    for l in path.read_text().splitlines():
        c=l.split('|')
        if len(c)!=12 or not c[3].strip().isdigit():continue
        indent=len(c[1])-len(c[1].lstrip());name=c[1].strip();module=c[2].strip()
        while stack and stack[-1][0]>=indent:stack.pop()
        parent='/'.join(x[1] for x in stack);instance=parent+'/'+name if parent else name
        values=list(map(int,(x.strip() for x in c[3:11])))
        purpose=None
        if name=='hdmi_axi_read_probe_i':purpose='非必要HDMI详细AR/R/AXIS计数/停顿/地址追踪'
        elif module=='g0_source_receiver' and name=='(inst)':purpose='新CSR/计数影子/单拍CPACK桥/自动phase与search控制（本级）'
        elif module.startswith('ad_dcfilter'):purpose='ADI可编程DC/offset滤波；与启动窗口DC估计的配置和动态不同，不能直接视为副本'
        elif module.startswith('ad_iqcor'):purpose='ADI四路IQ正交校正/格式适配；不能与载波NCO旋转混为同一功能'
        elif module.startswith(('axi_ad9361_rx_pnmon','ad_pnmon')):purpose='ADI 标准 PN 误码/失步状态，保留实板数字链路自检'
        elif module.startswith('rf_rx_pn_pair_observer'):purpose='RF PN成对校验/跨域状态观察；物理接口诊断，后续可独立审计'
        elif module.startswith('rf_rx_frame_window_observer'):purpose='RF帧窗/接口对齐诊断；后续可独立审计，不是CRC/FEC'
        else:
            base=module.split('_cost_probe')[0]
            purpose=PURPOSE.get(base)
        if purpose is None:
            for part in instance.split('/'):
                if part in TOP:purpose=TOP[part]+'的子逻辑'
            if purpose is None:purpose='平台时钟/复位/互联结构及其组合；不按同名判重复'
        decision='保留；未证明可安全删除或合并'
        if module.startswith(('rf_rx_pn_pair_observer','rf_rx_frame_window_observer')):decision='列为后续可裁剪观察项；先核驱动/bringup依赖及实际净收益，当前不预扣'
        if 'hdmi_axi_read_probe_i' in instance:decision='优先裁剪详细诊断；仅独立配对综合已测，整板尚未采用'
        elif 'link.packets' in instance:decision='保留；两空间流独立提交，CRC错转擦除继续接收'
        elif 'block_fec.ingress' in instance:decision='保留固定8-bank/RS参数；尽力恢复、提前源交付、截止释放，不逐包必修复'
        elif '/rf_output_dmac/' in instance or name=='rf_output_dmac':decision='复用既有DMA给SOURCE批次；边界/STOP/所有权保留'
        elif 'ingress' in instance or 'recovery' in instance:decision='保留唯一入口缓存和有界断续恢复；旧外部深FIFO已从候选移除'
        elif 'hdmi_presentation_observer' in instance:decision='保留物理EOF/token/IRQ/事件所有权；只分离详细诊断'
        rows.append({'instance':instance,'module':module,'indent':indent,'LUT':values[0],'logic_LUT':values[1],'LUTRAM':values[2],'SRL':values[3],'FF':values[4],'RAMB36':values[5],'RAMB18':values[6],'BRAM36_equivalent':values[5]+values[6]/2,'DSP':values[7],'purpose':purpose,'decision':decision,'inclusive_parent_do_not_add_children':not name.startswith('('),'individually_measured_net_saving':None})
        if not name.startswith('('):stack.append((indent,name))
    assert rows and rows[0]['instance']=='receiver_v2_wrapper'
    assert 0<=rows[0]['DSP']<=220
    return rows

def main(a):
    rows=read_rows(a.report)
    trace=any(x['module']=='hdmi_axi_read_probe' for x in rows)
    if not trace:
        for row in rows:
            if 'hdmi_presentation_observer' in row['instance']:
                row['purpose']='物理EOF/显示确认/token/IRQ/事件FIFO；详细读追踪已从候选关闭'
                row['decision']='保留物理确认与所有权；详细诊断关闭，净综合差额见独立配对收据'
    if a.adi_dc_bypass_proof:
        proof=json.loads(a.adi_dc_bypass_proof.read_text())
        assert proof['completed'] and proof['zero_offset_filter_off_all_cycles_data_valid_equal_original']
        count=0;channels=0
        for row in rows:
            if row['module'].startswith('ad_dcfilter'):
                row['purpose']='当前零offset/filter-off profile的三周期data/valid旁路；额外ADI IIR/offset能力已静态裁剪'
                row['decision']='保留已证明的三周期采样延迟/valid间隙；不删除IQ校正或PL启动DC。净收益按整板配对计，不扣毛成本'
                assert row['DSP']==0
                count+=1
            if row['module'].startswith('axi_ad9361_rx_channel') and row['instance'].rsplit('/',1)[-1].startswith('i_rx_channel_'):
                row['purpose']='单ADC通道格式/IQ校正/三周期零offset旁路；额外ADI DC IIR已静态裁剪'
                row['decision']='保留IQ/格式和已证明的三周期延迟；父级成本已包含被合并的旁路，不重复扣旧filter毛成本'
                channels+=1
        assert count in (0,4) and channels==4,(count,channels)
    r={'scope':'Current55RTL fixed8 production-intent candidate; NOT an accepted production bitstream. Integrated SYNTH only; physical placement/route status belongs to separate implementation receipt. All hierarchy costs inclusive unless parenthesized local row.','tool_part':'Vivado2025.2 xc7z020clg484-1','report':str(a.report),'report_sha256':hashlib.sha256(a.report.read_bytes()).hexdigest(),'integrated_synthesis':{k:rows[0][k] for k in ('LUT','FF','BRAM36_equivalent','DSP')},'old_raw_recorder_present':any('g0_adc_record' in x['module'] for x in rows),'external_legacy_fifo_present':any('rf_rx_elastic_fifo' in x['module'] for x in rows),'HDMI_detail_trace_present':trace,'hierarchy':rows,'whole_board_routed':False,'complete_resource_budget_passed':False,'net_saving_comparison_in_this_inventory':False}
    if a.route_review:
        review=json.loads(a.route_review.read_text())
        assert review.get('route_complete_no_errors', review.get('route_all95575_nets_complete_no_errors', False))
        assert review['typed_RF_HDMI_counter65_contracts_passed']
        assert review['WNS_ns']>=0 and review['WHS_ns']>=0
        for name,digest in review['source_report_sha256'].items():
            assert hashlib.sha256((a.route_review.parent/'project'/name).read_bytes()).hexdigest()==digest,name
        log = review.get('implementation_log')
        route_path = Path(log['path']) if log else a.route_review.parent/'route.log'
        route_log = route_path.read_bytes()
        if log:
            assert hashlib.sha256(route_log).hexdigest()==log['sha256']
        assert b'G0_SOURCE_ROUTE_COMPLETE_REVIEW_FULL_REPORTS_BEFORE_BITSTREAM' in route_log
        r['whole_board_routed']=True
        r['separate_route_review']={'path':str(a.route_review),'sha256':hashlib.sha256(a.route_review.read_bytes()).hexdigest(),'full_production_IO_signoff':review['full_production_IO_signoff']}
    a.output.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'rows':len(rows),'cost':r['integrated_synthesis'],'old_raw_recorder_present':r['old_raw_recorder_present'],'external_legacy_fifo_present':r['external_legacy_fifo_present']},ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--route-review',type=Path);p.add_argument('--adi-dc-bypass-proof',type=Path);main(p.parse_args())
