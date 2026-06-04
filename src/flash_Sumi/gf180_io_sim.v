`default_nettype none
`timescale 1ns/1ps

module gf180mcu_fd_io__bi_24t (
    input  CS, SL, IE, OE, PU, PD,
    input  A,
    inout  PAD,
    output Y,
    input  DVDD, DVSS, VDD, VSS
);
    assign PAD = OE ? A : 1'bz;
    assign Y   = PAD;
endmodule

module gf180mcu_fd_io__in_c (
    input  PU, PD,
    inout  PAD,
    output Y,
    input  DVDD, DVSS, VDD, VSS
);
    assign Y = PAD;
endmodule

module gf180mcu_fd_io__in_s (
    input  PU, PD,
    inout  PAD,
    output Y,
    input  DVDD, DVSS, VDD, VSS
);
    assign Y = PAD;
endmodule

module gf180mcu_ws_io__dvdd (DVDD, DVSS, VSS);
    inout DVDD, DVSS, VSS;
endmodule

module gf180mcu_ws_io__dvss (DVDD, DVSS, VDD);
    inout DVDD, DVSS, VDD;
endmodule

module gf180mcu_fd_io__asig_5p0 (
    inout ASIG5V,
    input DVDD, DVSS, VDD, VSS
);
endmodule

module gf180mcu_ws_ip__id (); endmodule
module gf180mcu_ws_ip__logo (); endmodule

`default_nettype wire
