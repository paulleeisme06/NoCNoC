`default_nettype none

module top (
    input  wire clk,
    input  wire rst,

    output wire flash_csb,
    output wire flash_clk,
    output wire flash_mosi,
    input  wire flash_miso,

    input  wire host_csb,
    input  wire host_sclk,
    input  wire host_mosi,
    output wire host_miso
);

    wire [33:0] inject_00_nw = 34'h0;
    wire [33:0] monitor_22_se;

    // host_spi_slave outputs — unused in flash test but needed for compilation
    wire        host_rst;
    wire        host_rst_en;
    wire [9:0]  host_sram_waddr;
    wire [7:0]  host_sram_wdata;
    wire        host_sram_wen;
    wire [3:0]  rd_tile;
    wire [9:0]  rd_addr;
    wire        rd_req;
    wire [7:0]  rd_data_from_xbar;

    // -----------------------------------------------------------------------
    // mesh_3x3 — contains internal boot_controller, flash path, all 9 tiles
    // -----------------------------------------------------------------------
    mesh_3x3 mesh_inst (
        .clk           (clk),
        .rst           (rst),
        .inject_00_nw  (inject_00_nw),
        .monitor_22_se (monitor_22_se),
        .flash_miso    (flash_miso),
        .flash_cs_n    (flash_csb),
        .flash_clk     (flash_clk),
        .flash_mosi    (flash_mosi)
    );

    // -----------------------------------------------------------------------
    // host_spi_slave — needed for compilation, inactive during flash test
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

endmodule

