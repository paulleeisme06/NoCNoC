`default_nettype none

module mesh_3x3 (
    input wire clk,
    input wire rst,

    // External flit injection/monitoring ports (36-bit flits)
    input  wire [35:0] inject_00_nw,
    output wire [35:0] monitor_22_se,

    // Flash SPI (boot_controller)
    input  wire flash_miso,
    output wire flash_cs_n,
    output wire flash_clk,
    output wire flash_mosi,

    // Host SRAM write bus (from host_spi_slave, post-boot overwrite path)
    // host_wen is active-high; internally inverted to match boot bus polarity.
    input  wire [10:0] host_waddr,
    input  wire [7:0]  host_wdata,
    input  wire        host_wen,       // active-high
    input  wire [8:0]  host_tile_mask, // bit (r*3+c) = 1 enables tile(r,c); default all ones
    input  wire        host_rst_in,   // 1 = hold tiles in reset (enables host writes)
    input  wire        host_rst_en,   // 1 = host owns reset/bus

    // DFT debug ports (Ethan)
    input  wire        dft_mode,
    input  wire [3:0]  dft_tile_id, // {row[1:0], col[1:0]}
    input  wire        dft_we,
    input  wire [10:0] dft_addr,
    input  wire [7:0]  dft_wdata,
    output wire [7:0]  dft_rdata
);

    // -------------------------------------------------------------------------
    // DFT per-tile chip-enables
    // -------------------------------------------------------------------------
    wire [7:0] tile_dft_rdata [0:2][0:2];
    wire       tile_dft_ce    [0:2][0:2];

    genvar dr, dc;
    generate
        for (dr = 0; dr < 3; dr = dr + 1) begin : dft_dec_r
            for (dc = 0; dc < 3; dc = dc + 1) begin : dft_dec_c
                assign tile_dft_ce[dr][dc] = dft_mode && (dft_tile_id == {2'(dr), 2'(dc)});
            end
        end
    endgenerate

    // -------------------------------------------------------------------------
    // Boot controller
    // -------------------------------------------------------------------------
    wire [7:0]  bc_data;
    wire [10:0] bc_addr;
    wire        bc_wen, bc_cpu_rst_n;

    boot_controller boot_inst (
        .clk(clk), .rst_n(!rst),
        .flash_cs_n(flash_cs_n), .flash_clk(flash_clk),
        .flash_mosi(flash_mosi), .flash_miso(flash_miso),
        .sram_wdata(bc_data), .sram_waddr(bc_addr),
        .sram_wen(bc_wen), .cpu_reset_n(bc_cpu_rst_n)
    );

    always @(negedge bc_wen) begin
        $display("[BOOT t=%0t] boot_wen pulse: addr=0x%03x data=0x%02x",
                 $time, bc_addr, bc_data);
    end

    // -------------------------------------------------------------------------
    // Host / boot-controller arbitration
    //
    // When host_rst_en=1 the host owns the SRAM bus:
    //   - boot_addr/data/wen are driven from host inputs
    //   - cpu_rst_n is driven by ~host_rst_in (host can hold tiles in reset
    //     to safely write, then release to start the CPUs)
    // When host_rst_en=0 the boot_controller owns everything (normal boot).
    // -------------------------------------------------------------------------
    wire [10:0] boot_addr = host_rst_en ? host_waddr   : bc_addr;
    wire [7:0]  boot_data = host_rst_en ? host_wdata   : bc_data;
    wire        cpu_rst_n = host_rst_en ? ~host_rst_in : bc_cpu_rst_n;
    wire        boot_mode = !cpu_rst_n;
    // boot_wen is computed per-tile inside the generate loop so host_tile_mask is applied

    // -------------------------------------------------------------------------
    // Mesh interconnect wires (36-bit flits)
    // -------------------------------------------------------------------------
    wire [35:0] grid_n [0:2][0:2], grid_s [0:2][0:2], grid_e [0:2][0:2], grid_w [0:2][0:2];
    wire [35:0] grid_ne[0:2][0:2], grid_nw[0:2][0:2], grid_se[0:2][0:2], grid_sw[0:2][0:2];

    genvar r, c;
    generate
        for (r = 0; r < 3; r = r + 1) begin : rows
            for (c = 0; c < 3; c = c + 1) begin : cols
                wire [35:0] n_i  = (r > 0) ? grid_s[r-1][c] : 36'b0;
                wire [35:0] s_i  = (r < 2) ? grid_n[r+1][c] : 36'b0;
                wire [35:0] e_i  = (c < 2) ? grid_w[r][c+1] : 36'b0;
                wire [35:0] w_i  = (c > 0) ? grid_e[r][c-1] : 36'b0;
                wire [35:0] ne_i = (r > 0 && c < 2) ? grid_sw[r-1][c+1] : 36'b0;
                wire [35:0] se_i = (r < 2 && c < 2) ? grid_nw[r+1][c+1] : 36'b0;
                wire [35:0] sw_i = (r < 2 && c > 0) ? grid_ne[r+1][c-1] : 36'b0;
                wire [35:0] nw_i = (r == 0 && c == 0) ? inject_00_nw :
                                   (r > 0  && c > 0)   ? grid_se[r-1][c-1] : 36'b0;

                // boot_wen is active-low; host_wen active-high, masked per tile
                wire tile_boot_wen = host_rst_en
                    ? (~host_wen | ~host_tile_mask[r*3+c])
                    : bc_wen;

                mesh_tile #(.TILE_ID({3'(r), 3'(c)})) tile_inst (
                    .clk(clk), .rst(!cpu_rst_n), .boot_mode(boot_mode),
                    .boot_addr(boot_addr), .boot_data(boot_data), .boot_wen(tile_boot_wen),
                    .north_in(n_i),   .south_in(s_i),   .east_in(e_i),   .west_in(w_i),
                    .north_out(grid_n[r][c]), .south_out(grid_s[r][c]),
                    .east_out(grid_e[r][c]),  .west_out(grid_w[r][c]),
                    .ne_in(ne_i),  .nw_in(nw_i),  .se_in(se_i),  .sw_in(sw_i),
                    .ne_out(grid_ne[r][c]), .nw_out(grid_nw[r][c]),
                    .se_out(grid_se[r][c]), .sw_out(grid_sw[r][c]),
                    // DFT ports
                    .dft_mode(dft_mode),
                    .dft_ce(tile_dft_ce[r][c]),
                    .dft_we(dft_we),
                    .dft_addr(dft_addr),
                    .dft_wdata(dft_wdata),
                    .dft_rdata(tile_dft_rdata[r][c])
                );
            end
        end
    endgenerate

    // -------------------------------------------------------------------------
    // DFT readback mux
    // -------------------------------------------------------------------------
    reg [7:0] dft_rdata_r;
    always @(*) begin
        dft_rdata_r = 8'h0;
        case (dft_tile_id)
            4'b0000: dft_rdata_r = tile_dft_rdata[0][0];
            4'b0001: dft_rdata_r = tile_dft_rdata[0][1];
            4'b0010: dft_rdata_r = tile_dft_rdata[0][2];
            4'b0100: dft_rdata_r = tile_dft_rdata[1][0];
            4'b0101: dft_rdata_r = tile_dft_rdata[1][1];
            4'b0110: dft_rdata_r = tile_dft_rdata[1][2];
            4'b1000: dft_rdata_r = tile_dft_rdata[2][0];
            4'b1001: dft_rdata_r = tile_dft_rdata[2][1];
            4'b1010: dft_rdata_r = tile_dft_rdata[2][2];
            default: dft_rdata_r = 8'h0;
        endcase
    end
    assign dft_rdata = dft_rdata_r;

    assign monitor_22_se = grid_se[2][2];

endmodule
