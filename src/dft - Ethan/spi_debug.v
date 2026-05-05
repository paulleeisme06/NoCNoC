`default_nettype none

// SPI debug slave — 32-bit frames, SPI mode 0 (CPOL=0 CPHA=0), MSB first.
//
// Frame layout (bit 31 sent first on MOSI):
//   [31]     R/W  — 1 = write, 0 = read
//   [30:27]  tile_id[3:0] = { row[1:0], col[1:0] }
//   [26:16]  addr[10:0]
//   [15:8]   don't-care
//   [7:0]    write data (MOSI) on writes; read data (MISO) on reads
//
// Read  : dbg_req issued after bit 15 (addr fully received).
//         SRAM ack arrives within a few sys_clk cycles.
//         Read data driven on MISO during bits 24-31 of the same frame.
//
// Write : full frame captured first; dbg_req issued on the rising edge of CS.
//
// Requirement: spi_clk must be slow enough that the 2-FF synchroniser
// resolves cleanly (spi_clk_period > 4 * clk_period is safe).

module spi_debug (
    input  wire        clk,
    input  wire        rst,

    input  wire        spi_clk,
    input  wire        spi_cs_n,
    input  wire        spi_mosi,
    output reg         spi_miso,

    output reg         dbg_req,
    output reg         dbg_we,
    output reg  [3:0]  dbg_tile_id,
    output reg  [10:0] dbg_addr,
    output reg  [7:0]  dbg_wdata,
    input  wire [7:0]  dbg_rdata,
    input  wire        dbg_ack
);

    // 2-FF synchronisers
    reg [1:0] sclk_r, cs_r;
    reg       mosi_r;
    always @(posedge clk or posedge rst) begin
        if (rst) begin
            sclk_r <= 2'b00;
            cs_r   <= 2'b11;
            mosi_r <= 1'b0;
        end else begin
            sclk_r <= {sclk_r[0], spi_clk};
            cs_r   <= {cs_r[0],   spi_cs_n};
            mosi_r <= spi_mosi;
        end
    end

    wire sclk_rise = (sclk_r == 2'b01);
    wire sclk_fall = (sclk_r == 2'b10);
    wire cs_active = ~cs_r[1];
    wire cs_fall   = (cs_r == 2'b10);
    wire cs_rise   = (cs_r == 2'b01);

    reg [31:0] shreg;
    reg  [5:0] bit_cnt;
    reg  [7:0] miso_shift;

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            shreg       <= 32'b0;
            bit_cnt     <= 6'b0;
            miso_shift  <= 8'b0;
            spi_miso    <= 1'b0;
            dbg_req     <= 1'b0;
            dbg_we      <= 1'b0;
            dbg_tile_id <= 4'b0;
            dbg_addr    <= 11'b0;
            dbg_wdata   <= 8'b0;
        end else begin
            dbg_req <= 1'b0;

            if (cs_fall)
                bit_cnt <= 6'b0;

            if (sclk_rise && cs_active) begin
                shreg   <= {shreg[30:0], mosi_r};
                bit_cnt <= bit_cnt + 1;

                // After 16 bits the address is complete.
                // shreg (pre-shift) holds bits [31:17]; mosi_r holds bit [16].
                //   shreg[14]   = bit[31] = R/W
                //   shreg[13:10]= bit[30:27] = tile_id
                //   shreg[9:0]  = bit[26:17] = addr[10:1]
                //   mosi_r      = bit[16]    = addr[0]
                if (bit_cnt == 6'd15) begin
                    dbg_we      <= shreg[14];
                    dbg_tile_id <= shreg[13:10];
                    dbg_addr    <= {shreg[9:0], mosi_r};
                    if (~shreg[14])          // read: request now, data in time for bit 24
                        dbg_req <= 1'b1;
                end
            end

            // Latch read data when SRAM acks (ignore ack on writes)
            if (dbg_ack && ~dbg_we)
                miso_shift <= dbg_rdata;

            // Drive MISO on falling SCK.
            // bit_cnt reflects bits received so far (post last rising edge).
            // Falling edges at bit_cnt 23..30 set up MISO bits [7:0].
            if (sclk_fall && cs_active) begin
                if (bit_cnt >= 6'd23 && bit_cnt <= 6'd30) begin
                    spi_miso   <= miso_shift[7];
                    miso_shift <= {miso_shift[6:0], 1'b0};
                end else
                    spi_miso <= 1'b0;
            end

            // CS deasserts after a full 32-bit frame: execute write.
            // shreg now holds the complete frame with shreg[31]=R/W.
            if (cs_rise && bit_cnt == 6'd32 && shreg[31]) begin
                dbg_we      <= 1'b1;
                dbg_tile_id <= shreg[30:27];
                dbg_addr    <= shreg[26:16];
                dbg_wdata   <= shreg[7:0];
                dbg_req     <= 1'b1;
            end
        end
    end

endmodule
