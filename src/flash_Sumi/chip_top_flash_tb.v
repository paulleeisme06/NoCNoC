`default_nettype none
`timescale 1ns/1ps

module chip_top_flash_tb;

    reg clk;
    reg rst_n;

    wire        clk_PAD;
    wire        rst_n_PAD;
    wire [11:0] input_PAD;
    wire [39:0] bidir_PAD;
    wire [1:0]  analog_PAD;

    wire flash_cs_n  = bidir_PAD[1];
    wire flash_clk   = bidir_PAD[2];
    wire flash_mosi  = bidir_PAD[3];
    wire flash_miso;
    wire wp_neg      = 1'b1;
    wire io3_reset_n = 1'b1;

    assign clk_PAD        = clk;
    assign rst_n_PAD      = rst_n;
    assign input_PAD[4]   = flash_miso;
    assign input_PAD[3:0] = 4'b0;
    assign input_PAD[11:5]= 7'b0;
    assign analog_PAD     = 2'bz;

    initial clk = 0;
    always #5 clk = ~clk;

    initial begin
        rst_n = 0;
        repeat(8) @(posedge clk);
        rst_n = 1;
    end

    chip_top dut (
        .clk_PAD   (clk_PAD),
        .rst_n_PAD (rst_n_PAD),
        .input_PAD (input_PAD),
        .bidir_PAD (bidir_PAD),
        .analog_PAD(analog_PAD)
    );

    s25fl128l #(
        .tdevice_PU    (1),
        .UserPreload   (1),
        .mem_file_name ("s25fl128l.mem"),
        .secr_file_name("s25fl128lSECR.mem")
    ) flash_model (
        .SI          (flash_mosi),
        .SO          (flash_miso),
        .SCK         (flash_clk),
        .CSNeg       (flash_cs_n),
        .RESETNeg    (rst_n),
        .WPNeg       (wp_neg),
        .IO3_RESETNeg(io3_reset_n)
    );

    always @(flash_miso)
        force dut.i_chip_core.u_mesh.boot_inst.flash_miso = flash_miso;

    initial begin
        #100;
        $display("[MEM] mem[0]=%02h mem[1]=%02h",
                 flash_model.Mem[0], flash_model.Mem[1]);
    end

    initial begin
        wait(dut.i_chip_core.u_mesh.boot_inst.sram_wen === 1'b0 && $time > 1000);
        @(posedge clk);
        $display("[WRITE t=%0t] CEN=%b GWEN=%b D=%02h A=%03h bm=%b bw=%b tw=%b sa=%b no_st=%b no_hd=%b cen_fell=%b",
                 $time,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.CEN,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.GWEN,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.D,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.A,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.boot_mode,
                 dut.i_chip_core.u_mesh.boot_inst.sram_wen,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.boot_wen,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_active,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.no_st_viol,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.no_hd_viol,
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.cen_fell);
        #1;
        $display("[AFTER1ps] mem[0]=%02h",dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.mem[0]);
        #1000;
        $display("[AFTER1ns] mem[0]=%02h mem[1]=%02h",
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.mem[0],
                 dut.i_chip_core.u_mesh.rows[0].cols[0].tile_inst.sram_inst.mem[1]);
    end

    initial begin
        #50_000_000;
        $display("TIMEOUT");
        $finish;
    end

endmodule
`default_nettype wire
