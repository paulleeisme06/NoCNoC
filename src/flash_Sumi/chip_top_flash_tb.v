`default_nettype none
`timescale 1ns/1ps

module chip_top_flash_tb;

    reg clk;
    reg rst_n;

    // Pad wires
    wire        clk_PAD;
    wire        rst_n_PAD;
    wire [11:0] input_PAD;
    wire [39:0] bidir_PAD;
    wire [1:0]  analog_PAD;

    // Flash signals
    wire flash_cs_n  = bidir_PAD[1];
    wire flash_clk   = bidir_PAD[2];
    wire flash_mosi  = bidir_PAD[3];
    wire flash_miso;
    wire wp_neg      = 1'b1;
    wire io3_reset_n = 1'b1;

    assign clk_PAD   = clk;
    assign rst_n_PAD = rst_n;
    assign input_PAD[4]    = flash_miso;
    assign input_PAD[3:0]  = 4'b0;
    assign input_PAD[11:5] = 7'b0;
    assign analog_PAD = 2'bz;

    // Clock
    initial clk = 0;
    always #5 clk = ~clk;

    // Reset
    initial begin
        rst_n = 0;
        repeat(8) @(posedge clk);
        rst_n = 1;
    end

    // DUT
    chip_top dut (
        .clk_PAD   (clk_PAD),
        .rst_n_PAD (rst_n_PAD),
        .input_PAD (input_PAD),
        .bidir_PAD (bidir_PAD),
        .analog_PAD(analog_PAD)
    );

    // Flash model
    s25fl128l flash_model (
        .SI          (flash_mosi),
        .SO          (flash_miso),
        .SCK         (flash_clk),
        .CSNeg       (flash_cs_n),
        .RESETNeg    (rst_n),
        .WPNeg       (wp_neg),
        .IO3_RESETNeg(io3_reset_n)
    );

    // Force miso into boot_controller bypassing pad
    initial forever begin
        #1;
        force dut.i_chip_core.u_mesh.boot_inst.flash_miso = flash_miso;
    end

initial begin
    @(negedge dut.i_chip_core.u_mesh.boot_inst.flash_cs_n);
    repeat(40) begin
        @(posedge dut.i_chip_core.u_mesh.boot_inst.flash_clk);
        $display("[CMD t=%0t] mosi=%b state=%0d shift=%08b",
                 $time,
                 dut.i_chip_core.u_mesh.boot_inst.flash_mosi,
                 dut.i_chip_core.u_mesh.boot_inst.state,
                 dut.i_chip_core.u_mesh.boot_inst.shift_reg);
    end
end

    // Debug
    initial begin
        #200000;
        repeat(40) begin
            @(posedge dut.i_chip_core.u_mesh.boot_inst.flash_clk);
            $display("[MISO t=%0t] SO=%b cs_n=%b miso_internal=%b",
                     $time, flash_model.SO, flash_cs_n,
                     dut.i_chip_core.u_mesh.boot_inst.flash_miso);
        end
    end

    // CS monitor
    initial begin
        @(negedge flash_cs_n);
        $display("[TB t=%0t] flash_cs_n went LOW", $time);
        @(posedge flash_cs_n);
        $display("[TB t=%0t] flash_cs_n went HIGH", $time);
    end

    initial begin
    #80100;
    repeat(10) begin
        #200;
        $display("[CLK t=%0t] flash_clk=%b mosi=%b", $time, flash_clk, flash_mosi);
    end
end

initial begin
    #80100;
    repeat(20) begin
        #200;
        $display("[SPI t=%0t] cs=%b clk=%b mosi=%b SO=%b",
                 $time, flash_model.CSNeg, flash_model.SCK,
                 flash_model.SI, flash_model.SO);
    end
end

initial begin
    #80100;
    repeat(20) begin
        #200;
$display("[BC t=%0t] bc_rst_n=%b mesh_rst=%b chip_rst_n=%b",
         $time,
         dut.i_chip_core.u_mesh.boot_inst.rst_n,
         dut.i_chip_core.u_mesh.rst,
         dut.i_chip_core.rst_n);
        $display("[BC t=%0t] bc_mosi=%b bc_clk=%b bc_cs=%b",
                 $time,
                 dut.i_chip_core.u_mesh.boot_inst.flash_mosi,
                 dut.i_chip_core.u_mesh.boot_inst.flash_clk,
                 dut.i_chip_core.u_mesh.boot_inst.flash_cs_n);
    end
end

initial begin
    #82000;
    $display("[RST t=%0t] bc_rst_n=%b mesh_rst=%b chip_rst_n=%b state=%0d",
             $time,
             dut.i_chip_core.u_mesh.boot_inst.rst_n,
             dut.i_chip_core.u_mesh.rst,
             dut.i_chip_core.rst_n,
             dut.i_chip_core.u_mesh.boot_inst.state);
end



    // Timeout
    initial begin
        #5_000_000;
        $display("TIMEOUT");
        $finish;
    end


endmodule
`default_nettype wire
