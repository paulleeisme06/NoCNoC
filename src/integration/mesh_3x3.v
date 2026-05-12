`default_nettype none

module mesh_3x3 (
    input wire clk,
    input wire rst,
    input  wire [33:0] inject_00_nw,
    output wire [33:0] monitor_22_se,

    // Flash SPI — boot_controller reads firmware and loads all 9 tile SRAMs
    input  wire flash_miso,
    output wire flash_cs_n,
    output wire flash_clk,
    output wire flash_mosi,

    // Host SRAM write bus (active after boot while host_rst=1, active-LOW wen)
    input  wire [10:0] host_sram_addr,
    input  wire  [7:0] host_sram_data,
    input  wire        host_sram_wen,       // active-LOW
    input  wire  [3:0] host_sram_wtile,     // unicast target {row[1:0],col[1:0]}
    input  wire        host_sram_wunicast,  // 1=unicast one tile, 0=broadcast all

    // Host reset — 1 = hold all CPUs in reset, keeps boot bus path open
    input  wire        host_rst,

    // Host SRAM readback
    input  wire  [3:0] rd_tile,
    input  wire [10:0] rd_addr,
    input  wire        rd_req,
    output wire  [7:0] rd_data

`ifdef SIMULATION
    ,
    // Testbench backdoor: select tile via {row[1:0],col[1:0]}, read one SRAM byte
    input  wire  [3:0] tb_tile_sel,
    input  wire [10:0] tb_raddr,
    output reg   [7:0] tb_rdata
`endif
);

    // -------------------------------------------------------------------------
    // Boot controller — reads flash, drives shared write bus to all 9 tiles
    // -------------------------------------------------------------------------
    wire [7:0]  boot_data;
    wire [10:0] boot_addr;
    wire        boot_wen, cpu_rst_n;

    // boot_mode: SRAM bus stays in "boot" mode while either flash is loading
    // OR host is holding CPUs in reset (so host can write SRAM safely)
    wire boot_mode = !cpu_rst_n | host_rst;
    wire tile_rst  = !cpu_rst_n | host_rst;

    boot_controller boot_inst (
        .clk        (clk),
        .rst_n      (!rst),
        .flash_cs_n (flash_cs_n),
        .flash_clk  (flash_clk),
        .flash_mosi (flash_mosi),
        .flash_miso (flash_miso),
        .sram_wdata (boot_data),
        .sram_waddr (boot_addr),
        .sram_wen   (boot_wen),
        .cpu_reset_n(cpu_rst_n)
    );

    // -------------------------------------------------------------------------
    // Shared write bus mux: flash boot owns bus until cpu_rst_n,
    // then host SPI takes over via the same boot_addr/boot_data/boot_wen path
    // -------------------------------------------------------------------------
    wire [10:0] mux_boot_addr = cpu_rst_n ? host_sram_addr : boot_addr;
    wire  [7:0] mux_boot_data = cpu_rst_n ? host_sram_data : boot_data;
    wire        mux_boot_wen  = cpu_rst_n ? host_sram_wen  : boot_wen;

    // -------------------------------------------------------------------------
    // Mesh routing wires
    // -------------------------------------------------------------------------
    wire [35:0] grid_n [0:2][0:2], grid_s [0:2][0:2];
    wire [35:0] grid_e [0:2][0:2], grid_w [0:2][0:2];
    wire [35:0] grid_ne[0:2][0:2], grid_nw[0:2][0:2];
    wire [35:0] grid_se[0:2][0:2], grid_sw[0:2][0:2];

    wire [7:0] tile_rd_data[0:2][0:2];

`ifdef SIMULATION
    wire [7:0] tb_tile_rdata[0:2][0:2];
