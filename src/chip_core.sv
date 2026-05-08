// SPDX-FileCopyrightText: © 2026 CSE-127 NOC Group
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

    input  wire clk,
    input  wire rst_n,

    input  wire [NUM_INPUT_PADS-1:0] input_in,
    output wire [NUM_INPUT_PADS-1:0] input_pu,
    output wire [NUM_INPUT_PADS-1:0] input_pd,

    input  wire [NUM_BIDIR_PADS-1:0] bidir_in,
    output wire [NUM_BIDIR_PADS-1:0] bidir_out,
    output wire [NUM_BIDIR_PADS-1:0] bidir_oe,
    output wire [NUM_BIDIR_PADS-1:0] bidir_cs,
    output wire [NUM_BIDIR_PADS-1:0] bidir_sl,
    output wire [NUM_BIDIR_PADS-1:0] bidir_ie,
    output wire [NUM_BIDIR_PADS-1:0] bidir_pu,
    output wire [NUM_BIDIR_PADS-1:0] bidir_pd,

    inout  wire [NUM_ANALOG_PADS-1:0] analog
);

    // Unused pad controls
    assign input_pu = '0;
    assign input_pd = '0;

    // Set the bidir as output
    assign bidir_out[0] = spi_miso;
    assign bidir_oe = {{NUM_BIDIR_PADS-4{1'b0}}, 4'b1111};  // [3:1]=flash outputs, [0]=miso
    assign bidir_cs = '0;
    assign bidir_sl = '0;
    assign bidir_pu = '0;
    assign bidir_pd = '0;

    // -------------------------------------------------------------------------
    // Pad assignments (1x1 slot: 12 input pads, 40 bidir pads)
    //
    // Input pads:
    //   input_in[0]  = flash_miso    (boot flash MISO)
    //   input_in[1]  = spi_csb       (host SPI chip select, active low)
    //   input_in[2]  = spi_sclk      (host SPI clock)
    //   input_in[3]  = spi_mosi      (host SPI MOSI)
    //
    // Bidir pads (outputs):
    //   bidir[0]     = flash_cs_n    (boot flash CS)
    //   bidir[1]     = flash_clk     (boot flash CLK)
    //   bidir[2]     = flash_mosi    (boot flash MOSI)
    //   bidir[3]     = spi_miso      (host SPI MISO back to host)
    //   bidir[4:37]  = monitor_out   (34-bit mesh monitor output)
    // -------------------------------------------------------------------------

    // Flash SPI (boot controller side)
    wire flash_miso  = input_in[0];
    wire flash_cs_n, flash_clk, flash_mosi_out;

    // Host SPI (gateway side)
    wire spi_csb   = input_in[1];
    wire spi_sclk  = input_in[2];
    wire spi_mosi  = input_in[3];
    wire spi_miso_out;

    // Mesh monitor output — connect to pads to prevent Yosys optimization
    wire [33:0] monitor_out;

    // Bidir pad direction: outputs for flash(3) + spi_miso(1) + monitor(34) = 38
    // bidir[0..2] = flash outputs, bidir[3] = spi_miso, bidir[4..37] = monitor
    assign bidir_oe  = {{(NUM_BIDIR_PADS-38){1'b0}}, {34{1'b1}}, 1'b1, 3'b111};
    assign bidir_ie  = ~bidir_oe;
    assign bidir_out = {{(NUM_BIDIR_PADS-38){1'b0}}, monitor_out, spi_miso_out, flash_mosi_out, flash_clk, flash_cs_n};

    
    
    logic _unused;
    assign _unused = &{bidir_in, analog};

    // -------------------------------------------------------------------------
    // mesh_3x3 — 3x3 grid of SERV RISC-V cores
    // -------------------------------------------------------------------------
    mesh_3x3 mesh_inst (
        .clk          (clk),
        .rst          (~rst_n),
        .inject_00_nw (34'b0),
        .monitor_22_se(monitor_out),
        .flash_miso   (flash_miso),
        .flash_cs_n   (flash_cs_n),
        .flash_clk    (flash_clk),
        .flash_mosi   (flash_mosi_out)
    );

    // -------------------------------------------------------------------------
    // host_spi_slave — Host to chip SPI gateway
    // -------------------------------------------------------------------------
    host_spi_slave gateway_inst (
        .sys_clk    (clk),
        .sys_rst    (~rst_n),
        .spi_csb    (spi_csb),
        .spi_sclk   (spi_sclk),
        .spi_mosi   (spi_mosi),
        .spi_miso   (spi_miso_out),
        // SRAM write bus — tied off for now (boot_controller owns this)
        .sram_waddr (),
        .sram_wdata (),
        .sram_wen   (),
        // Reset control — tied off for now
        .host_rst   (),
        .host_rst_en(),
        // Readback — tied off for now
        .rd_tile    (),
        .rd_addr    (),
        .rd_req     (),
        .rd_data    (8'b0)
    );
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
    wire        spi_miso;

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

    // Ethan: mesh 3x3 instantiation:
    mesh_3x3 u_mesh (
        .clk          (clk),
        .rst          (~rst_n),
        .inject_00_nw (34'b0),       // tie off unused NoC injection for now
        .monitor_22_se(),

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

// // SPDX-FileCopyrightText: © 2025 XXX Authors
// // SPDX-License-Identifier: Apache-2.0

// `default_nettype none

// module chip_core #(
//     parameter NUM_INPUT_PADS,
//     parameter NUM_BIDIR_PADS,
//     parameter NUM_ANALOG_PADS
//     )(
//     `ifdef USE_POWER_PINS
//     inout  wire VDD,
//     inout  wire VSS,
//     `endif
    
//     input  wire clk,       // clock
//     input  wire rst_n,     // reset (active low)
    
//     input  wire [NUM_INPUT_PADS-1:0] input_in,   // Input value
//     output wire [NUM_INPUT_PADS-1:0] input_pu,   // Pull-up
//     output wire [NUM_INPUT_PADS-1:0] input_pd,   // Pull-down

//     input  wire [NUM_BIDIR_PADS-1:0] bidir_in,   // Input value
//     output wire [NUM_BIDIR_PADS-1:0] bidir_out,  // Output value
//     output wire [NUM_BIDIR_PADS-1:0] bidir_oe,   // Output enable
//     output wire [NUM_BIDIR_PADS-1:0] bidir_cs,   // Input type (0=CMOS Buffer, 1=Schmitt Trigger)
//     output wire [NUM_BIDIR_PADS-1:0] bidir_sl,   // Slew rate (0=fast, 1=slow)
//     output wire [NUM_BIDIR_PADS-1:0] bidir_ie,   // Input enable
//     output wire [NUM_BIDIR_PADS-1:0] bidir_pu,   // Pull-up
//     output wire [NUM_BIDIR_PADS-1:0] bidir_pd,   // Pull-down

//     inout  wire [NUM_ANALOG_PADS-1:0] analog  // Analog
// );

//     // See here for usage: https://gf180mcu-pdk.readthedocs.io/en/latest/IPs/IO/gf180mcu_fd_io/digital.html
    
//     // Disable pull-up and pull-down for input
//     assign input_pu = '0;
//     assign input_pd = '0;

//     // Set the bidir as output
//     assign bidir_oe = '1;
//     assign bidir_cs = '0;
//     assign bidir_sl = '0;
//     assign bidir_ie = ~bidir_oe;
//     assign bidir_pu = '0;
//     assign bidir_pd = '0;
    
//     logic _unused;
//     assign _unused = &bidir_in;

//     logic [NUM_BIDIR_PADS-1:0] count;

//     always_ff @(posedge clk) begin
//         if (!rst_n) begin
//             count <= '0;
//         end else begin
//             if (&input_in) begin
//                 count <= count + 1;
//             end
//         end
//     end

//     logic [7:0] sram_0_out;

//     gf180mcu_ocd_ip_sram__sram512x8m8wm1 sram_0 (
//         `ifdef USE_POWER_PINS
//         .VDD  (VDD),
//         .VSS  (VSS),
//         `endif

//         .CLK  (clk),
//         .CEN  (1'b1),
//         .GWEN (1'b0),
//         .WEN  (8'b0),
//         .A    ('0),
//         .D    ('0),
//         .Q    (sram_0_out)
//     );

//     logic [7:0] sram_1_out;

//     gf180mcu_ocd_ip_sram__sram512x8m8wm1 sram_1 (
//         `ifdef USE_POWER_PINS
//         .VDD  (VDD),
//         .VSS  (VSS),
//         `endif

//         .CLK  (clk),
//         .CEN  (1'b1),
//         .GWEN (1'b0),
//         .WEN  (8'b0),
//         .A    ('0),
//         .D    ('0),
//         .Q    (sram_1_out)
//     );

//     assign bidir_out = count ^ {24'd0, sram_0_out, sram_1_out};

// endmodule

// `default_nettype wire
