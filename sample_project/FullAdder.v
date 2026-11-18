`timescale 1ns/1ns

// 1-bit full adder, pure gate level (structural primitives only).
//   sum  = a ^ b ^ cin
//   cout = a&b | cin&(a^b)
module FullAdder (
    output sum,
    output cout,
    input  a,
    input  b,
    input  cin
);

    wire axb;      // a ^ b
    wire ab;       // a & b
    wire c_axb;    // cin & (a ^ b)

    xor g1 (axb, a, b);
    xor g2 (sum, axb, cin);
    and g3 (ab, a, b);
    and g4 (c_axb, axb, cin);
    or  g5 (cout, ab, c_axb);

endmodule