`endif

    // -------------------------------------------------------------------------
    // Tile generate loop
    // -------------------------------------------------------------------------
    genvar r, c;
    generate
        for (r = 0; r < 3; r = r + 1) begin : rows
            for (c = 0; c < 3; c = c + 1) begin : cols
                wire [35:0] n_i  = (r > 0)           ? grid_s [r-1][c]          : 36'b0;
                wire [35:0] s_i  = (r < 2)           ? grid_n [r+1][c]          : 36'b0;
                wire [35:0] e_i  = (c < 2)           ? grid_w [r][c+1]          : 36'b0;
                wire [35:0] w_i  = (c > 0)           ? grid_e [r][c-1]          : 36'b0;
                wire [35:0] ne_i = (r>0 && c<2)      ? grid_sw[r-1][c+1]        : 36'b0;
                wire [35:0] se_i = (r<2 && c<2)      ? grid_nw[r+1][c+1]        : 36'b0;
                wire [35:0] sw_i = (r<2 && c>0)      ? grid_ne[r+1][c-1]        : 36'b0;
                wire [35:0] nw_i = (r==0 && c==0)    ? {2'b0, inject_00_nw}     :
                                   (r>0  && c>0)     ? grid_se[r-1][c-1]        : 36'b0;

                // Unicast WEN gating: when host is doing a unicast write,
                // only the targeted tile receives the write pulse.
                // During flash boot (cpu_rst_n=0) all tiles always get boot_wen.
                wire tile_wen_w = (cpu_rst_n & host_sram_wunicast &
                                   (host_sram_wtile != {2'(r), 2'(c)}))
                                  ? 1'b1 : mux_boot_wen;

                mesh_tile #(.TILE_ID({2'(r), 2'(c)})) tile_inst (
                    .clk       (clk),
                    .rst       (tile_rst),
                    .boot_mode (boot_mode),
                    .boot_addr (mux_boot_addr),
                    .boot_data (mux_boot_data),
                    .boot_wen  (tile_wen_w),

                    .north_in (n_i),  .south_in (s_i),
                    .east_in  (e_i),  .west_in  (w_i),
                    .north_out(grid_n[r][c]), .south_out(grid_s[r][c]),
                    .east_out (grid_e[r][c]), .west_out (grid_w[r][c]),

                    .ne_in(ne_i), .nw_in(nw_i), .se_in(se_i), .sw_in(sw_i),
                    .ne_out(grid_ne[r][c]), .nw_out(grid_nw[r][c]),
                    .se_out(grid_se[r][c]), .sw_out(grid_sw[r][c]),

                    .host_rd_addr(rd_addr),
                    .host_rd_req (rd_req && (rd_tile == {2'(r), 2'(c)})),
                    .host_rd_data(tile_rd_data[r][c])
`ifdef SIMULATION
                    , .tb_raddr(tb_raddr),
                    .tb_rdata(tb_tile_rdata[r][c])
`endif
                );
            end
        end
    endgenerate

    assign monitor_22_se = grid_se[2][2][33:0];

    // -------------------------------------------------------------------------
    // Host readback mux: route selected tile's SRAM byte to rd_data
    // -------------------------------------------------------------------------
    reg [7:0] rd_data_r;
    always @(*) begin
        case (rd_tile)
            4'd0:  rd_data_r = tile_rd_data[0][0];
            4'd1:  rd_data_r = tile_rd_data[0][1];
            4'd2:  rd_data_r = tile_rd_data[0][2];
            4'd4:  rd_data_r = tile_rd_data[1][0];
            4'd5:  rd_data_r = tile_rd_data[1][1];
            4'd6:  rd_data_r = tile_rd_data[1][2];
            4'd8:  rd_data_r = tile_rd_data[2][0];
            4'd9:  rd_data_r = tile_rd_data[2][1];
            4'd10: rd_data_r = tile_rd_data[2][2];
            default: rd_data_r = 8'h00;
        endcase
    end
    assign rd_data = rd_data_r;

`ifdef SIMULATION
    // -------------------------------------------------------------------------
    // Testbench backdoor mux: tb_tile_sel → one tile's SRAM byte
    // -------------------------------------------------------------------------
    always @(*) begin
        case (tb_tile_sel)
            4'd0:  tb_rdata = tb_tile_rdata[0][0];
            4'd1:  tb_rdata = tb_tile_rdata[0][1];
            4'd2:  tb_rdata = tb_tile_rdata[0][2];
            4'd4:  tb_rdata = tb_tile_rdata[1][0];
            4'd5:  tb_rdata = tb_tile_rdata[1][1];
            4'd6:  tb_rdata = tb_tile_rdata[1][2];
            4'd8:  tb_rdata = tb_tile_rdata[2][0];
            4'd9:  tb_rdata = tb_tile_rdata[2][1];
            4'd10: tb_rdata = tb_tile_rdata[2][2];
            default: tb_rdata = 8'h00;
        endcase
    end
`endif

endmodule
