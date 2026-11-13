`timescale 1ns/1ps
// One outstanding interpolation request. Capture is four S16 Q12 lanes;
// output is four S32 Q15 lanes. Integer coordinates are never rounded.
// Capture RAM must acknowledge each rd_en with exactly one rd_valid response.
// This helper does NOT choose timing/SFO or substitute a fixed phase for it.
module rfv2_fractional_reader (
    input wire clk,input wire resetn,
    input wire req_valid,output wire req_ready,
    input wire [63:0] req_floor,input wire [15:0] req_fraction,
    output wire rd_en,output wire [63:0] rd_sample,
    input wire rd_valid,input wire [63:0] rd_data,
    output reg m_valid,input wire m_ready,output reg [127:0] m_data
);
    // Keep the historical encodings through EMIT stable: integration
    // diagnostics observe them hierarchically. CALC is the only new state.
    localparam IDLE=0, READ_A=1, WAIT_A=2, READ_B=3, WAIT_B=4, EMIT=5, CALC=6;
    reg [2:0] state;
    reg [63:0] floor_q,a_q;
    reg [15:0] fraction_q;
    reg signed [33:0] product_q[0:3];
    wire chain_b_read=(state==WAIT_A)&&rd_valid&&(fraction_q!=0);
    assign req_ready=resetn&&(state==IDLE);
    // A response retires the only outstanding read on this edge, so the B
    // request may be accepted on the same edge without creating two owners.
    // This removes the historical READ_B request bubble and pays for CALC.
    assign rd_en=resetn&&((state==READ_A)||chain_b_read);
    assign rd_sample=floor_q+(chain_b_read?64'd1:64'd0);
    function automatic signed [31:0] finish_interpolate(
        input signed [15:0] a,input signed [33:0] product);
        reg [33:0] magnitude,rounded;
        reg signed [31:0] term,base;
        begin
            magnitude=product[33]?-product:product;
            // Q12 * Q16 -> Q15: right shift13, nearest-even symmetric.
            rounded=(magnitude>>13)+((magnitude[12:0]>13'd4096)||
                ((magnitude[12:0]==13'd4096)&&magnitude[13]));
            term=product[33]?-$signed(rounded[31:0]):$signed(rounded[31:0]);
            base=$signed(a);finish_interpolate=(base<<<3)+term;
        end
    endfunction
    integer lane;
    always @(posedge clk)begin
        if(!resetn)begin
            state<=IDLE;floor_q<=0;fraction_q<=0;a_q<=0;m_valid<=0;m_data<=0;
            for(lane=0;lane<4;lane=lane+1)product_q[lane]<=0;
        end
        else case(state)
            IDLE:if(req_valid)begin floor_q<=req_floor;fraction_q<=req_fraction;state<=READ_A;end
            READ_A:state<=WAIT_A;
            WAIT_A:if(rd_valid)begin
                a_q<=rd_data;
                if(fraction_q==0)begin
                    for(lane=0;lane<4;lane=lane+1)
                        m_data[lane*32+:32]<=$signed(rd_data[lane*16+:16])<<<3;
                    m_valid<=1;state<=EMIT;
                end else state<=WAIT_B;
            end
            WAIT_B:if(rd_valid)begin
                for(lane=0;lane<4;lane=lane+1)
                    product_q[lane]<=($signed({rd_data[lane*16+15],rd_data[lane*16+:16]})-
                        $signed({a_q[lane*16+15],a_q[lane*16+:16]}))*$signed({1'b0,fraction_q});
                state<=CALC;
            end
            CALC:begin
                for(lane=0;lane<4;lane=lane+1)
                    m_data[lane*32+:32]<=finish_interpolate(a_q[lane*16+:16],product_q[lane]);
                m_valid<=1;state<=EMIT;
            end
            EMIT:if(m_ready)begin m_valid<=0;state<=IDLE;end
            default:begin state<=IDLE;m_valid<=0;end
        endcase
    end
endmodule
