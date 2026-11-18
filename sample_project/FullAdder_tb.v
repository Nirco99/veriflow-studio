`timescale 1ns/1ns

// Exhaustive testbench for FullAdder: prints a truth table and self-checks every row.
module FullAdder_tb;

    reg  a, b, cin;
    wire sum, cout;
    integer i, errors;

    FullAdder uut (
        .sum  (sum),
        .cout (cout),
        .a    (a),
        .b    (b),
        .cin  (cin)
    );

    initial begin
        $dumpfile("FullAdder.vcd");
        $dumpvars(0, FullAdder_tb);
        $display("a | b | cin | sum | cout");
        $display("------------------------");
        errors = 0;
        for (i = 0; i < 8; i = i + 1) begin
            {a, b, cin} = i;
            #10;
            if ({cout, sum} !== a + b + cin) begin
                $display("FAIL: a=%b | b=%b | cin=%b | sum=%b | cout=%b", a, b, cin, sum, cout);
                errors = errors + 1;
            end else
                $display("a=%b | b=%b | cin=%b | sum=%b | cout=%b", a, b, cin, sum, cout);
        end
        if (errors == 0) $display("All 8 vectors passed.");
        else             $display("%0d vector(s) FAILED.", errors);
        #10 $finish;
    end

endmodule
