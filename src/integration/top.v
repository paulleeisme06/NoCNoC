`default_nettype none

// ============================================================================
// top.v — Full chip top level
// ============================================================================
//
// Boot + runtime sequence:
//
//   PHASE 1 — BOOT  (cpu_rst_n=0 inside mesh_3x3)
//     boot_controller (inside mesh_3x3) reads firmware.bin from SPI flash.
//     All 9 tile SRAMs loaded. cpu_rst_n goes high when done.
//
//   PHASE 2 — HOST SEED  (host sends CMD 0x03/0xFF → host_rst=1)
//     host_rst=1 holds tiles in reset AND keeps boot_mode=1 inside mesh_3x3,
//     so the boot bus path in mesh_tile remains active for host SRAM writes.
//     Host sends CMD 0x00 writes to seed current_grid in all tile SRAMs.
//
//   PHASE 3 — RUN  (host sends CMD 0x03/0x00 → host_rst=0)
//     Tiles released. All 9 SERV cores execute firmware (Conway's Game of Life).
//     Firmware signals 0xCCCCCCCC on monitor_22_se when generation stable.
//
//   PHASE 4 — READBACK
//     Cocotb reads tile SRAMs via hierarchy probing (sram_inst.mem[addr]).
//
// SRAM write bus arbitration (inside mesh_3x3, active-low boot_wen):
//   cpu_rst_n=0  →  boot_controller owns bus  (firmware load)
//   cpu_rst_n=1  →  host_spi_slave owns bus   (host seed while host_rst=1)
//
// ============================================================================

module top (
    input  wire clk,
    input  wire rst,

    // SPI Flash — boot_controller (inside mesh_3x3) talks directly
    output wire flash_csb,
    output wire flash_clk,
    output wire flash_mosi,
    input  wire flash_miso,

    // Host SPI — seed SRAM, control reset
    input  wire host_csb,
    input  wire host_sclk,
    input  wire host_mosi,
    output wire host_miso
);

    wire [33:0] inject_00_nw = 34'h0;
    wire [33:0] monitor_22_se;

    // host_spi_slave outputs
    wire [9:0]  host_sram_waddr;
    wire [7:0]  host_sram_wdata;
    wire        host_sram_wen;   // active-high, one sys_clk pulse
    wire        host_rst;
    wire        host_rst_en;

    // Readback interface (testbench uses hierarchy probe — tie rd_data to 0)
    wire [3:0]  rd_tile;
    wire [9:0]  rd_addr;
    wire        rd_req;
    wire [7:0]  rd_data_from_xbar = 8'h00;

    // -----------------------------------------------------------------------
    // mesh_3x3 — 3×3 SERV tiles, boot_controller and flash path inside
    // -----------------------------------------------------------------------
    mesh_3x3 mesh_inst (
        .clk           (clk),
        .rst           (rst),
        .inject_00_nw  (inject_00_nw),
        .monitor_22_se (monitor_22_se),
        .flash_miso    (flash_miso),
        .flash_cs_n    (flash_csb),
        .flash_clk     (flash_clk),
        .flash_mosi    (flash_mosi),
        // Host SRAM write bus: zero-extend addr 10→11 bits, invert wen polarity
        .host_sram_addr({1'b0, host_sram_waddr}),
        .host_sram_data(host_sram_wdata),
        .host_sram_wen (~host_sram_wen),
        // Gate host_rst through host_rst_en so idle host_rst=0 doesn't affect tiles
        .host_rst      (host_rst_en ? host_rst : 1'b0)
    );

    // -----------------------------------------------------------------------
    // host_spi_slave — seeds SRAM, controls reset
    // -----------------------------------------------------------------------
    host_spi_slave host_spi (
        .sys_clk     (clk),
        .sys_rst     (rst),
        .spi_csb     (host_csb),
        .spi_sclk    (host_sclk),
        .spi_mosi    (host_mosi),
        .spi_miso    (host_miso),
        .sram_waddr  (host_sram_waddr),
        .sram_wdata  (host_sram_wdata),
        .sram_wen    (host_sram_wen),
        .host_rst    (host_rst),
        .host_rst_en (host_rst_en),
        .rd_tile     (rd_tile),
        .rd_addr     (rd_addr),
        .rd_req      (rd_req),
        .rd_data     (rd_data_from_xbar)
    );

    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, top);
    end

endmodule
