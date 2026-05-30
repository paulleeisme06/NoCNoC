`default_nettype none

// ============================================================================
// mesh_tile — one RISC-V tile in the MESH_R × MESH_C mesh
//
// Parameters:
//   TILE_ID  : 6-bit tile identifier, encoded as {row[2:0], col[2:0]}
//   MESH_R   : mesh row count    (passed through to router)
//   MESH_C   : mesh column count (passed through to router)
//
// Do NOT hardcode MESH_R / MESH_C here — they are driven from mesh_nxn.v
// which reads them from mesh_config.vh.
// ============================================================================
module mesh_tile #(
    parameter [5:0]   TILE_ID = 6'b000000,
    parameter integer MESH_R  = 5,
    parameter integer MESH_C  = 5
)(
`ifdef USE_POWER_PINS
    inout wire VDD,
    inout wire VSS,
`endif
    input wire clk,
    input wire rst,
    input wire boot_mode,
    input wire [10:0] boot_addr,
    input wire [7:0]  boot_data,
    input wire        boot_wen,

    input  wire [35:0] north_in, south_in, east_in, west_in,
    output wire [35:0] north_out, south_out, east_out, west_out,
    input  wire [35:0] ne_in, nw_in, se_in, sw_in,
    output wire [35:0] ne_out, nw_out, se_out, sw_out,

    input  wire        dft_mode,
    input  wire        dft_ce,
    input  wire        dft_we,
    input  wire [10:0] dft_addr,
    input  wire [7:0]  dft_wdata,
    output wire [7:0]  dft_rdata
);

`ifndef SYNTHESIS
    initial begin
        $display("[TILE_ID_CHECK] TILE_ID param = %0d  (row=%0d col=%0d)",
                TILE_ID, TILE_ID[5:3], TILE_ID[2:0]);
    end
`endif

    wire [31:0] wb_adr, wb_dat_c2r, wb_dat_r2c;
    wire [3:0]  wb_sel;
    wire        wb_we, wb_stb, wb_ack;

    wire [10:0] sram_waddr, sram_raddr;
    wire [7:0]  sram_wdata, sram_rdata;
    wire        sram_wen, sram_ren;

    // -------------------------------------------------------------------------
    // Address pipeline registers
    // -------------------------------------------------------------------------
    reg [10:0] sram_raddr_reg;
    reg [10:0] sram_waddr_reg;
    reg [7:0]  sram_wdata_reg;
    reg        sram_wen_reg;

    always @(posedge clk) begin
        if (rst) begin
            sram_raddr_reg <= 0;
            sram_waddr_reg <= 0;
            sram_wdata_reg <= 0;
            sram_wen_reg   <= 0;
        end else begin
            sram_raddr_reg <= sram_raddr;
            sram_waddr_reg <= sram_waddr;
            sram_wdata_reg <= sram_wdata;
            sram_wen_reg   <= sram_wen;
        end
    end

    // -------------------------------------------------------------------------
    // Address / Data MUX (declared here so all always blocks below can use them)
    // -------------------------------------------------------------------------
    wire [10:0] final_a = boot_mode ? boot_addr  : (sram_wen ? sram_waddr : sram_raddr);
    wire [7:0]  final_d = boot_mode ? boot_data  : sram_wdata;

    // -------------------------------------------------------------------------
    // Seed/grid write monitor (tile 1 only)
    // -------------------------------------------------------------------------
`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (!boot_mode && TILE_ID == 1) begin
            if (sram_wen && final_a >= 11'h500 && final_a <= 11'h563)
                $display("[SEED_WRITE t=%0t] TILE=1 addr=0x%03x data=0x%02x",
                        $time, final_a, final_d);
            if (!sram_wen && final_a >= 11'h500 && final_a <= 11'h563)
                $display("[SEED_READ  t=%0t] TILE=1 addr=0x%03x data=0x%02x",
                        $time, final_a, sram_rdata);
        end
    end
`endif

    // -------------------------------------------------------------------------
    // CEN startup-pulse generator
    // -------------------------------------------------------------------------
    reg boot_mode_q;
    always @(posedge clk or posedge rst)
        if (rst) boot_mode_q <= 1'b1;
        else     boot_mode_q <= boot_mode;

    wire cpu_sram_init_pulse = boot_mode_q & ~boot_mode;

    // -------------------------------------------------------------------------
    // SRAM control signals
    // -------------------------------------------------------------------------
    wire sram_active = boot_mode ? ~boot_wen            : ~cpu_sram_init_pulse;
    wire sram_write  = boot_mode ? ~boot_wen            : sram_wen;

    // -------------------------------------------------------------------------
    // Subservient RISC-V core (bit-serial SERV)
    // -------------------------------------------------------------------------
    subservient_core #(.memsize(2048)) core_inst (
        .i_clk       (clk),
        .i_rst       (rst | boot_mode | dft_mode),
        .o_sram_waddr(sram_waddr),
        .o_sram_wdata(sram_wdata),
        .o_sram_wen  (sram_wen),
        .o_sram_raddr(sram_raddr),
        .i_sram_rdata(sram_rdata),
        .o_sram_ren  (sram_ren),
        .o_wb_adr    (wb_adr),
        .o_wb_dat    (wb_dat_c2r),
        .i_wb_rdt    (wb_dat_r2c),
        .o_wb_we     (wb_we),
        .o_wb_stb    (wb_stb),
        .i_wb_ack    (wb_ack),
        .i_timer_irq (1'b0)
    );

    // -------------------------------------------------------------------------
    // GF180 2048×8 SRAM
    // -------------------------------------------------------------------------
    sram2048x8_gf180 sram2048 (
`ifdef USE_POWER_PINS
        .VDD (VDD),
        .VSS (VSS),
