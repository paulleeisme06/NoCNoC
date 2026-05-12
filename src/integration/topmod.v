module topmod (
    input  wire clk,
    input  wire reset,

    // Physical Pins
    input  wire flash_miso,
    output wire flash_mosi,
    output wire flash_clk,
    output wire flash_csb,

    // Host Pins
    input  wire bypass_en,
    input  wire host_mosi,

    // Wishbone Bus (NoC Gateway)
    output wire [31:0] wbs_adr,
    output wire [31:0] wbs_dat,
    output wire        wbs_cyc,
    output wire        wbs_stb,
    output wire        wbs_we,
    input  wire        wbs_ack
);

    wire fetch_en;
    wire h_csb;
    wire [31:0] shifted_word;
    wire word_ready;
    wire fetch_o;

    // Flash Clock Module (Flashs clock is slower than systems clock)
    flash_clk slow_clk (
        .clk(clk),
        .reset(reset),
        .enable(fetch_en),
        .flash_clk(flash_clk)
    );

    //FSM Module
    housekeeping_fsm FSM(
        .clk(clk),
        .reset(reset),
        .bypass_en(bypass_en),
        .word_ready(word_ready),
        .shifted_word(shifted_word),
        .fetch_en(fetch_en),
        .fetch_o(fetch_o),
        .flash_csb(h_csb),
        .wbs_adr(wbs_adr),
        .wbs_dat(wbs_dat),
        .wbs_cyc(wbs_cyc),
        .wbs_stb(wbs_stb),
        .wbs_we(wbs_we),
        .wbs_ack(wbs_ack)
    );

    reg flash_clk_delayed;
    wire flash_tick;

    always @(posedge clk) flash_clk_delayed <= flash_clk;
    assign flash_tick = (flash_clk && !flash_clk_delayed); // High for 1 fast clock cycle

    // Shift Register
    shiftregister word (
        .clk(clk),
        .reset(reset || bypass_en),
        .serial_in(flash_miso),
        .shift_en(flash_tick),
        .fetch_o(fetch_o),
        .shifted_word(shifted_word),
        .done_word(word_ready)
    );

    wire [9:0] sram_addr;
    wire [7:0] sram_wen;
    wire [7:0] sram_dout;
    wire sram_cen;
    wire [7:0] sram_din;

    // SRAM
    gf180mcu_ocd_ip_sram__sram1024x8m8wm1 sram (
        .CLK(clk),
        .CEN(sram_cen),
        .WEN(sram_wen),
        .GWEN(!wbs_we),
        .A(sram_addr),
        .D(sram_din[7:0]),
        .Q(sram_dout)
    );

    reg [1:0] byte_sel;
    always @(posedge clk) begin
        if (reset) byte_sel <= 0;
        else if (wbs_stb && wbs_we) byte_sel <= byte_sel + 1;
    end

    assign sram_din = (byte_sel == 2'd0) ? wbs_dat[7:0]  :
                      (byte_sel == 2'd1) ? wbs_dat[15:8]  :
                      (byte_sel == 2'd2) ? wbs_dat[23:16] :
                                           wbs_dat[31:24];

    assign sram_addr = ((wbs_adr - 32'h1000) + byte_sel);

    assign flash_mosi = (bypass_en) ? host_mosi : 1'b0;
    assign flash_csb  = (bypass_en) ? 1'b0 : h_csb;

    assign sram_cen = !wbs_stb;
    assign sram_wen = {8'd0};

    reg wbs_ack_r;
    always @(posedge clk) wbs_ack_r <= wbs_stb;

endmodule
