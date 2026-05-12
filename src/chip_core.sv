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
    assign bidir_cs = '0;
    assign bidir_sl = '0;
    assign bidir_pu = '0;
    assign bidir_pd = '0;

    // -------------------------------------------------------------------------
    // Pad assignments (1x1 slot: 12 input pads, 40 bidir pads)
    //
    // Input pads:
    //   input_in[0]  = flash_miso    (boot flash MISO)
    //   input_in[1]  = host_csb      (host SPI chip select, active low)
    //   input_in[2]  = host_sclk     (host SPI clock)
    //   input_in[3]  = host_mosi     (host SPI MOSI)
    //   input_in[4]  = spi_clk       (DFT SPI clock)
    //   input_in[5]  = spi_mosi      (DFT SPI MOSI)
    //   input_in[6]  = spi_cs_n      (DFT SPI chip select)
    //   input_in[7]  = dft_mode      (DFT mode enable)
    //
    // Bidir pads (outputs):
    //   bidir[0]     = flash_cs_n    (boot flash CS)
    //   bidir[1]     = flash_clk     (boot flash CLK)
    //   bidir[2]     = flash_mosi    (boot flash MOSI)
    //   bidir[3]     = host_miso     (host SPI MISO back to host)
    //   bidir[4]     = dft_miso      (DFT SPI MISO)
    //   bidir[5:38]  = monitor_out   (34-bit mesh monitor, prevent Yosys opt)
    // -------------------------------------------------------------------------

    // Flash SPI
    wire flash_miso  = input_in[0];
    wire flash_cs_n, flash_clk, flash_mosi_out;

    // Host SPI (Paul & Andrea's gateway)
    wire host_csb   = input_in[1];
    wire host_sclk  = input_in[2];
    wire host_mosi  = input_in[3];
    wire host_miso_out;

    // DFT SPI (Ethan's debug interface)
    wire spi_clk  = input_in[4];
    wire spi_mosi = input_in[5];
    wire spi_cs_n = input_in[6];
    wire dft_mode = input_in[7];
    wire spi_miso;

    // DFT debug signals
    wire        dbg_req, dbg_we;
    wire [3:0]  dbg_tile_id;
    wire [10:0] dbg_addr;
    wire [7:0]  dbg_wdata;
    wire [7:0]  dbg_rdata;
    wire        dbg_ack;

    // Mesh monitor output — prevents Yosys from optimizing away mesh logic
    wire [35:0] monitor_out;

    // Bidir pad directions: flash(3) + host_miso(1) + dft_miso(1) + monitor(34) = 39
    assign bidir_oe  = {{(NUM_BIDIR_PADS-40){1'b0}}, {36{1'b1}}, 1'b1, 3'b111};
    assign bidir_ie  = ~bidir_oe;
    assign bidir_out = {{(NUM_BIDIR_PADS-40){1'b0}}, monitor_out,
                        host_miso_out,
                        flash_mosi_out, flash_clk, flash_cs_n};

    logic _unused;
    assign _unused = &{bidir_in, analog};

    // -------------------------------------------------------------------------
    // mesh_rxc — 5x5 grid of SERV RISC-V cores (contains the boot_controller)
    // Its flash SPI signals are routed through spi_arbiter (below) so the
    // housekeeping subsystem can be muxed onto the same flash bus.
    // -------------------------------------------------------------------------
    wire mesh_flash_cs_n, mesh_flash_clk, mesh_flash_mosi;
    wire boot_flash_miso;  // MISO going back to mesh's boot_controller
    mesh_rxc u_mesh (
        .clk          (clk),
        .rst          (~rst_n),
        .inject_00_nw (36'b0),
        .monitor_se(monitor_out),
        .flash_miso   (boot_flash_miso),
        .flash_cs_n   (mesh_flash_cs_n),
        .flash_clk    (mesh_flash_clk),
        .flash_mosi   (mesh_flash_mosi)
    );

    // -------------------------------------------------------------------------
    // Flash housekeeping subsystem (flash_Sumi/)
    //
    //   shiftregister  -> housekeeping_fsm -> hk_boot_adapter (Wishbone path)
    //                          |
    //   flash_clk      -> SPI clock for HK side of spi_arbiter
    //
    // spi_arbiter muxes mesh_rxc.boot_controller's flash SPI with the
    // housekeeping SPI to the physical flash pads. cpu_rst_n is tied 1'b0 so
    // boot_controller always wins the bus (mesh_rxc doesn't expose its
    // internal cpu_reset_n today); the HK chain is still synthesized.
    // -------------------------------------------------------------------------
    wire        hk_csb;
    wire        hk_clk_spi;
    wire        hk_mosi = 1'b0;       // HK is read-only; no MOSI payload
    wire        hk_flash_miso;
    wire        hk_fetch_o;
    wire        hk_word_ready;
    wire [31:0] hk_shifted_word;
    wire        hk_fetch_en;
    wire [31:0] hk_wbs_adr;
    wire [31:0] hk_wbs_dat;
    wire        hk_wbs_cyc;
    wire        hk_wbs_stb;
    wire        hk_wbs_we;
    wire        hk_wbs_ack;
    wire        hk_done_loading;
    wire [9:0]  hk_boot_addr;
    wire [7:0]  hk_boot_data;
    wire        hk_boot_wen;

    spi_arbiter u_spi_arb (
        .boot_csb   (mesh_flash_cs_n),
        .boot_clk   (mesh_flash_clk),
        .boot_mosi  (mesh_flash_mosi),
        .boot_miso  (boot_flash_miso),
        .hk_csb     (hk_csb),
        .hk_clk     (hk_clk_spi),
        .hk_mosi    (hk_mosi),
        .hk_miso    (hk_flash_miso),
        .flash_csb  (flash_cs_n),
        .flash_clk  (flash_clk),
        .flash_mosi (flash_mosi_out),
        .flash_miso (flash_miso),
        .cpu_rst_n  (1'b0)
    );

    flash_clk u_flash_clk (
        .clk      (clk),
        .reset    (~rst_n),
        .enable   (~hk_csb),
        .flash_clk(hk_clk_spi)
    );

    shiftregister u_shiftreg (
        .clk         (clk),
        .reset       (~rst_n),
        .serial_in   (hk_flash_miso),
        .shift_en    (~hk_csb),
        .fetch_o     (hk_fetch_o),
        .shifted_word(hk_shifted_word),
        .done_word   (hk_word_ready)
    );

    housekeeping_fsm u_hk_fsm (
        .clk         (clk),
        .reset       (~rst_n),
        .bypass_en   (1'b0),
        .word_ready  (hk_word_ready),
        .shifted_word(hk_shifted_word),
        .fetch_en    (hk_fetch_en),
        .fetch_o     (hk_fetch_o),
        .flash_csb   (hk_csb),
        .wbs_adr     (hk_wbs_adr),
        .wbs_dat     (hk_wbs_dat),
        .wbs_cyc     (hk_wbs_cyc),
        .wbs_stb     (hk_wbs_stb),
        .wbs_we      (hk_wbs_we),
        .wbs_ack     (hk_wbs_ack),
        .done_loading(hk_done_loading)
    );

    hk_boot_adapter u_hk_boot_adapter (
        .clk      (clk),
        .rst      (~rst_n),
        .wbs_adr  (hk_wbs_adr),
        .wbs_dat  (hk_wbs_dat),
        .wbs_cyc  (hk_wbs_cyc),
        .wbs_stb  (hk_wbs_stb),
        .wbs_we   (hk_wbs_we),
        .wbs_ack  (hk_wbs_ack),
        .boot_addr(hk_boot_addr),
        .boot_data(hk_boot_data),
        .boot_wen (hk_boot_wen)
    );

    // -------------------------------------------------------------------------
    // rd_crossbar — readback crossbar (designed for a 3x3 mesh, 9 tiles).
    // The current mesh is 5x5 and exposes no per-tile readback ports, so the
    // 9 tile data inputs are tied to 0 and the crossbar's tile addr/req
    // outputs dangle. Its rd_data drives dbg_rdata back into spi_debug.
    // -------------------------------------------------------------------------
    rd_crossbar u_rd_crossbar (
        .clk            (clk),
        .rd_tile        (dbg_tile_id),
        .rd_addr        (dbg_addr[9:0]),
        .rd_req         (dbg_req),
        .rd_data        (dbg_rdata),
        .tile_rd_addr_0 (), .tile_rd_addr_1 (), .tile_rd_addr_2 (),
        .tile_rd_addr_3 (), .tile_rd_addr_4 (), .tile_rd_addr_5 (),
        .tile_rd_addr_6 (), .tile_rd_addr_7 (), .tile_rd_addr_8 (),
        .tile_rd_req_0  (), .tile_rd_req_1  (), .tile_rd_req_2  (),
        .tile_rd_req_3  (), .tile_rd_req_4  (), .tile_rd_req_5  (),
        .tile_rd_req_6  (), .tile_rd_req_7  (), .tile_rd_req_8  (),
        .tile_rd_data_0 (8'b0), .tile_rd_data_1 (8'b0), .tile_rd_data_2 (8'b0),
        .tile_rd_data_3 (8'b0), .tile_rd_data_4 (8'b0), .tile_rd_data_5 (8'b0),
        .tile_rd_data_6 (8'b0), .tile_rd_data_7 (8'b0), .tile_rd_data_8 (8'b0)
    );

    // -------------------------------------------------------------------------
    // host_spi_slave — Host to chip SPI gateway (Paul & Andrea)
    // -------------------------------------------------------------------------
    host_spi_slave gateway_inst (
        .sys_clk    (clk),
        .sys_rst    (~rst_n),
        .spi_csb    (host_csb),
        .spi_sclk   (host_sclk),
        .spi_mosi   (host_mosi),
        .spi_miso   (host_miso_out),
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

    // -------------------------------------------------------------------------
    // spi_debug — DFT interface (Ethan)
    // -------------------------------------------------------------------------
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

endmodule

`default_nettype wire