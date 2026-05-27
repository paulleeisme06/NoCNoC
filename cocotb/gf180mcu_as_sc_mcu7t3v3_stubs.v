// Behavioral stubs for GF180 standard cells missing from the functional
// verilog library.  Used only in mixed RTL/GL cocotb simulation.

// ao211_2: Y = (A & B) | C | D
module gf180mcu_as_sc_mcu7t3v3__ao211_2 (
    input  VPW, VNW, VDD, VSS,
    input  A, B, C, D,
    output Y
);
    assign Y = (A & B) | C | D;
endmodule

// aoi211_2: Y = ~((A & B) | C | D)
module gf180mcu_as_sc_mcu7t3v3__aoi211_2 (
    input  VPW, VNW, VDD, VSS,
    input  A, B, C, D,
    output Y
);
    assign Y = ~((A & B) | C | D);
endmodule

// oai211_2: Y = ~((A | B) & C & D)
module gf180mcu_as_sc_mcu7t3v3__oai211_2 (
    input  VPW, VNW, VDD, VSS,
    input  A, B, C, D,
    output Y
);
    assign Y = ~((A | B) & C & D);
endmodule
