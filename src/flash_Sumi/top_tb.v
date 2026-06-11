`default_nettype none
`timescale 1ns/1ps

module top_tb (
    input  wire clk,
    input  wire rst,
    input  wire host_csb,
    input  wire host_sclk,
    input  wire host_mosi,
    output wire host_miso
);
    wire flash_csb;
    wire flash_clk;
    wire flash_mosi;
    wire flash_miso;
    wire wp_neg      = 1'b1;
    wire io3_reset_n = 1'b1;

    top dut (
        .clk       (clk),
        .rst       (rst),
        .flash_csb (flash_csb),
        .flash_clk (flash_clk),
        .flash_mosi(flash_mosi),
        .flash_miso(flash_miso),
        .host_csb  (host_csb),
        .host_sclk (host_sclk),
        .host_mosi (host_mosi),
        .host_miso (host_miso)
    );

    s25fl128l flash_model (
        .SI          (flash_mosi),
        .SO          (flash_miso),
        .SCK         (flash_clk),
        .CSNeg       (flash_csb),
        .RESETNeg    (~rst),
        .WPNeg       (wp_neg),
        .IO3_RESETNeg(io3_reset_n)
    );

endmodule
`default_nettype wire
