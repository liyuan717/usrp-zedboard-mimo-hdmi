`timescale 1ns/1ps
// Isolated G0 SOURCE receiver platform candidate, no SD/production signoff.
module g0_source_receiver (
    (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER="ASSOCIATED_BUSIF s_axi:m_axis, ASSOCIATED_RESET aresetn, FREQ_HZ 50000000" *) input wire aclk,
    (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER="POLARITY ACTIVE_LOW" *) input wire aresetn,
    input wire [15:0] s_axi_awaddr, input wire s_axi_awvalid, output wire s_axi_awready,
    input wire [31:0] s_axi_wdata, input wire [3:0] s_axi_wstrb, input wire s_axi_wvalid, output wire s_axi_wready,
    output wire [1:0] s_axi_bresp, output reg s_axi_bvalid, input wire s_axi_bready,
    input wire [15:0] s_axi_araddr, input wire s_axi_arvalid, output wire s_axi_arready,
    output reg [31:0] s_axi_rdata, output wire [1:0] s_axi_rresp, output reg s_axi_rvalid, input wire s_axi_rready,
    (* X_INTERFACE_IGNORE="true" *) input wire [127:0] packed_data,
    (* X_INTERFACE_IGNORE="true" *) input wire packed_valid,boundary_resetn,capture_enable,
    output wire [63:0] m_axis_tdata, output wire [7:0] m_axis_tkeep,
    output wire m_axis_tvalid, input wire m_axis_tready, output wire m_axis_tlast,
    (* X_INTERFACE_IGNORE="true" *) output reg source_overflow,
    (* X_INTERFACE_IGNORE="true" *) output reg flush_ack,
    (* X_INTERFACE_IGNORE="true" *) input wire rf_path_ready,
    (* X_INTERFACE_IGNORE="true" *) input wire capture_epoch_fence
);
    reg run,run_previous;
    reg [31:0] limit_batches,remaining,batches_sent;
    reg aw_pending,w_pending;reg [15:0] awaddr;reg [31:0] wdata;reg [3:0] wstrb;
    reg pipe_valid;reg [127:0] pipe_data;
    wire batch_busy;
    // Session reset is allowed only after the entire final DMA batch drains.
    // RF epoch changes below cancel/recalibrate reception, never this AXIS.
    wire core_reset=!aresetn || (!run && !batch_busy);
    wire rearm=run && !run_previous;
    wire [31:0] ingress_faults,reacquisitions,crc_good,crc_bad,unmapped;
    wire [31:0] descriptors,desc_bad,range_busy,fec_event_drop,expired;
    wire [31:0] sources,dropped,bad,aborted,late,event_drop,record_count;
    wire [31:0] core_batches,batch_records,batch_bad,batch_timeout,cal_faults,cal_range;
    wire ingress_running,cal_ready;
    reg [31:0] counters[0:13];
    reg [31:0] last_cal_faults,last_ingress_faults,last_cal_range;
    reg [47:0] phase_origin;reg have_origin,search_armed,search_pending;
    reg [63:0] latest_tag,search_tag;reg [31:0] search_id;
    reg [5:0] tag_age;
    reg [63:0] tag_snapshot;
    reg [7:0] tag_snapshot_status;
    wire search_ready;
    assign s_axi_awready=!aw_pending && !s_axi_bvalid;
    assign s_axi_wready=!w_pending && !s_axi_bvalid;
    assign s_axi_bresp=0;
    assign s_axi_arready=!s_axi_rvalid || s_axi_rready;
    assign s_axi_rresp=0;
    integer i;
    // Low-rate PS clock bracket only. Atomicity is on the existing CPACK
    // tag, not a claim about ADC/FIFO/RF propagation latency. No IQ export.
    always @(posedge aclk)begin
      if(core_reset || capture_epoch_fence)tag_age<=63;
      else if(pipe_valid)tag_age<=0;
      else if(tag_age!=63)tag_age<=tag_age+1'b1;
    end
    always @(posedge aclk) begin
      if(!aresetn)begin
        pipe_valid<=0;pipe_data<=0;flush_ack<=0;source_overflow<=0;
      end else begin
        // This single registered CPACK boundary is the real flushed object.
        // No1024-word external FIFO or raw16B serializer remains here.
        flush_ack<=boundary_resetn && capture_epoch_fence;
        pipe_valid<=boundary_resetn && capture_enable && packed_valid && !capture_epoch_fence;
        if(boundary_resetn && capture_enable && packed_valid)pipe_data<=packed_data;
        source_overflow<=!core_reset && ((ingress_faults!=last_ingress_faults) || (cal_range!=last_cal_range));
      end
    end
    always @(posedge aclk) begin
      if(core_reset)begin
        run_previous<=0;phase_origin<=0;have_origin<=0;search_armed<=0;search_pending<=0;
        latest_tag<=0;search_tag<=0;search_id<=32'h80000000;
        last_cal_faults<=0;last_ingress_faults<=0;last_cal_range<=0;
      end else begin
        run_previous<=run;
        last_cal_faults<=cal_faults;last_ingress_faults<=ingress_faults;last_cal_range<=cal_range;
        if(pipe_valid)latest_tag<=pipe_data[127:64];
        if(!have_origin || cal_faults!=last_cal_faults)begin
          have_origin<=pipe_valid;
          if(pipe_valid)phase_origin<=pipe_data[111:64];
        end
        if(cal_faults!=last_cal_faults)begin search_armed<=0;search_pending<=0;end
        else if(search_pending && search_ready)begin search_pending<=0;search_armed<=1;end
        else if(!search_pending && !search_armed && have_origin && cal_ready && ingress_running)begin
          search_pending<=1;search_tag<={latest_tag[63:48],latest_tag[47:0]+48'd4};search_id<=search_id+1'b1;
        end
      end
    end
    calibrated_batched_frontend_cost_probe receiver(
      .clk(aclk),.rst(core_reset),.cancel(1'b0),.rearm(rearm),.run(run),
      .raw_valid(pipe_valid),.raw_data(pipe_data[63:0]),.raw_tag(pipe_data[127:64]),.upstream_loss(capture_epoch_fence),
      .search_valid(search_pending),.search_ready(search_ready),.search_id(search_id),.search_tag(search_tag),.phase_origin(phase_origin),
      .ingress_running(ingress_running),.ingress_faults(ingress_faults),.local_reacquisitions(reacquisitions),
      .m_axis_tdata(m_axis_tdata),.m_axis_tkeep(m_axis_tkeep),.m_axis_tvalid(m_axis_tvalid),.m_axis_tready(m_axis_tready),.m_axis_tlast(m_axis_tlast),
      .packets_accepted(crc_good),.packets_erased(crc_bad),.packets_unmapped(unmapped),
      .descriptors_accepted(descriptors),.descriptors_rejected(desc_bad),.range_busy(range_busy),.fec_events_dropped(fec_event_drop),.expired_blocks(expired),
      .sources_committed(sources),.sources_dropped(dropped),.sources_bad(bad),.sources_aborted(aborted),.sources_late_or_duplicate(late),.events_dropped(event_drop),.records_completed(record_count),
      .batch_busy(batch_busy),.batches_completed(core_batches),.batch_records(batch_records),.batch_rejected(batch_bad),.batch_input_timeouts(batch_timeout),
      .calibration_ready(cal_ready),.calibration_faults(cal_faults),.calibration_range_drops(cal_range));
    always @(posedge aclk)begin
      if(!aresetn)begin
        run<=0;limit_batches<=0;remaining<=0;batches_sent<=0;
        aw_pending<=0;w_pending<=0;awaddr<=0;wdata<=0;wstrb<=0;
        s_axi_bvalid<=0;s_axi_rvalid<=0;s_axi_rdata<=0;tag_snapshot<=0;tag_snapshot_status<=0;
        for(i=0;i<14;i=i+1)counters[i]<=0;
      end else begin
        if(!core_reset)begin
          counters[0]<=sources;counters[1]<=dropped;counters[2]<=bad;counters[3]<=aborted;
          counters[4]<=crc_good;counters[5]<=crc_bad;counters[6]<=ingress_faults;counters[7]<=reacquisitions;
          counters[8]<=cal_faults;counters[9]<=cal_range;counters[10]<=batch_bad;counters[11]<=batch_timeout;
          counters[12]<=expired;counters[13]<=event_drop+fec_event_drop;
        end
        if(m_axis_tvalid && m_axis_tready && m_axis_tlast)begin
          batches_sent<=batches_sent+1'b1;
          if(run && limit_batches!=0)begin remaining<=remaining-1'b1;if(remaining==1)run<=0;end
        end
        if(s_axi_awvalid && s_axi_awready)begin aw_pending<=1;awaddr<=s_axi_awaddr;end
        if(s_axi_wvalid && s_axi_wready)begin w_pending<=1;wdata<=s_axi_wdata;wstrb<=s_axi_wstrb;end
        if(s_axi_bvalid && s_axi_bready)s_axi_bvalid<=0;
        if(aw_pending && w_pending && !s_axi_bvalid)begin
          aw_pending<=0;w_pending<=0;s_axi_bvalid<=1;
          if(awaddr[15:2]==2 && wstrb[0])begin
            if(!wdata[0])run<=0;
            else if(!run && !batch_busy && rf_path_ready)begin
              run<=1;remaining<=limit_batches;batches_sent<=0;
              for(i=0;i<14;i=i+1)counters[i]<=0;
            end
          end
          if(awaddr[15:2]==11 && !run && !batch_busy && wstrb==15)limit_batches<=wdata;
        end
        if(s_axi_rvalid && s_axi_rready)s_axi_rvalid<=0;
        if(s_axi_arvalid && s_axi_arready)begin
          s_axi_rvalid<=1;
          case(s_axi_araddr[15:2])
            0:s_axi_rdata<=32'h50533356; // "V3SP", distinct from old raw ADC
            1:s_axi_rdata<=32'h20261103;
            2:s_axi_rdata<={31'd0,run};
            3:s_axi_rdata<={26'd0,ingress_running,search_armed,cal_ready,run,batch_busy,rf_path_ready};
            4:s_axi_rdata<=batches_sent;
            5:s_axi_rdata<=32'd32768;
            6:s_axi_rdata<=counters[6];
            7:s_axi_rdata<=counters[10];
            8:s_axi_rdata<=counters[8];
            9:s_axi_rdata<=counters[0];
            10:s_axi_rdata<=counters[1];
            11:s_axi_rdata<=limit_batches;
            12:s_axi_rdata<=remaining;
            16,17,18,19,20,21,22,23,24,25,26,27,28,29:s_axi_rdata<=counters[s_axi_araddr[5:2]];
            32:s_axi_rdata<=32'h43543356; // "V3TC", optional clock-tag capability
            33:begin
              s_axi_rdata<=latest_tag[31:0];tag_snapshot<=latest_tag;
              tag_snapshot_status<={tag_age,1'b0,(run && have_origin && tag_age!=63 && !capture_epoch_fence)};
            end
            34:s_axi_rdata<=tag_snapshot[63:32];
            35:s_axi_rdata<={24'd0,tag_snapshot_status};
            default:s_axi_rdata<=0;
          endcase
        end
      end
    end
endmodule
