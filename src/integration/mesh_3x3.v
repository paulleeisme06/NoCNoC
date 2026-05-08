`default_nettype none

module mesh_3x3 (
    input wire clk,
    input wire rst,
    input  wire [33:0] inject_00_nw,
    output wire [33:0] monitor_22_se,
    input  wire flash_miso,
    output wire flash_cs_n,
    output wire flash_clk,
    output wire flash_mosi,
    // Host SRAM write bus (active after boot while host_rst=1, active-low wen)
    input  wire [10:0] host_sram_addr,
    input  wire  [7:0] host_sram_data,
    input  wire        host_sram_wen,
    input  wire  [3:0] host_sram_wtile,    // unicast target tile {row[1:0],col[1:0]}
    input  wire        host_sram_wunicast, // 1=unicast, 0=broadcast
    // Host reset (1 = hold tiles in reset; also activates boot bus path for host writes)
    input  wire        host_rst,
    // Testbench backdoor: select tile via TILE_ID encoding (row<<2|col), read one SRAM byte
    input  wire  [3:0] tb_tile_sel,
    input  wire [10:0] tb_raddr,
    output reg   [7:0] tb_rdata
);
    wire [7:0] boot_data;
    wire [10:0] boot_addr;
    wire       boot_wen, cpu_rst_n;

    // boot_mode stays high while host holds tiles in reset so the boot bus
    // path in mesh_tile remains active for host SRAM writes after boot.
    wire boot_mode = !cpu_rst_n | host_rst;
    wire tile_rst  = !cpu_rst_n | host_rst;

    boot_controller boot_inst (
        .clk(clk), .rst_n(!rst),
        .flash_cs_n(flash_cs_n), .flash_clk(flash_clk), .flash_mosi(flash_mosi), .flash_miso(flash_miso),
        .sram_wdata(boot_data), .sram_waddr(boot_addr), .sram_wen(boot_wen), .cpu_reset_n(cpu_rst_n)
    );

    // After boot (cpu_rst_n=1) host can write via the same boot bus path
    wire [10:0] mux_boot_addr = cpu_rst_n ? host_sram_addr : boot_addr;
    wire  [7:0] mux_boot_data = cpu_rst_n ? host_sram_data : boot_data;
    wire        mux_boot_wen  = cpu_rst_n ? host_sram_wen  : boot_wen;

    wire [33:0] grid_n [0:2][0:2], grid_s [0:2][0:2], grid_e [0:2][0:2], grid_w [0:2][0:2];
    wire [33:0] grid_ne[0:2][0:2], grid_nw[0:2][0:2], grid_se[0:2][0:2], grid_sw[0:2][0:2];
    wire  [7:0] tb_tile_rdata[0:2][0:2];

    genvar r, c;
    generate
        for (r = 0; r < 3; r = r + 1) begin : rows
            for (c = 0; c < 3; c = c + 1) begin : cols
                wire [33:0] n_i  = (r > 0) ? grid_s[r-1][c] : 34'b0;
                wire [33:0] s_i  = (r < 2) ? grid_n[r+1][c] : 34'b0;
                wire [33:0] e_i  = (c < 2) ? grid_w[r][c+1] : 34'b0;
                wire [33:0] w_i  = (c > 0) ? grid_e[r][c-1] : 34'b0;
                wire [33:0] ne_i = (r > 0 && c < 2) ? grid_sw[r-1][c+1] : 34'b0;
                wire [33:0] se_i = (r < 2 && c < 2) ? grid_nw[r+1][c+1] : 34'b0;
                wire [33:0] sw_i = (r < 2 && c > 0) ? grid_ne[r+1][c-1] : 34'b0;
                wire [33:0] nw_i = (r == 0 && c == 0) ? inject_00_nw :
                                  (r > 0 && c > 0)   ? grid_se[r-1][c-1] : 34'b0;

                // Gate boot_wen per-tile: unicast only enables the target tile
                wire tile_wen_w = (cpu_rst_n & host_sram_wunicast &
                                   (host_sram_wtile != {r[1:0], c[1:0]}))
                                  ? 1'b1 : mux_boot_wen;

                mesh_tile #(.TILE_ID({2'(r), 2'(c)})) tile_inst (
                    .clk(clk), .rst(tile_rst), .boot_mode(boot_mode),
                    .boot_addr(mux_boot_addr), .boot_data(mux_boot_data), .boot_wen(tile_wen_w),
                    .north_in(n_i), .south_in(s_i), .east_in(e_i), .west_in(w_i),
                    .north_out(grid_n[r][c]), .south_out(grid_s[r][c]), .east_out(grid_e[r][c]), .west_out(grid_w[r][c]),
                    .ne_in(ne_i), .nw_in(nw_i), .se_in(se_i), .sw_in(sw_i),
                    .ne_out(grid_ne[r][c]), .nw_out(grid_nw[r][c]), .se_out(grid_se[r][c]), .sw_out(grid_sw[r][c]),
                    .tb_raddr(tb_raddr), .tb_rdata(tb_tile_rdata[r][c])
                );
            end
        end
    endgenerate

    assign monitor_22_se = grid_se[2][2];

    // 9-to-1 mux: select tile by TILE_ID encoding (row<<2|col)
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
endmodule
