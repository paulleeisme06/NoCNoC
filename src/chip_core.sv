// SPDX-FileCopyrightText: © 2025 XXX Authors
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

module chip_core #(
    parameter NUM_INPUT_PADS,
    parameter NUM_BIDIR_PADS,
    parameter NUM_ANALOG_PADS
    )(
    `ifdef USE_POWER_PINS
    inout  wire VDD,
    inout  wire VSS,
    `endif
    
    input  wire clk,       // clock
    input  wire rst_n,     // reset (active low)
    
    input  wire [NUM_INPUT_PADS-1:0] input_in,   // Input value
    output wire [NUM_INPUT_PADS-1:0] input_pu,   // Pull-up
    output wire [NUM_INPUT_PADS-1:0] input_pd,   // Pull-down

    input  wire [NUM_BIDIR_PADS-1:0] bidir_in,   // Input value
    output wire [NUM_BIDIR_PADS-1:0] bidir_out,  // Output value
    output wire [NUM_BIDIR_PADS-1:0] bidir_oe,   // Output enable
    output wire [NUM_BIDIR_PADS-1:0] bidir_cs,   // Input type (0=CMOS Buffer, 1=Schmitt Trigger)
    output wire [NUM_BIDIR_PADS-1:0] bidir_sl,   // Slew rate (0=fast, 1=slow)
    output wire [NUM_BIDIR_PADS-1:0] bidir_ie,   // Input enable
    output wire [NUM_BIDIR_PADS-1:0] bidir_pu,   // Pull-up
    output wire [NUM_BIDIR_PADS-1:0] bidir_pd,   // Pull-down

    inout  wire [NUM_ANALOG_PADS-1:0] analog  // Analog
);

    // See here for usage: https://gf180mcu-pdk.readthedocs.io/en/latest/IPs/IO/gf180mcu_fd_io/digital.html
    
    // Disable pull-up and pull-down for input
    assign input_pu = '0;
    assign input_pd = '0;

    // moved this declaration up since its used in line 45
    wire        spi_miso;

    // Set the bidir as output
    assign bidir_out[0] = spi_miso;
    assign bidir_out[NUM_BIDIR_PADS-1:4] = '0;
    assign bidir_oe = {{NUM_BIDIR_PADS-4{1'b0}}, 4'b1111};  // [3:1]=flash outputs, [0]=miso
    assign bidir_cs = '0;
    assign bidir_sl = '0;
    assign bidir_ie = ~bidir_oe;
    assign bidir_pu = '0;
    assign bidir_pd = '0;
    
    
    logic _unused;
    assign _unused = &bidir_in;

    // logic [NUM_BIDIR_PADS-1:0] count;

    // always_ff @(posedge clk) begin
    //     if (!rst_n) begin
    //         count <= '0;
    //     end else begin
    //         if (&input_in) begin
    //             count <= count + 1;
    //         end
    //     end
    // end

    // Ethan: SPI pad assignments and instantiation:

    wire spi_clk  = input_in[0];
    wire spi_mosi = input_in[1];
    wire spi_cs_n = input_in[2];
    wire dft_mode = input_in[3];

    wire       dbg_req, dbg_we;
    wire [3:0] dbg_tile_id;
    wire [10:0] dbg_addr;
    wire [7:0]  dbg_wdata;
    wire [7:0]  dbg_rdata;
    wire        dbg_ack;
    

    spi_debug my_spi_debug (
        .clk        (clk),
        .rst        (~rst_n),
        .spi_clk    (spi_clk),
        .spi_cs_n   (spi_cs_n),
        .spi_mosi   (spi_mosi),
        .spi_miso   (spi_miso),
        .dbg_req    (dbg_req),
        .dbg_we     (dbg_we),
        .dbg_tile_id(dbg_tile_id),
        .dbg_addr   (dbg_addr),
        .dbg_wdata  (dbg_wdata),
        .dbg_rdata  (dbg_rdata),
        .dbg_ack    (dbg_ack)
    );

    reg dbg_ack_r;
    always @(posedge clk) begin
        if (!rst_n) begin
            dbg_ack_r <= 1'b0;
        end else begin
            dbg_ack_r <= dbg_req & ~dbg_we;
        end
    end
    assign dbg_ack = dbg_ack_r;

    mesh_rxc #(.MESH_R(3), .MESH_C(4)) u_mesh (
        .clk          (clk),
        .rst          (~rst_n),
        .inject_00_nw (36'b0),
        .monitor_se   (),

        // Flash boot pins — assign to remaining input/bidir pads
        .flash_miso   (input_in[4]),
        .flash_cs_n   (bidir_out[1]),
        .flash_clk    (bidir_out[2]),
        .flash_mosi   (bidir_out[3]),

        // DFT
        .dft_mode     (dft_mode),
        .dft_tile_id  (dbg_tile_id),
        .dft_we       (dbg_we & dbg_req),
        .dft_addr     (dbg_addr),
        .dft_wdata    (dbg_wdata),
        .dft_rdata    (dbg_rdata)
    );

    


endmodule

`default_nettype wire