`endif
        .CLK (clk),
        .CEN (dft_ce ? 1'b0       : ~sram_active),
        .GWEN(dft_ce ? ~dft_we    : ~sram_write),
        .WEN (8'b0),
        .A   (dft_ce ? dft_addr   : final_a),
        .D   (dft_ce ? dft_wdata  : final_d),
        .Q   (sram_rdata)
    );

    assign dft_rdata = sram_rdata;

    // -------------------------------------------------------------------------
    // Debug: SRAM write monitor — tile 1
    // -------------------------------------------------------------------------
`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (!boot_mode && sram_wen && final_d != 8'h00 && TILE_ID == 1) begin
            $display("[SRAM t=%0t] MY_ID=%0d WRITE addr=0x%03x data=0x%02x",
                     $time, TILE_ID, final_a, final_d);
        end
    end
`endif

    // -------------------------------------------------------------------------
    // Debug: SRAM read monitor — tile 1
    // -------------------------------------------------------------------------
`ifndef SYNTHESIS
    reg [10:0] prev_raddr_t1;
    reg        prev_rvalid_t1;
    always @(posedge clk) begin
        if (!boot_mode && TILE_ID == 1) begin
            prev_raddr_t1  <= final_a;
            prev_rvalid_t1 <= !sram_wen;
        end else begin
            prev_rvalid_t1 <= 1'b0;
        end
        if (prev_rvalid_t1)
            $display("[SRAM_READ t=%0t] MY_ID=%0d READ addr=0x%03x data=0x%02x (1-cycle-delayed)",
                     $time, TILE_ID, prev_raddr_t1, sram_rdata);
    end
`endif

    // -------------------------------------------------------------------------
    // Debug: ghost buffer write monitor — tile(0,0)
    // -------------------------------------------------------------------------
`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (!boot_mode && TILE_ID == 0 && sram_wen) begin
            if (final_a >= 11'h600 && final_a <= 11'h609)
                $display("[GHOST_BUF t=%0t] TILE(0,0) ghost_N[%0d] <= %0d  (addr=0x%03x)",
                         $time, {21'b0, final_a} - 32'h600, final_d, final_a);
            if (final_a >= 11'h60A && final_a <= 11'h613)
                $display("[GHOST_BUF t=%0t] TILE(0,0) ghost_S[%0d] <= %0d  (addr=0x%03x)",
                         $time, {21'b0, final_a} - 32'h60A, final_d, final_a);
            if (final_a >= 11'h614 && final_a <= 11'h61D)
                $display("[GHOST_BUF t=%0t] TILE(0,0) ghost_W[%0d] <= %0d  (addr=0x%03x)",
                         $time, {21'b0, final_a} - 32'h614, final_d, final_a);
            if (final_a >= 11'h61E && final_a <= 11'h627)
                $display("[GHOST_BUF t=%0t] TILE(0,0) ghost_E[%0d] <= %0d  (addr=0x%03x)",
                         $time, {21'b0, final_a} - 32'h61E, final_d, final_a);
        end
    end
`endif

    // -------------------------------------------------------------------------
    // Debug: next_grid border-cell write monitor — tile(0,0)
    // -------------------------------------------------------------------------
`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (!boot_mode && TILE_ID == 0 && sram_wen &&
            final_a >= 11'h640 && final_a <= 11'h6A3) begin
            begin : ng_block
                integer ng_off, ng_row, ng_col;
                ng_off = {21'b0, final_a} - 32'h640;
                ng_row = ng_off / 10;
                ng_col = ng_off % 10;
                if (ng_row == 0 || ng_row == 9 || ng_col == 0 || ng_col == 9)
                    $display("[NEXT_GRID t=%0t] TILE(0,0) next_grid[%0d][%0d] <= %0d  (addr=0x%03x)",
                             $time, ng_row, ng_col, final_d, final_a);
            end
        end
    end
`endif

    // -------------------------------------------------------------------------
    // Mesh router
    // -------------------------------------------------------------------------
    mesh_router #(
        .MY_ID (TILE_ID),
        .MESH_R(MESH_R),
        .MESH_C(MESH_C)
    ) router_inst (
        .clk            (clk),
        .rst            (rst),
        .local_wb_adr   (wb_adr),
        .local_wb_dat_o (wb_dat_c2r),
        .local_wb_dat_i (wb_dat_r2c),
        .local_wb_we    (wb_we),
        .local_wb_stb   (wb_stb),
        .local_wb_ack   (wb_ack),
        .n_in (north_in),  .s_in (south_in),  .e_in (east_in),  .w_in (west_in),
        .n_out(north_out), .s_out(south_out), .e_out(east_out), .w_out(west_out),
        .ne_in(ne_in),  .nw_in(nw_in),  .se_in(se_in),  .sw_in(sw_in),
        .ne_out(ne_out),.nw_out(nw_out),.se_out(se_out),.sw_out(sw_out)
    );

endmodule