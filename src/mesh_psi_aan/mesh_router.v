// Copyright 2026 [Aan Yadav & Psi Padhya]
// SPDX-License-Identifier: Apache-2.0

`default_nettype none

// ============================================================================
// mesh_router — XY dimension-order router for a MESH_R × MESH_C mesh
//
// TILE_ID encoding (6-bit):
//   [5:3] = row   (0..7)
//   [2:0] = col   (0..7)
//
// Flit format: 36 bits total
//   [35]    = valid
//   [34:29] = dest TILE_ID  (6 bits: {dest_row[2:0], dest_col[2:0]})
//   [28:0]  = payload (unchanged end-to-end)
//
// MESH_R / MESH_C are used for display messages and boundary-aware
// diagnostics; routing logic is purely based on comparing dest vs MY_ID
// and is naturally correct for any rectangular mesh.
// ============================================================================
module mesh_router #(
    parameter [5:0]   MY_ID  = 6'b000000,
    parameter integer MESH_R = 5,   // mesh rows    — for display / diagnostics
    parameter integer MESH_C = 5    // mesh columns — for display / diagnostics
)(
    input  wire        clk,
    input  wire        rst,

    // Wishbone local CPU interface
    input  wire [31:0] local_wb_adr,
    input  wire [31:0] local_wb_dat_o,   // CPU→router (32-bit, matches SERV)
    output wire [31:0] local_wb_dat_i,   // router→CPU (32-bit, matches SERV)
    input  wire        local_wb_we,
    input  wire        local_wb_stb,
    output wire        local_wb_ack,

    // Cardinal outputs/inputs
    output reg  [35:0] n_out, s_out, e_out, w_out,
    input  wire [35:0] n_in,  s_in,  e_in,  w_in,

    // Diagonal outputs/inputs
    output reg  [35:0] ne_out, nw_out, se_out, sw_out,
    input  wire [35:0] ne_in,  nw_in,  se_in,  sw_in
);

    // Decode this tile's position from MY_ID
    wire [2:0] my_row = MY_ID[5:3];
    wire [2:0] my_col = MY_ID[2:0];

    // -------------------------------------------------------------------------
    // Wishbone ack — registered, fires one cycle after stb
    // -------------------------------------------------------------------------
    reg wb_ack_reg;
    always @(posedge clk) begin
        if (rst) wb_ack_reg <= 1'b0;
        else     wb_ack_reg <= local_wb_stb & !wb_ack_reg;
    end
    assign local_wb_ack = wb_ack_reg;

    // -------------------------------------------------------------------------
    // Injection
    // SERV is bit-serial: dat_o is only guaranteed stable on the ack cycle.
    // -------------------------------------------------------------------------
    reg [35:0] inject_flit;

    wire inject_ack_cycle = wb_ack_reg &&
                            local_wb_we &&
                            (local_wb_adr == 32'h80000000);

    always @(posedge clk) begin
        if (rst) begin
            inject_flit <= 36'h0;
        end else begin
            inject_flit <= 36'h0;
            if (inject_ack_cycle) begin
                inject_flit <= {1'b1,
                                local_wb_dat_o[31:26],   // dest TILE_ID [5:0]
                                local_wb_dat_o[28:0]};   // payload
            end
        end
    end

    // -------------------------------------------------------------------------
    // Ejection — 4-deep synchronous FIFO
    // -------------------------------------------------------------------------
    localparam FIFO_DEPTH = 4;
    localparam FIFO_BITS  = 2;

    reg [35:0]          fifo_mem   [0:FIFO_DEPTH-1];
    reg [FIFO_BITS-1:0] fifo_wr_ptr, fifo_rd_ptr;
    reg [FIFO_BITS:0]   fifo_count;

    wire fifo_empty = (fifo_count == 0);
    wire fifo_full  = (fifo_count == FIFO_DEPTH[FIFO_BITS:0]);

    wire cpu_read = local_wb_stb && !local_wb_we &&
                    (local_wb_adr == 32'h80000004);

    wire [35:0] eject_flit_next;
    wire fifo_push = eject_flit_next[35] && !fifo_full;

    reg cpu_read_q;
    always @(posedge clk) begin
        if (rst) cpu_read_q <= 1'b0;
        else     cpu_read_q <= cpu_read && !fifo_empty;
    end
    wire fifo_pop = cpu_read_q && !fifo_empty;

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            fifo_wr_ptr <= 0;
            fifo_rd_ptr <= 0;
            fifo_count  <= 0;
            for (i = 0; i < FIFO_DEPTH; i = i + 1)
                fifo_mem[i] <= 36'h0;
        end else begin
            if (fifo_push) begin
                fifo_mem[fifo_wr_ptr] <= eject_flit_next;
                fifo_wr_ptr           <= fifo_wr_ptr + 1;
            end
            if (fifo_pop)
                fifo_rd_ptr <= fifo_rd_ptr + 1;

            if      ( fifo_push && !fifo_pop) fifo_count <= fifo_count + 1;
            else if (!fifo_push &&  fifo_pop) fifo_count <= fifo_count - 1;
        end
    end

    wire [35:0] fifo_head_comb = fifo_empty ? 36'h0 : fifo_mem[fifo_rd_ptr];

    // CPU reads:
    //   0x80000008 → my TILE_ID (6-bit, zero-extended)
    //   0x80000004 → head of ejection FIFO (payload only)
    assign local_wb_dat_i =
        (local_wb_stb && !local_wb_we && local_wb_adr == 32'h80000008)
            ? {26'b0, MY_ID}
        : (local_wb_stb && !local_wb_we && local_wb_adr == 32'h80000004)
            ? {3'b0, fifo_head_comb[28:0]}
        : 32'h0;

    // -------------------------------------------------------------------------
    // Routing — XY dimension-order, fully combinatorial
    //
    // Priority: inject > n_in > s_in > e_in > w_in > ne_in > nw_in > se_in > sw_in
    // A flit is ejected when dest == my (row,col).
    // Otherwise: row mismatch first (N/S), then col mismatch (E/W).
    // -------------------------------------------------------------------------
    reg [35:0] next_n, next_s, next_e, next_w;
    reg [35:0] next_ne, next_nw, next_se, next_sw;
    reg [35:0] next_eject;

    task automatic route_one;
        input [35:0] flit;
        reg [2:0] tgt_row, tgt_col;
        begin
            tgt_row = flit[34:32];   // dest row  in bits [34:32]
            tgt_col = flit[31:29];   // dest col  in bits [31:29]
            if (tgt_row == my_row && tgt_col == my_col) begin
                if (!next_eject[35] && !fifo_full) next_eject = flit;
            end else if (tgt_row > my_row) begin
                if (!next_s[35]) next_s = flit;
            end else if (tgt_row < my_row) begin
                if (!next_n[35]) next_n = flit;
            end else if (tgt_col > my_col) begin
                if (!next_e[35]) next_e = flit;
            end else begin
                if (!next_w[35]) next_w = flit;
            end
        end
    endtask

    always @(*) begin
        {next_n, next_s, next_e, next_w,
         next_ne, next_nw, next_se, next_sw, next_eject} = 0;

        if (inject_flit[35]) route_one(inject_flit);
        if (n_in[35])        route_one(n_in);
        if (s_in[35])        route_one(s_in);
        if (e_in[35])        route_one(e_in);
        if (w_in[35])        route_one(w_in);
        if (ne_in[35])       route_one(ne_in);
        if (nw_in[35])       route_one(nw_in);
        if (se_in[35])       route_one(se_in);
        if (sw_in[35])       route_one(sw_in);
    end

    assign eject_flit_next = next_eject;

    // ── TILE(0,0) transit & misroute monitor ─────────────────────────────────
    // Shows every flit that passes THROUGH tile (0,0) without being ejected,
    // and flags any ghost flit that is being forwarded instead of ejected
    // (which would indicate a destination ID mismatch / routing bug).
    // Combinatorial — fires in the same always @(*) evaluation window.
    always @(*) begin
        if (MY_ID == 0) begin
            // South-bound transit (heading to row 1 or 2)
            if (next_s[33])
                $display("[TRANSIT t=%0t] TILE(0,0) ->S  dest=%0d bmap=0x%03x bit10=%0b raw=0x%09x",
                         $time, {next_s[32:29]}, next_s[9:0], next_s[10], next_s);
            // East-bound transit (heading to col 1 or 2)
            if (next_e[33])
                $display("[TRANSIT t=%0t] TILE(0,0) ->E  dest=%0d bmap=0x%03x bit10=%0b raw=0x%09x",
                         $time, {next_e[32:29]}, next_e[9:0], next_e[10], next_e);
            // SE diagonal transit
            if (next_se[33])
                $display("[TRANSIT t=%0t] TILE(0,0) ->SE dest=%0d bmap=0x%03x bit10=%0b raw=0x%09x",
                         $time, {next_se[32:29]}, next_se[9:0], next_se[10], next_se);
            // Ghost flit being forwarded instead of ejected — this is a bug
            if (next_s[33]  && next_s[10])
                $display("[MISROUTE t=%0t] TILE(0,0) ghost going ->S  dest=%0d (expected dest=0) raw=0x%09x",
                         $time, {next_s[32:29]}, next_s);
            if (next_e[33]  && next_e[10])
                $display("[MISROUTE t=%0t] TILE(0,0) ghost going ->E  dest=%0d (expected dest=0) raw=0x%09x",
                         $time, {next_e[32:29]}, next_e);
            if (next_se[33] && next_se[10])
                $display("[MISROUTE t=%0t] TILE(0,0) ghost going ->SE dest=%0d (expected dest=0) raw=0x%09x",
                         $time, {next_se[32:29]}, next_se);
            if (MY_ID == 0 && cpu_read) begin
                $display("[RD1 t=%0t] ID=0 reading FIFO head=0x%08x empty=%b",
                        $time, fifo_head_comb[28:0], fifo_empty);
            end
            if (MY_ID == 1 && cpu_read) begin
                $display("[RD1 t=%0t] ID=1 reading FIFO head=0x%08x empty=%b",
                        $time, fifo_head_comb[28:0], fifo_empty);
            end
            if (MY_ID == 2 && cpu_read) begin
                $display("[RD1 t=%0t] ID=2 reading FIFO head=0x%08x empty=%b",
                        $time, fifo_head_comb[28:0], fifo_empty);
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            {n_out, s_out, e_out, w_out,
             ne_out, nw_out, se_out, sw_out} <= 0;
        end else begin
            n_out  <= next_n;  s_out  <= next_s;
            e_out  <= next_e;  w_out  <= next_w;
            ne_out <= next_ne; nw_out <= next_nw;
            se_out <= next_se; sw_out <= next_sw;

            // ── Wishbone write cycle monitor ─────────────────────────────────
            if (local_wb_stb && local_wb_we && local_wb_adr == 32'h80000000) begin
                $display("[WB_CYCLE t=%0t] TILE=%0d stb=%b we=%b ack=%b dat_o=0x%08x",
                        $time, MY_ID,
                        local_wb_stb, local_wb_we, wb_ack_reg,
                        local_wb_dat_o);
            end

            // ── Flit injected by local CPU ────────────────────────────────────
            if (inject_ack_cycle)
                $display("[FLIT t=%0t] TILE=%0d INJECT dest_row=%0d dest_col=%0d payload=0x%08x",
                         $time, MY_ID,
                         local_wb_dat_o[31:29], local_wb_dat_o[28:26],
                         local_wb_dat_o[28:0]);

            // ── Flit ejected into local FIFO ──────────────────────────────────
            if (fifo_push)
                $display("[FLIT t=%0t] TILE=%0d EJECT  payload=0x%08x fifo_count_after=%0d",
                         $time, MY_ID, eject_flit_next[28:0], fifo_count + 1);

            // ── Dropped flit (FIFO full) ──────────────────────────────────────
            if (eject_flit_next[35] && fifo_full)
                $display("[GHOST_DROP t=%0t] TILE(%0d,%0d) DROPPED bmap=0x%03x",
                         $time, MY_ID[5:3], MY_ID[2:0], eject_flit_next[9:0]);

            // ── Forwarded flits ───────────────────────────────────────────────
            if (next_e[35])
                $display("[FLIT t=%0t] TILE=%0d ->E dest_row=%0d dest_col=%0d payload=0x%08x",
                         $time, MY_ID, next_e[34:32], next_e[31:29], next_e[28:0]);
            if (next_w[35])
                $display("[FLIT t=%0t] TILE=%0d ->W dest_row=%0d dest_col=%0d payload=0x%08x",
                         $time, MY_ID, next_w[34:32], next_w[31:29], next_w[28:0]);
            if (next_n[35])
                $display("[FLIT t=%0t] TILE=%0d ->N dest_row=%0d dest_col=%0d payload=0x%08x",
                         $time, MY_ID, next_n[34:32], next_n[31:29], next_n[28:0]);
            if (next_s[35])
                $display("[FLIT t=%0t] TILE=%0d ->S dest_row=%0d dest_col=%0d payload=0x%08x",
                         $time, MY_ID, next_s[34:32], next_s[31:29], next_s[28:0]);

            $fflush();
        end
    end

endmodule