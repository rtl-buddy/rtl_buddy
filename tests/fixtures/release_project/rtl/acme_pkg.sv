// Package of private types: everything here is obfuscated and encrypted.
package acme_pkg;
  localparam int unsigned STEP_SCALE = 3;
  typedef logic [7:0] acme_word_t;
  function automatic acme_word_t scaled_step(input acme_word_t step);
    return step * STEP_SCALE;
  endfunction
endpackage
