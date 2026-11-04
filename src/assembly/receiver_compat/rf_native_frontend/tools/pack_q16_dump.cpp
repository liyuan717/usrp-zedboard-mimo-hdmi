#include "mrf1_native_frontend.h"

#include <array>
#include <iostream>

int main() {
  const double h[] = {1, 0, 0, 0, 0, 0, 1, 0};
  const double y[] = {1, 0, 0, 1};
  std::array<uint8_t, MRF1_TASK_BYTES> frame{};
  if (mrf1_nf_pack_q16_task(1, 2, 64, 1, 32768, h, y, frame.data()) != MRF1_NF_OK) return 1;
  std::cout.write(reinterpret_cast<const char *>(frame.data()), frame.size());
  return std::cout ? 0 : 2;
}
