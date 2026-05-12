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
`ifdef SIMULATION
    ,
    // Testbench backdoor (sim only — not present in chip_top)
    input  wire  [3:0] tb_tile_sel,
    input  wire [10:0] tb_raddr,
    output wire  [7:0] tb_rdata
`endif
);

    wire [33:0] inject_00_nw = 34'h0;
    wire [33:0] monitor_22_se;

    // host_spi_slave outputs
    wire        host_rst;
    wire        host_rst_en;
    wire [10:0] host_sram_waddr;
    wire [7:0]  host_sram_wdata;
    wire        host_sram_wen;
    wire [3:0]  host_sram_wtile;
    wire        host_sram_wunicast;
    wire [3:0]  rd_tile;
    wire [10:0] rd_addr;
    wire        rd_req;
    wire [7:0]  rd_data_from_xbar;

    // -----------------------------------------------------------------------
    // mesh_3x3 — contains internal boot_controller, flash path, all 9 tiles
    // -----------------------------------------------------------------------
    mesh_3x3 mesh_inst (
        .clk            (clk),
        .rst            (rst),
        .inject_00_nw   (inject_00_nw),
        .monitor_22_se  (monitor_22_se),
        .flash_miso     (flash_miso),
        .flash_cs_n     (flash_csb),
        .flash_clk      (flash_clk),
        .flash_mosi     (flash_mosi),
        // Host SPI write bus
        // host_spi_slave.sram_wen is active-HIGH; mesh boot bus is active-LOW
        .host_sram_addr    (host_sram_waddr),
        .host_sram_data    (host_sram_wdata),
        .host_sram_wen     (~host_sram_wen),
        .host_sram_wtile   (host_sram_wtile),
        .host_sram_wunicast(host_sram_wunicast),
        .host_rst       (host_rst_en & host_rst),
        // Host readback — direct from host_spi_slave, no crossbar needed
        .rd_tile        (rd_tile),
        .rd_addr        (rd_addr),
        .rd_req         (rd_req),
        .rd_data        (rd_data_from_xbar)
`ifdef SIMULATION
        ,
        // Testbench backdoor pass-through
        .tb_tile_sel    (tb_tile_sel),
        .tb_raddr       (tb_raddr),
        .tb_rdata       (tb_rdata)
`endif
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
        .sram_waddr   (host_sram_waddr),
        .sram_wdata   (host_sram_wdata),
        .sram_wen     (host_sram_wen),
        .sram_wtile   (host_sram_wtile),
        .sram_wunicast(host_sram_wunicast),
        .host_rst    (host_rst),
        .host_rst_en (host_rst_en),
        .rd_tile     (rd_tile),
        .rd_addr     (rd_addr),
        .rd_req      (rd_req),
        .rd_data     (rd_data_from_xbar)
    );

`ifdef SIMULATION
    // -----------------------------------------------------------------------
    // SRAM check — fires when internal boot_controller asserts cpu_rst_n
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
        $display("Expected: [0]=13 [1]=01 [2]=c0 [3]=7f");
    end

    always @(posedge mesh_inst.cpu_rst_n) begin
        #200;
        $display("=== NOC BOOT SRAM CHECK ===");
        $display("tile(0,0) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[0].cols[0].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[0].cols[0].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[0].cols[0].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[0].cols[0].tile_inst.sram_inst.mem[3]);
        $display("tile(0,1) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[0].cols[1].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[0].cols[1].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[0].cols[1].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[0].cols[1].tile_inst.sram_inst.mem[3]);
        $display("tile(0,2) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[0].cols[2].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[0].cols[2].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[0].cols[2].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[0].cols[2].tile_inst.sram_inst.mem[3]);
        $display("tile(1,0) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[1].cols[0].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[1].cols[0].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[1].cols[0].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[1].cols[0].tile_inst.sram_inst.mem[3]);
        $display("tile(1,1) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[1].cols[1].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[1].cols[1].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[1].cols[1].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[1].cols[1].tile_inst.sram_inst.mem[3]);
        $display("tile(1,2) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[1].cols[2].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[1].cols[2].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[1].cols[2].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[1].cols[2].tile_inst.sram_inst.mem[3]);
        $display("tile(2,0) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[2].cols[0].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[2].cols[0].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[2].cols[0].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[2].cols[0].tile_inst.sram_inst.mem[3]);
        $display("tile(2,1) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[2].cols[1].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[2].cols[1].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[2].cols[1].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[2].cols[1].tile_inst.sram_inst.mem[3]);
        $display("tile(2,2) [0]=%02x [1]=%02x [2]=%02x [3]=%02x",
            mesh_inst.rows[2].cols[2].tile_inst.sram_inst.mem[0],
            mesh_inst.rows[2].cols[2].tile_inst.sram_inst.mem[1],
            mesh_inst.rows[2].cols[2].tile_inst.sram_inst.mem[2],
            mesh_inst.rows[2].cols[2].tile_inst.sram_inst.mem[3]);
        $display("Expected all tiles: [0]=13 [1]=01 [2]=c0 [3]=7f");
    end
`endif

endmodule
