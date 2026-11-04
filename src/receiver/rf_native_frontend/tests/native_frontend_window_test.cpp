// White-box invariants for the SFO evidence window.  This translation unit
// intentionally includes the implementation and is not linked to the static
// frontend library, avoiding duplicate definitions while testing the internal
// state transitions that have no production ABI surface.
#include "../src/mrf1_native_frontend.cpp"

#include <cstdlib>
#include <iostream>
#include <vector>

namespace {
#define CHECK(condition) do { \
  if (!(condition)) { \
    std::cerr << "CHECK failed: " #condition " at " << __FILE__ << ':' << __LINE__ << '\n'; \
    std::exit(EXIT_FAILURE); \
  } \
} while (false)

std::vector<uint8_t> unusable_rxth() {
  std::vector<uint8_t> bytes(MRF1_RXTH_BLOCK_BYTES, 0U);
  std::memcpy(bytes.data(), "RXTH", 4U);
  bytes[4] = 1U;
  bytes[5] = 32U;
  // flags=0 means not VALID while all framing fields remain well-formed.
  put32(bytes.data() + 8U, 3U);
  put32(bytes.data() + 12U, 7U);
  put64(bytes.data() + 16U, 7168U);
  put32(bytes.data() + 24U, MRF1_RXTH_BLOCK_SAMPLES);
  put32(bytes.data() + 28U, crc32(bytes.data(), 28U));
  return bytes;
}

void invalid_innovation_clears_window() {
  mrf1_nf_frontend state{};
  state.sfo_innovation_count = 15U;
  for (unsigned i = 0; i != state.sfo_innovation_count; ++i) state.sfo_innovation_window[i] = 0.03125;
  const double next = update_sfo_window(&state, false, false, false, 0.0, 12.0);
  CHECK(next == 12.0);
  CHECK(state.sfo_innovation_count == 0U);
  // A new acquisition likewise cannot complete a partial old window.
  state.sfo_innovation_count = 11U;
  CHECK(update_sfo_window(&state, true, false, true, 0.03125, -7.0) == -7.0);
  CHECK(state.sfo_innovation_count == 0U);
}

void loss_of_continuity_clears_window() {
  mrf1_nf_frontend *state = nullptr;
  CHECK(mrf1_nf_create(nullptr, &state) == MRF1_NF_OK);
  state->locked = true;
  state->sfo_innovation_count = 9U;
  const auto block = unusable_rxth();
  CHECK(append_block(state, block.data()) == MRF1_NF_REACQUIRING);
  CHECK(!state->locked);
  CHECK(state->sfo_innovation_count == 0U);
  mrf1_nf_destroy(state);
}
}  // namespace

int main() {
  invalid_innovation_clears_window();
  loss_of_continuity_clears_window();
  std::cout << "native_frontend_window_tests: pass\n";
}
