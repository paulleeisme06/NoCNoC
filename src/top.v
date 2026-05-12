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

    wire [35:0] monitor_22_se;

    // host_to_chip outputs
    wire        host_rst;
    wire        host_rst_en;
    wire [35:0] host_flit;
    wire [3:0]  rd_tile;
    wire [10:0] rd_addr;
    wire        rd_req;
    wire [7:0]  rd_data_from_xbar;

    // -----------------------------------------------------------------------
    // mesh_3x3 — boot controller + 9 tiles + host write bus
    // -----------------------------------------------------------------------
    mesh_3x3 mesh_inst (
        .clk           (clk),
        .rst           (rst),
        .inject_00_nw  (host_flit),
        .monitor_22_se (monitor_22_se),
        .flash_miso    (flash_miso),
        .flash_cs_n    (flash_csb),
        .flash_clk     (flash_clk),
        .flash_mosi    (flash_mosi),
        // Host reset control (write flits enter via inject_00_nw)
        .host_rst_in     (host_rst),
        .host_rst_en     (host_rst_en)
    );

    // -----------------------------------------------------------------------
    // host_to_chip — SPI gateway for host SRAM writes and CPU reset control
    // -----------------------------------------------------------------------
    host_to_chip host_spi (
        .sys_clk     (clk),
        .sys_rst     (rst),
        .spi_csb     (host_csb),
        .spi_sclk    (host_sclk),
        .spi_mosi    (host_mosi),
        .spi_miso    (host_miso),
        .host_flit   (host_flit),
        .host_rst    (host_rst),
        .host_rst_en (host_rst_en),
        .rd_tile     (rd_tile),
        .rd_addr     (rd_addr),
        .rd_req      (rd_req),
        .rd_data     (rd_data_from_xbar)
    );

    // rd_data_from_xbar is undriven (readback path not yet wired);
    // host read commands return 0xFF until rd_crossbar is integrated.
    assign rd_data_from_xbar = 8'hFF;

    // -----------------------------------------------------------------------
    // SRAM snapshot — fires when boot_controller asserts cpu_rst_n
    // -----------------------------------------------------------------------
    always @(posedge mesh_inst.cpu_rst_n) begin
        #100;
        $display("=== SRAM_CHECK at time %0t ===", $time);
        $display("tile(0,0) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[0].cols[0].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[0].cols[0].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[0].cols[0].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[0].cols[0].tile_inst.sram_inst.mem[3]);
        $display("tile(2,2) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[2].cols[2].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[2].cols[2].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[2].cols[2].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[2].cols[2].tile_inst.sram_inst.mem[3]);
        $display("Expected: [0]=13 [1]=01 [2]=00 [3]=40");
    end

endmodule
