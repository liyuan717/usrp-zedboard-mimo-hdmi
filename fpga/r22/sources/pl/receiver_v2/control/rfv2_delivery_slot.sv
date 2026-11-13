`timescale 1ns/1ps

// Wrap exact RFD1/RFE1 in a fixed 512-byte RFS2 DMA slot. This is a local
// hardware/software boundary only; neither over-air nor HDMI bytes change.
module rfv2_delivery_slot (
    input wire clk, input wire resetn, input wire flush,
    input wire [31:0] epoch, input wire [63:0] first_sample,
    input wire [63:0] s_data, input wire [7:0] s_keep,
    input wire s_valid, output wire s_ready, input wire s_last,
    output wire [63:0] m_data, output wire [7:0] m_keep,
    output wire m_valid, input wire m_ready, output wire m_last,
    output logic protocol_error,
    output wire busy,output wire rejected_record
);
    typedef enum logic [2:0] {IDLE,RECORD,PAD,HCRC,HSTORE,OREAD,OSEND,FAULT} state_t;
    state_t state;
    (* ram_style="block" *) logic [63:0] ram [0:63];
    logic [63:0] ram_q;
    logic [319:0] header;
    logic [31:0] slot_sequence, crc;
    logic [5:0] index, last_record, crc_index;
    logic [2:0] header_index;
    logic rejected;
    logic flush_q, flush_pending, committed;
    wire flush_event=flush&&!flush_q;
    wire live=resetn&&(!flush||committed);
    assign s_ready=resetn&&!flush&&!flush_pending&&(state==IDLE||state==RECORD);
    // Once output is committed, a soft flush cannot truncate a DMA slot or
    // revoke an asserted TVALID under backpressure. Hardware reset requires
    // the external DMA owner to quiesce/reset its descriptor too.
    assign m_valid=resetn&&state==OSEND;
    assign m_data=ram_q;
    assign m_keep=8'hff;
    assign m_last=m_valid&&index==63;
    assign busy=resetn&&state!=IDLE&&state!=FAULT;
    assign rejected_record=rejected;

    function automatic [31:0] crc_byte(input [31:0] c0,input [7:0] b);
        reg [31:0] c; integer j;
        begin
            c=c0^{24'd0,b};
            for(j=0;j<8;j=j+1)c=(c>>1)^(c[0]?32'hedb88320:0);
            crc_byte=c;
        end
    endfunction
    logic we,re;
    logic [5:0] wa,ra;
    logic [63:0] wd;
    always_comb begin
        we=0;re=0;wa=0;ra=0;wd=0;
        case(state)
            IDLE:if(s_valid)begin we=1;wa=5;wd=s_data;end
            RECORD:if(s_valid)begin we=1;wa=index;wd=s_last?{32'd0,s_data[31:0]}:s_data;end
            PAD:begin we=1;wa=index;wd=0;end
            HSTORE:begin we=1;wa={3'd0,header_index};wd=header[64*header_index+:64];end
            OREAD:begin re=1;ra=index;end
            default:begin end
        endcase
    end
    always_ff @(posedge clk)begin
        if(live&&we)ram[wa]<=wd;
        if(live&&re)ram_q<=ram[ra];
    end
    always_ff @(posedge clk)begin
        if(!resetn)begin
            state<=IDLE;header<=0;slot_sequence<=0;crc<=0;index<=0;
            last_record<=0;crc_index<=0;header_index<=0;rejected<=0;protocol_error<=0;
            flush_q<=0;flush_pending<=0;committed<=0;
        end else begin
            flush_q<=flush;
            if(flush_event)begin
                if(committed)flush_pending<=1;
                else begin
                    state<=IDLE;protocol_error<=0;flush_pending<=0;
                    // One abandoned admitted slot consumes one identity,
                    // regardless of how many clocks flush stays asserted.
                    if(state!=IDLE&&state!=FAULT&&slot_sequence!=32'hffffffff)
                        slot_sequence<=slot_sequence+1'b1;
                end
            end
            if(!(flush_event&&!committed))case(state)
            IDLE:if(!flush&&!flush_pending&&s_valid)begin
                if(s_keep!=8'hff||s_last||slot_sequence==32'hffffffff||
                   (s_data[31:0]!=32'h31444652&&s_data[31:0]!=32'h31454652))begin
                    protocol_error<=1;state<=FAULT;
                end else begin
                    rejected<=s_data[31:0]==32'h31454652;
                    last_record<=s_data[31:0]==32'h31454652 ? 7 : 45;
                    header[63:0]<={16'd40,16'd1,32'h32534652};
                    header[127:64]<={s_data[31:0]==32'h31454652 ? 32'd20 : 32'd324,32'd512};
                    header[191:128]<={s_data[31:0]==32'h31454652 ? 32'd1 : 32'd0,epoch};
                    header[255:192]<=first_sample;
                    header[319:256]<={32'd0,slot_sequence};
                    index<=6;state<=RECORD;
                end
            end
            RECORD:if(!flush&&s_valid)begin
                if(s_last!=(index==last_record)||s_keep!=(index==last_record?8'h0f:8'hff)||
                   (index==6 && (s_data[31:0]==0 || s_data[63:32]!=(rejected?0:304))))begin
                    protocol_error<=1;state<=FAULT;
                end else if(index==last_record)begin index<=index+1'b1;state<=PAD;end
                else index<=index+1'b1;
            end
            PAD:if(index==63)begin crc<=32'hffffffff;crc_index<=0;state<=HCRC;end
                else index<=index+1'b1;
            HCRC:begin
                crc<=crc_byte(crc,header[8*crc_index+:8]);
                if(crc_index==35)begin
                    header[319:288]<=~crc_byte(crc,header[8*crc_index+:8]);
                    header_index<=0;state<=HSTORE;
                end else crc_index<=crc_index+1'b1;
            end
            HSTORE:if(header_index==4)begin index<=0;committed<=1;state<=OREAD;end
                else header_index<=header_index+1'b1;
            OREAD:state<=OSEND;
            OSEND:if(m_ready)begin
                if(index==63)begin
                    if(slot_sequence!=32'hffffffff)slot_sequence<=slot_sequence+1'b1;
                    committed<=0;flush_pending<=0;state<=IDLE;
                end
                else begin index<=index+1'b1;state<=OREAD;end
            end
            default:state<=FAULT;
            endcase
        end
    end
endmodule
