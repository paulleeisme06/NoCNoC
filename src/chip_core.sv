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

    // See: https://gf180mcu-pdk.readthedocs.io/en/latest/IPs/IO/gf180mcu_fd_io/digital.html
    assign input_pu = '0;
    assign input_pd = '0;

    // bidir_out[0]   = host SPI MISO (driven by host_spi_slave below)
    // bidir_out[3:1] = flash CS_N / CLK / MOSI (driven by mesh_3x3 outputs below)
    assign bidir_oe = {{NUM_BIDIR_PADS-4{1'b0}}, 4'b1111};
    assign bidir_cs = '0;
    assign bidir_sl = '0;
    assign bidir_ie = ~bidir_oe;
    assign bidir_pu = '0;
    assign bidir_pd = '0;

    logic _unused;
    assign _unused = &{bidir_in, input_in[3]};

    // -----------------------------------------------------------------------
    // Pad assignments
    // input_in[0] = host SPI SCLK  (from host)
    // input_in[1] = host SPI MOSI  (from host)
    // input_in[2] = host SPI CSB   (from host, active-low)
    // input_in[3] = spare
    // input_in[4] = flash MISO     (from flash chip)
    // bidir_out[0] = host SPI MISO (to host)
    // bidir_out[1] = flash CS_N    (to flash chip)
    // bidir_out[2] = flash CLK     (to flash chip)
    // bidir_out[3] = flash MOSI    (to flash chip)
    // -----------------------------------------------------------------------
    wire spi_sclk = input_in[0];
    wire spi_mosi = input_in[1];
    wire spi_csb  = input_in[2];
    wire spi_miso;   // driven by host_spi_slave, routed to bidir_out[0]

    assign bidir_out[0] = spi_miso;

    // -----------------------------------------------------------------------
    // host_spi_slave wires
    // -----------------------------------------------------------------------
    wire        host_rst, host_rst_en;
    wire [10:0] sram_waddr;
    wire  [7:0] sram_wdata;
    wire        sram_wen;       // active-HIGH from host_spi_slave
    wire  [3:0] sram_wtile;
    wire        sram_wunicast;
    wire  [3:0] rd_tile;
    wire [10:0] rd_addr;
    wire        rd_req;
    wire  [7:0] rd_data;

    // -----------------------------------------------------------------------
    // Host SPI slave — receives commands from the external host over SPI.
    // Provides broadcast/unicast SRAM writes, readback, and reset control.
    // Uses flash_Sumi/host_spi_slave.v (11-bit addresses, CMD 0x00–0x03).
    // -----------------------------------------------------------------------
    host_spi_slave host_spi (
        .sys_clk      (clk),
        .sys_rst      (~rst_n),
        .spi_csb      (spi_csb),
        .spi_sclk     (spi_sclk),
        .spi_mosi     (spi_mosi),
        .spi_miso     (spi_miso),
        .sram_waddr   (sram_waddr),
        .sram_wdata   (sram_wdata),
        .sram_wen     (sram_wen),
        .sram_wtile   (sram_wtile),
        .sram_wunicast(sram_wunicast),
        .host_rst     (host_rst),
        .host_rst_en  (host_rst_en),
        .rd_tile      (rd_tile),
        .rd_addr      (rd_addr),
        .rd_req       (rd_req),
        .rd_data      (rd_data)
    );

    // -----------------------------------------------------------------------
    // 3×3 mesh — contains boot_controller (flash loader), 9 RISC-V tiles,
    // and the host SRAM bus mux.  Flash pins go straight to pads.
    //
    // Polarity note: host_spi_slave.sram_wen is active-HIGH;
    // mesh_3x3.host_sram_wen is active-LOW (matches boot_controller).
    // -----------------------------------------------------------------------
    mesh_3x3 u_mesh (
        .clk                (clk),
        .rst                (~rst_n),
        .inject_00_nw       (34'b0),
        .monitor_22_se      (),

        // Flash: MISO from input pad; CS_N/CLK/MOSI to bidir output pads
        .flash_miso         (input_in[4]),
        .flash_cs_n         (bidir_out[1]),
        .flash_clk          (bidir_out[2]),
        .flash_mosi         (bidir_out[3]),

        // Host SRAM write bus (active-LOW wen)
        .host_sram_addr     (sram_waddr),
        .host_sram_data     (sram_wdata),
        .host_sram_wen      (~sram_wen),
        .host_sram_wtile    (sram_wtile),
        .host_sram_wunicast (sram_wunicast),
        .host_rst           (host_rst_en & host_rst),

        // Host readback
        .rd_tile            (rd_tile),
        .rd_addr            (rd_addr),
        .rd_req             (rd_req),
        .rd_data            (rd_data)
    );

    // Chip ID - do not remove, necessary for tapeout
    (* keep *)
    gf180mcu_ws_ip__id chip_id ();

endmodule

`default_nettype wire
