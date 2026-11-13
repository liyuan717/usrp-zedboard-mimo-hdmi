`timescale 1ns/1ps
// Actual acquisition/shared timing/training -> shared-pool header replay/CRC.
// Normal H is atomically accepted by both header and future PL payload consumer.
// No host dispatch or reverse RF; actual header/pilot phase tracking, payload/FEC/DMA pending.
module frame_acquiring_frontend_cost_probe #(parameter ENABLE_PAYLOAD=0,ENABLE_LINK=0,ENABLE_FEC=0,CHECK_INPUT_TAG=1)(
 input wire clk,rst,cancel,rearm,raw_valid,upstream_loss,input wire [63:0] raw_tag,raw_data,
 input wire search_valid,output wire search_ready,input wire [31:0] search_id,
 input wire [63:0] search_tag,input wire [47:0] phase_origin,
 output wire result_valid,input wire result_ready,output wire [31:0] result_id,result_generation,
 output wire [2:0] result_error,result_detail,output wire [31:0] result_step,result_phase,
 output wire [9:0] first_symbol,output wire [8:0] training_count,output wire [63:0] coarse_known,final_commit,
 output wire ingress_running,output wire [31:0] ingress_faults,output wire [2:0] ingress_reason,output wire [7:0] ingress_peak,
 output wire normal_valid,input wire normal_ready,output wire [31:0] normal_id,normal_context,normal_generation,
 output wire [15:0] normal_epoch,output wire [207:0] normal_h,output wire [63:0] normal_steps,
 output wire [1:0] normal_selected,output wire [199:0] normal_energies,output wire [2:0] normal_error,normal_detail,
 output wire tracking_locked,receiver_fence,output wire timing_update,
 output wire [31:0] timing_id,timing_generation,output wire [15:0] timing_epoch,
 output wire [79:0] timing_position,training_position,next_position,output wire [34:0] symbol_step,
 output wire [2:0] peaks_used,output wire [31:0] history_generation,output wire [63:0] latest_tag,output wire [12:0] history_count,
 output wire diagnostic_valid,input wire diagnostic_ready,output wire [121:0] diagnostic_data,output wire [31:0] diagnostic_replaced,
 output reg [31:0] local_reacquisitions,dispatch_failures,output reg [2:0] last_local_reason,
 output wire startup_locked,output wire [31:0] retry_count,recovery_count,output wire recovery_waiting,
 output wire header_valid,input wire header_ready,output wire [31:0] header_id,header_context,header_generation,
 output wire [15:0] header_epoch,output wire [2:0] header_error,header_detail,output wire [191:0] header_raw,
 output wire [31:0] header_session,header_superframe,output wire [15:0] header_stream0_bytes,header_stream1_bytes,
 output wire phase_valid,input wire phase_ready,output wire [31:0] phase_id,phase_context,phase_generation,
 output wire [15:0] phase_epoch,output wire [8:0] phase_point,phase_known_point,output wire phase_tx,output wire signed [40:0] phase_value,
 output wire frame_abort,output wire status_valid,input wire status_ready,output wire [31:0] status_id,status_context,status_generation,
 output wire [15:0] status_epoch,output wire [2:0] status_error,status_detail,output wire [8:0] status_point,
 output wire payload_valid,input wire payload_ready,output wire [31:0] payload_id,payload_context,payload_generation,
 output wire [15:0] payload_epoch,output wire [14:0] payload_symbol,output wire [3:0] payload_decision,output wire [61:0] payload_metric,
 output wire packet_valid,input wire packet_ready,output wire [31:0] packet_id,packet_context,packet_generation,
 output wire [15:0] packet_epoch,output wire packet_tx,output wire [2:0] packet_slot,packet_error,
 output wire [255:0] packet_header,output wire [15:0] packet_payload_bytes,
 output wire packet_data_valid,input wire packet_data_ready,output wire [7:0] packet_data,
 output wire [15:0] packet_data_index,output wire packet_data_last,
 output wire fec_source_valid,input wire fec_source_ready,output wire [7:0] fec_source_data,
 output wire [31:0] fec_source_session,fec_source_block,output wire [4:0] fec_source_index,
 output wire [8:0] fec_source_offset,output wire fec_source_last,fec_source_recovered,fec_source_abort,
 output wire fec_event_valid,input wire fec_event_ready,output wire [31:0] fec_event_session,fec_event_block,
 output wire [2:0] fec_event_code,output wire [19:0] fec_event_missing,fec_event_delivered,
 output wire [31:0] fec_packets_accepted,fec_packets_erased,fec_packets_unmapped,
 output wire [31:0] fec_descriptors_accepted,fec_descriptors_rejected,fec_range_busy,fec_events_dropped,fec_expired_blocks,
 output wire control_valid,input wire control_ready,output wire [63:0] control_data,
 output wire [1:0] control_word,output wire control_last,
 output wire [31:0] control_session,control_superframe,output wire [95:0] control_position
);
 localparam LINK_ACTIVE=ENABLE_LINK || ENABLE_FEC;
 localparam PAYLOAD_ACTIVE=ENABLE_PAYLOAD || LINK_ACTIVE;
 wire link_input_ready,link_header_ready,link_done,link_abort,packet_valid_internal,packet_data_valid_internal;
 wire fec_meta_ready,fec_data_ready,fec_abort_internal;
 wire packet_sink_ready,packet_data_sink_ready;
 wire [2:0] link_detail;wire [4:0] link_good,link_erased;
 wire payload_sink_ready=LINK_ACTIVE?link_input_ready:payload_ready;
 wire header_sink_ready=LINK_ACTIVE?link_header_ready:header_ready;
 wire core_normal_valid,core_normal_ready,header_cfg_ready;
 wire frame_cfg_ready,payload_cfg_ready,node_ready;
 wire business_req_valid,business_req_ready,business_sample_valid,business_sample_ready,business_done,business_abort;
 wire [31:0] business_req_generation,business_req_id,business_sample_generation,business_sample_id;
 wire [63:0] business_req_tag,business_sample_tag;wire [15:0] business_req_mu,business_sample_mu;
 wire [2:0] business_sample_error,business_detail;wire [83:0] business_sample_data;
 wire rrc_valid,rrc_start,pool_ready,pool_restart,reader_active,read_enable,read_reservation,window_ready;
 wire frame_read_enable,frame_header_valid,frame_phase_valid,frame_status_valid,frame_abort_internal,payload_valid_internal;
 wire domain_clear,domain_live,boundary_live,descriptor_valid,handoff_pending;
 // The public packet boundary revokes immediately. Internal link work clears
 // on the next edge, avoiding a source-fence -> nested CRC cancel -> IO cone.
 reg link_clear;
 always @(posedge clk)begin if(rst)link_clear<=1;else link_clear<=domain_clear || frame_abort_internal;end
 wire packet_public_live=boundary_live && !frame_abort_internal && !link_clear;
 assign packet_sink_ready=ENABLE_FEC?(fec_meta_ready && packet_public_live):packet_ready;
 assign packet_data_sink_ready=ENABLE_FEC?(fec_data_ready && packet_public_live):packet_data_ready;
 assign packet_valid=packet_valid_internal && packet_public_live;
 assign packet_data_valid=packet_data_valid_internal && packet_public_live;
 wire [31:0] descriptor_id,descriptor_context,descriptor_generation;wire [15:0] descriptor_epoch;
 wire [79:0] descriptor_training;wire [34:0] descriptor_step;wire [207:0] descriptor_h;wire [63:0] descriptor_steps;
 reg window_ready_sampled;
 always @(posedge clk)begin if(rst)window_ready_sampled<=0;else window_ready_sampled<=window_ready;end
 assign boundary_live=domain_live && (!PAYLOAD_ACTIVE || window_ready || !reader_active);
 assign read_enable=frame_read_enable && boundary_live;
 assign header_valid=frame_header_valid && boundary_live;
 assign phase_valid=frame_phase_valid && boundary_live;
 assign status_valid=frame_status_valid && boundary_live;
 assign payload_valid=payload_valid_internal && boundary_live;
 assign frame_abort=!rst && (frame_abort_internal || (PAYLOAD_ACTIVE && (!domain_live || (reader_active && !window_ready))));
 wire [63:0] rrc_tag;wire [79:0] rrc_data,read_word,training_position_q;
 wire [11:0] read_address;wire [31:0] current_context;wire [34:0] step_q;
 assign normal_valid=core_normal_valid && header_cfg_ready;
 assign core_normal_ready=normal_ready && header_cfg_ready;
 generate if(PAYLOAD_ACTIVE)begin: handoff
  wire [498:0] descriptor;
  frame_domain_cost_probe domain(.clk(clk),.rst(rst),.fence(cancel || receiver_fence),
   .in_valid(core_normal_valid && normal_ready),.in_ready(header_cfg_ready),
   .in_data({normal_id,normal_context,normal_generation,normal_epoch,training_position_q,step_q,normal_h,normal_steps}),
   .cfg_valid(descriptor_valid),.cfg_ready(frame_cfg_ready && payload_cfg_ready),.cfg_data(descriptor),
   .clear(domain_clear),.live(domain_live),.pending(handoff_pending));
  assign {descriptor_id,descriptor_context,descriptor_generation,descriptor_epoch,descriptor_training,descriptor_step,descriptor_h,descriptor_steps}=descriptor;
 end else begin: direct_handoff
  assign header_cfg_ready=frame_cfg_ready && payload_cfg_ready;
  assign domain_clear=cancel || receiver_fence;assign domain_live=1'b1;assign handoff_pending=1'b0;
  assign descriptor_valid=core_normal_valid && normal_ready;
  assign descriptor_id=normal_id;assign descriptor_context=normal_context;assign descriptor_generation=normal_generation;assign descriptor_epoch=normal_epoch;
  assign descriptor_training=training_position_q;assign descriptor_step=step_q;assign descriptor_h=normal_h;assign descriptor_steps=normal_steps;
 end endgenerate
 tracking_acquiring_frontend_cost_probe #(.ENABLE_EXTERNAL_READER(1),.EXTERNAL_RESERVATION(PAYLOAD_ACTIVE),.CHECK_INPUT_TAG(CHECK_INPUT_TAG)) receiver(
 .clk(clk),
 .rst(rst),
 .cancel(cancel),
 .rearm(rearm),
 .raw_valid(raw_valid),
 .upstream_loss(upstream_loss),
 .raw_tag(raw_tag),
 .raw_data(raw_data),
 .search_valid(search_valid),
 .search_ready(search_ready),
 .search_id(search_id),
 .search_tag(search_tag),
 .phase_origin(phase_origin),
 .result_valid(result_valid),
 .result_ready(result_ready),
 .result_id(result_id),
 .result_generation(result_generation),
 .result_error(result_error),
 .result_detail(result_detail),
 .result_step(result_step),
 .result_phase(result_phase),
 .first_symbol(first_symbol),
 .training_count(training_count),
 .coarse_known(coarse_known),
 .final_commit(final_commit),
 .ingress_running(ingress_running),
 .ingress_faults(ingress_faults),
 .ingress_reason(ingress_reason),
 .ingress_peak(ingress_peak),
 .normal_valid(core_normal_valid),
 .normal_ready(core_normal_ready),
 .normal_id(normal_id),
 .normal_context(normal_context),
 .normal_generation(normal_generation),
 .normal_epoch(normal_epoch),
 .normal_h(normal_h),
 .normal_steps(normal_steps),
 .normal_selected(normal_selected),
 .normal_energies(normal_energies),
 .normal_error(normal_error),
 .normal_detail(normal_detail),
 .tracking_locked(tracking_locked),
 .receiver_fence(receiver_fence),
 .timing_update(timing_update),
 .timing_id(timing_id),
 .timing_generation(timing_generation),
 .timing_epoch(timing_epoch),
 .timing_position(timing_position),
 .training_position(training_position),
 .next_position(next_position),
 .symbol_step(symbol_step),
 .peaks_used(peaks_used),
 .history_generation(history_generation),
 .latest_tag(latest_tag),
 .history_count(history_count),
 .diagnostic_valid(diagnostic_valid),
 .diagnostic_ready(diagnostic_ready),
 .diagnostic_data(diagnostic_data),
 .diagnostic_replaced(diagnostic_replaced),
 .local_reacquisitions(local_reacquisitions),
 .dispatch_failures(dispatch_failures),
 .last_local_reason(last_local_reason),
 .startup_locked(startup_locked),
 .retry_count(retry_count),
 .recovery_count(recovery_count),
 .recovery_waiting(recovery_waiting),
 .external_reader_active(reader_active),.external_read_enable(read_enable),.external_read_address(read_address),.external_read_word(read_word),.external_read_reservation(read_reservation),
 .shared_input_valid(rrc_valid),.shared_input_start(rrc_start),.shared_input_ready(pool_ready),.shared_history_restart(pool_restart),
 .shared_input_tag(rrc_tag),.shared_input_data(rrc_data),.frontend_window_ready(window_ready),.frontend_context(current_context),
 .normal_training_position(training_position_q),.normal_symbol_step(step_q));
 frame_replay_cost_probe #(.BUSINESS_ENABLED(PAYLOAD_ACTIVE)) frame(
 .clk(clk),.rst(rst),.cancel(domain_clear),.cfg_valid(descriptor_valid && payload_cfg_ready),.cfg_ready(frame_cfg_ready),
 .cfg_id(descriptor_id),.cfg_context(descriptor_context),.cfg_generation(descriptor_generation),.cfg_epoch(descriptor_epoch),
 .cfg_training_position(descriptor_training),.cfg_symbol_step(descriptor_step),.cfg_h(descriptor_h),.cfg_phase_steps(descriptor_steps),
 .window_ready(PAYLOAD_ACTIVE?window_ready_sampled:window_ready),.current_context(current_context),
 .rrc_valid(rrc_valid),.rrc_start(rrc_start),.shared_ready(pool_ready),.rrc_tag(rrc_tag),.rrc_data(rrc_data),
 .history_generation(history_generation),.latest_tag(latest_tag),.history_count(history_count),.history_restart(pool_restart),
 .reader_active(reader_active),.read_enable(frame_read_enable),.read_address(read_address),.read_word(read_word),.read_reservation(read_reservation),
 .header_valid(frame_header_valid),.header_ready(header_sink_ready),.header_id(header_id),.header_context(header_context),.header_generation(header_generation),.header_epoch(header_epoch),
 .header_error(header_error),.header_detail(header_detail),.header_raw(header_raw),.header_session(header_session),.header_superframe(header_superframe),
 .header_stream0_bytes(header_stream0_bytes),.header_stream1_bytes(header_stream1_bytes),
 .phase_valid(frame_phase_valid),.phase_ready(PAYLOAD_ACTIVE?node_ready:phase_ready),.phase_id(phase_id),.phase_context(phase_context),.phase_generation(phase_generation),
 .phase_epoch(phase_epoch),.phase_point(phase_point),.phase_known_point(phase_known_point),.phase_tx(phase_tx),.phase_value(phase_value),
 .frame_abort(frame_abort_internal),.status_valid(frame_status_valid),.status_ready(status_ready),.status_id(status_id),.status_context(status_context),.status_generation(status_generation),
 .status_epoch(status_epoch),.status_error(status_error),.status_detail(status_detail),.status_point(status_point),
 .business_req_valid(business_req_valid),.business_req_ready(business_req_ready),.business_req_generation(business_req_generation),.business_req_id(business_req_id),.business_req_tag(business_req_tag),.business_req_mu(business_req_mu),
 .business_sample_valid(business_sample_valid),.business_sample_ready(business_sample_ready),.business_sample_generation(business_sample_generation),.business_sample_id(business_sample_id),.business_sample_tag(business_sample_tag),.business_sample_mu(business_sample_mu),.business_sample_error(business_sample_error),.business_sample_data(business_sample_data),
 .business_done(business_done && (!LINK_ACTIVE || link_done)),.business_abort(business_abort || (LINK_ACTIVE && link_abort)),.business_detail(LINK_ACTIVE && link_abort?3'd7:business_detail));
 // In business mode phase outputs remain observable, while their consumer is
 // the PL node queue. Diagnostic phase_ready cannot stall the RF data plane.
 generate if(PAYLOAD_ACTIVE)begin: business
  payload_schedule_cost_probe payload(.clk(clk),.rst(rst),.cancel(domain_clear || frame_abort_internal),
   .cfg_valid(descriptor_valid && frame_cfg_ready),.cfg_ready(payload_cfg_ready),
   .cfg_id(descriptor_id),.cfg_context(descriptor_context),.cfg_generation(descriptor_generation),.cfg_epoch(descriptor_epoch),.cfg_training_position(descriptor_training),.cfg_symbol_step(descriptor_step),.cfg_h(descriptor_h),.cfg_phase_steps(descriptor_steps),
   .trusted_header(frame_header_valid && header_error==0),.node_valid(frame_phase_valid),.node_ready(node_ready),.node_id(phase_id),.node_context(phase_context),.node_generation(phase_generation),.node_epoch(phase_epoch),.node_point(phase_point),.node_known(phase_known_point),.node_tx(phase_tx),.node_phase(phase_value),
   .req_valid(business_req_valid),.req_ready(business_req_ready),.req_generation(business_req_generation),.req_id(business_req_id),.req_tag(business_req_tag),.req_mu(business_req_mu),
   .sample_valid(business_sample_valid),.sample_ready(business_sample_ready),.sample_generation(business_sample_generation),.sample_id(business_sample_id),.sample_tag(business_sample_tag),.sample_mu(business_sample_mu),.sample_error(business_sample_error),.sample_data(business_sample_data),
   .out_valid(payload_valid_internal),.out_ready(payload_sink_ready),.out_id(payload_id),.out_context(payload_context),.out_generation(payload_generation),.out_epoch(payload_epoch),.out_symbol(payload_symbol),.out_decision(payload_decision),.out_metric(payload_metric),.done(business_done),.abort(business_abort),.detail(business_detail),.active());
 end else begin: no_business
  assign payload_cfg_ready=1'b1;assign node_ready=1'b0;assign business_req_valid=1'b0;assign business_req_generation=0;assign business_req_id=0;assign business_req_tag=0;assign business_req_mu=0;
  assign business_sample_ready=1'b1;assign business_done=1'b1;assign business_abort=1'b0;assign business_detail=0;
  assign payload_valid_internal=1'b0;assign payload_id=0;assign payload_context=0;assign payload_generation=0;assign payload_epoch=0;assign payload_symbol=0;assign payload_decision=0;assign payload_metric=0;
 end endgenerate
 generate if(LINK_ACTIVE)begin: link
  payload_link_cost_probe packets(.clk(clk),.rst(rst),.cancel(link_clear),
   .cfg_valid(frame_header_valid && header_error==0),.cfg_ready(link_header_ready),
   .cfg_id(header_id),.cfg_context(header_context),.cfg_generation(header_generation),.cfg_epoch(header_epoch),
   .cfg_session(header_session),.cfg_superframe(header_superframe),.cfg_stream0_bytes(header_stream0_bytes),.cfg_stream1_bytes(header_stream1_bytes),
   .in_valid(payload_valid),.in_ready(link_input_ready),.in_id(payload_id),.in_context(payload_context),.in_generation(payload_generation),.in_epoch(payload_epoch),.in_symbol(payload_symbol),.in_decision(payload_decision),
   .result_valid(packet_valid_internal),.result_ready(packet_sink_ready),.result_id(packet_id),.result_context(packet_context),.result_generation(packet_generation),.result_epoch(packet_epoch),
   .result_tx(packet_tx),.result_slot(packet_slot),.result_error(packet_error),.result_header(packet_header),.result_payload_bytes(packet_payload_bytes),
   .out_valid(packet_data_valid_internal),.out_ready(packet_data_sink_ready),.out_data(packet_data),.out_index(packet_data_index),.out_last(packet_data_last),
   .done(link_done),.abort(link_abort),.detail(link_detail),.accepted_packets(link_good),.erased_packets(link_erased));
 end else begin: no_link
  assign link_input_ready=1'b1;assign link_header_ready=1'b1;assign link_done=1'b1;assign link_abort=1'b0;assign link_detail=0;assign link_good=0;assign link_erased=0;
  assign packet_valid_internal=1'b0;assign packet_data_valid_internal=1'b0;assign packet_id=0;assign packet_context=0;assign packet_generation=0;assign packet_epoch=0;
  assign packet_tx=0;assign packet_slot=0;assign packet_error=0;assign packet_header=0;assign packet_payload_bytes=0;assign packet_data=0;assign packet_data_index=0;assign packet_data_last=0;
 end endgenerate
 generate if(ENABLE_FEC)begin: block_fec
  // RF frame loss only revokes the currently streaming packet. Committed banks
  // continue their independent recovery/deadlines. Explicit STOP/cancel resets
  // the session and revokes any incomplete SOURCE for the downstream owner.
  link_fec_ingress_cost_probe ingress(.clk(clk),.rst(rst || cancel),.packet_abort(frame_abort),
   .result_valid(packet_valid),.result_ready(fec_meta_ready),.result_error(packet_error),
   .result_header(packet_header),.result_payload_bytes(packet_payload_bytes),
   .reference_valid(packet_id==descriptor_id && packet_context==descriptor_context &&
     packet_generation==descriptor_generation && packet_epoch==descriptor_epoch),
   .reference_position({descriptor_epoch,descriptor_training}),
   .control_valid(control_valid),.control_ready(control_ready),.control_data(control_data),
   .control_word(control_word),.control_last(control_last),.control_session(control_session),
   .control_superframe(control_superframe),.control_position(control_position),
   .payload_valid(packet_data_valid),.payload_ready(fec_data_ready),.payload_data(packet_data),
   .payload_index(packet_data_index),.payload_last(packet_data_last),
   .source_valid(fec_source_valid),.source_ready(fec_source_ready),.source_data(fec_source_data),
   .source_session(fec_source_session),.source_block(fec_source_block),.source_index(fec_source_index),
   .source_offset(fec_source_offset),.source_last(fec_source_last),.source_recovered(fec_source_recovered),.source_abort(fec_abort_internal),
   .event_valid(fec_event_valid),.event_ready(fec_event_ready),.event_session(fec_event_session),.event_block(fec_event_block),
   .event_code(fec_event_code),.event_missing(fec_event_missing),.event_delivered(fec_event_delivered),
   .packets_accepted(fec_packets_accepted),.packets_erased(fec_packets_erased),.packets_unmapped(fec_packets_unmapped),
   .descriptors_accepted(fec_descriptors_accepted),.descriptors_rejected(fec_descriptors_rejected),
   .range_busy(fec_range_busy),.events_dropped(fec_events_dropped),.expired_blocks(fec_expired_blocks));
  assign fec_source_abort=fec_abort_internal || (!rst && cancel);
 end else begin: no_fec
  assign fec_meta_ready=1'b1;assign fec_data_ready=1'b1;assign fec_abort_internal=1'b0;
  assign fec_source_valid=1'b0;assign fec_source_data=0;assign fec_source_session=0;assign fec_source_block=0;
  assign fec_source_index=0;assign fec_source_offset=0;assign fec_source_last=1'b0;assign fec_source_recovered=1'b0;assign fec_source_abort=1'b0;
  assign fec_event_valid=1'b0;assign fec_event_session=0;assign fec_event_block=0;assign fec_event_code=0;assign fec_event_missing=0;assign fec_event_delivered=0;
  assign fec_packets_accepted=0;assign fec_packets_erased=0;assign fec_packets_unmapped=0;assign fec_descriptors_accepted=0;
  assign fec_descriptors_rejected=0;assign fec_range_busy=0;assign fec_events_dropped=0;assign fec_expired_blocks=0;
  assign control_valid=0;assign control_data=0;assign control_word=0;assign control_last=0;
  assign control_session=0;assign control_superframe=0;assign control_position=0;
 end endgenerate
endmodule
