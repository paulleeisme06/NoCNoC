`default_nettype none

// ============================================================================
// mesh_nxn — parameterized MESH_R × MESH_C mesh of RISC-V tiles
//
// Dimensions come from mesh_config.vh — do NOT pass N here.
// To resize the mesh, edit mesh_config.vh only.
//
// TILE_ID encoding: row in [5:3], col in [2:0]  (3 bits each, max 8×8)
//
// Ports:
//   inject_00_nw  : external flit injection into tile(0,0) via its NW port
//   monitor_se    : south-east output of tile(MESH_R-1, MESH_C-1)
//   flash_*       : SPI flash shared by the boot controller
// ============================================================================
module mesh_rxc #(
    parameter integer MESH_R = 5,
    parameter integer MESH_C = 5
)(
    input  wire        clk,
    input  wire        rst,
    input  wire [35:0] inject_00_nw,
    output wire [35:0] monitor_se,
    input  wire        flash_miso,
    output wire        flash_cs_n,
    output wire        flash_clk,
    output wire        flash_mosi,

    input  wire        dft_mode,
    input  wire [3:0]  dft_tile_id,
    input  wire        dft_we,
    input  wire [10:0] dft_addr,
    input  wire [7:0]  dft_wdata,
    output wire [7:0]  dft_rdata
);


    // -------------------------------------------------------------------------
    // Boot controller — shared across all tiles (broadcast bus)
    // -------------------------------------------------------------------------
    wire [7:0]  boot_data;
    wire [10:0] boot_addr;
    wire        boot_wen, cpu_rst_n;
    wire        boot_mode = !cpu_rst_n;

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
    // Inter-tile link arrays  [row][col]
    // -------------------------------------------------------------------------
    wire [35:0] grid_n [0:MESH_R-1][0:MESH_C-1];
    wire [35:0] grid_s [0:MESH_R-1][0:MESH_C-1];
    wire [35:0] grid_e [0:MESH_R-1][0:MESH_C-1];
    wire [35:0] grid_w [0:MESH_R-1][0:MESH_C-1];
    wire [35:0] grid_ne[0:MESH_R-1][0:MESH_C-1];
    wire [35:0] grid_nw[0:MESH_R-1][0:MESH_C-1];
    wire [35:0] grid_se[0:MESH_R-1][0:MESH_C-1];
    wire [35:0] grid_sw[0:MESH_R-1][0:MESH_C-1];

    // -------------------------------------------------------------------------
    // DFT per-tile chip-enable and read-data collection
    // -------------------------------------------------------------------------
    wire [7:0] tile_dft_rdata [0:MESH_R-1][0:MESH_C-1];
    wire       tile_dft_ce    [0:MESH_R-1][0:MESH_C-1];

    genvar dr, dc;
    generate
        for (dr = 0; dr < MESH_R; dr = dr + 1) begin : dft_dec_r
            for (dc = 0; dc < MESH_C; dc = dc + 1) begin : dft_dec_c
                assign tile_dft_ce[dr][dc] = dft_mode &&
                    (dft_tile_id == {2'(dr), 2'(dc)});
            end
        end
    endgenerate

    reg [7:0] dft_rdata_r;
    always @(*) begin
        dft_rdata_r = 8'h0;
        case (dft_tile_id)
            4'b0000: dft_rdata_r = tile_dft_rdata[0][0];
            4'b0001: dft_rdata_r = tile_dft_rdata[0][1];
            4'b0010: dft_rdata_r = tile_dft_rdata[0][2];
            4'b0011: dft_rdata_r = tile_dft_rdata[0][3];
            4'b0100: dft_rdata_r = tile_dft_rdata[1][0];
            4'b0101: dft_rdata_r = tile_dft_rdata[1][1];
            4'b0110: dft_rdata_r = tile_dft_rdata[1][2];
            4'b0111: dft_rdata_r = tile_dft_rdata[1][3];
            4'b1000: dft_rdata_r = tile_dft_rdata[2][0];
            4'b1001: dft_rdata_r = tile_dft_rdata[2][1];
            4'b1010: dft_rdata_r = tile_dft_rdata[2][2];
            4'b1011: dft_rdata_r = tile_dft_rdata[2][3];
            default: dft_rdata_r = 8'h0;
        endcase
    end
    assign dft_rdata = dft_rdata_r;

    // -------------------------------------------------------------------------
    // Tile generate loop
    // -------------------------------------------------------------------------
    genvar r, c;
    generate
        for (r = 0; r < MESH_R; r = r + 1) begin : rows
            for (c = 0; c < MESH_C; c = c + 1) begin : cols

                // TILE_ID encoding: row in [5:3], col in [2:0]
                localparam [5:0] THIS_TILE_ID = {r[2:0], c[2:0]};

                // ----------------------------------------------------------
                // Neighbour inputs — XY convention:
                //   north_in  = south_out of the tile ABOVE  (r-1)
                //   south_in  = north_out of the tile BELOW  (r+1)
                //   east_in   = west_out  of the tile RIGHT  (c+1)
                //   west_in   = east_out  of the tile LEFT   (c-1)
                // Border tiles receive 36'b0 on missing neighbours.
                // ----------------------------------------------------------
                wire [35:0] n_i  = (r > 0)                          ? grid_s [r-1][c  ] : 36'b0;
                wire [35:0] s_i  = (r < MESH_R-1)                ? grid_n [r+1][c  ] : 36'b0;
                wire [35:0] e_i  = (c < MESH_C-1)                ? grid_w [r  ][c+1] : 36'b0;
                wire [35:0] w_i  = (c > 0)                          ? grid_e [r  ][c-1] : 36'b0;
                wire [35:0] ne_i = (r > 0 && c < MESH_C-1)       ? grid_sw[r-1][c+1] : 36'b0;
                wire [35:0] se_i = (r < MESH_R-1 && c < MESH_C-1) ? grid_nw[r+1][c+1] : 36'b0;
                wire [35:0] sw_i = (r < MESH_R-1 && c > 0)       ? grid_ne[r+1][c-1] : 36'b0;
                // NW of (0,0) is the external injection port
                wire [35:0] nw_i = (r == 0 && c == 0) ? inject_00_nw
                                 : (r > 0  && c > 0 ) ? grid_se[r-1][c-1]
                                 :                       36'b0;

                mesh_tile #(
                    .TILE_ID(THIS_TILE_ID),
                    .MESH_R (MESH_R),
                    .MESH_C (MESH_C)
                ) tile_inst (
                    .clk      (clk),
                    .rst      (!cpu_rst_n),
                    .boot_mode(boot_mode),
                    .boot_addr(boot_addr),
                    .boot_data(boot_data),
                    .boot_wen (boot_wen),

                    .north_in (n_i),  .south_in (s_i),
                    .east_in  (e_i),  .west_in  (w_i),

                    .north_out(grid_n [r][c]), .south_out(grid_s [r][c]),
                    .east_out (grid_e [r][c]), .west_out (grid_w [r][c]),

                    .ne_in (ne_i), .nw_in (nw_i),
                    .se_in (se_i), .sw_in (sw_i),

                    .ne_out(grid_ne[r][c]), .nw_out(grid_nw[r][c]),
                    .se_out(grid_se[r][c]), .sw_out(grid_sw[r][c]),

                    .dft_mode (dft_mode),
                    .dft_ce   (tile_dft_ce[r][c]),
                    .dft_we   (dft_we),
                    .dft_addr (dft_addr),
                    .dft_wdata(dft_wdata),
                    .dft_rdata(tile_dft_rdata[r][c])
                );
            end
        end
    endgenerate

    // SE output of the bottom-right tile — useful for testbench probing
    assign monitor_se = grid_se[MESH_R-1][MESH_C-1];

    // =========================================================================
    // INTER-TILE WIRE MONITOR (RTL only — disabled in gate-level sim)
    // =========================================================================
`ifndef FUNCTIONAL
    generate
        if (MESH_C >= 2) begin : wire_monitor
            always @(posedge clk) begin

                // ── (0,0) east output ─────────────────────────────────────────
                if (rows[0].cols[0].tile_inst.router_inst.e_out[33]) begin
                    $display("[WIRE t=%0t] (0,0).east_out   VALID  dest=%0d  payload=0x%08x",
                             $time,
                             rows[0].cols[0].tile_inst.router_inst.e_out[32:29],
                             rows[0].cols[0].tile_inst.router_inst.e_out[28:0]);
                end

                // ── (0,1) west input ──────────────────────────────────────────
                if (rows[0].cols[1].tile_inst.router_inst.w_in[33]) begin
                    $display("[WIRE t=%0t] (0,1).west_in     VALID  dest=%0d  payload=0x%08x",
                             $time,
                             rows[0].cols[1].tile_inst.router_inst.w_in[32:29],
                             rows[0].cols[1].tile_inst.router_inst.w_in[28:0]);
                end

                // ── (0,1) ejection decision ───────────────────────────────────
                if (rows[0].cols[1].tile_inst.router_inst.next_eject[33]) begin
                    $display("[WIRE t=%0t] (0,1).next_eject  VALID  payload=0x%08x  fifo_full=%0b  fifo_count=%0d",
                             $time,
                             rows[0].cols[1].tile_inst.router_inst.next_eject[28:0],
                             rows[0].cols[1].tile_inst.router_inst.fifo_full,
                             rows[0].cols[1].tile_inst.router_inst.fifo_count);
                end

                // ── (0,1) FIFO push confirmation ──────────────────────────────
                if (rows[0].cols[1].tile_inst.router_inst.fifo_push) begin
                    $display("[WIRE t=%0t] (0,1).fifo_push   count_after=%0d",
                             $time,
                             rows[0].cols[1].tile_inst.router_inst.fifo_count + 1);
                end

                // ── (0,0) inject flit ─────────────────────────────────────────
                if (rows[0].cols[0].tile_inst.router_inst.inject_flit[33]) begin
                    $display("[WIRE t=%0t] (0,0).inject_flit VALID  dest=%0d  payload=0x%08x",
                             $time,
                             rows[0].cols[0].tile_inst.router_inst.inject_flit[32:29],
                             rows[0].cols[0].tile_inst.router_inst.inject_flit[28:0]);
                end

                $fflush();
            end
        end
    endgenerate
`endif

endmodule