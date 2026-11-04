#pragma once
#include "current_window_cfo.h"
#include <algorithm>
constexpr std::size_t kMaximumFiniteCaptureBlocks = 5120;
struct FiniteCfoSelection {
  CurrentCfoEstimate estimate{};
  std::size_t window_start{};
  unsigned coherent_windows{};
  unsigned examined_windows{};
  double score{-1};
};
// Same bounded proposal policy that passed the recorded-IQ offline checks.
// It reads only the fresh, validated finite capture, never TX truth or files.
template<class FillWindow>
inline FiniteCfoSelection select_finite_capture_cfo_windows(
    std::size_t sample_count, FillWindow fill_window) {
  if (sample_count < 65536 || sample_count > kMaximumFiniteCaptureBlocks * 1024U)
    throw std::runtime_error("finite CFO sample bound invalid");
  FiniteCfoSelection result;
  std::vector<std::array<std::complex<double>,2>> window(65536);
  for (std::size_t at=0; at+window.size()<=sample_count; at+=16384) {
    ++result.examined_windows;
    fill_window(at, window);
    try {
      const auto estimate=estimate_current_cfo(window);
      ++result.coherent_windows;
      const double score=std::min(estimate.coherence[0],estimate.coherence[1]);
      if(score>result.score) {
        result.score=score;result.estimate=estimate;result.window_start=at;
      }
    } catch(const std::runtime_error&) {}
  }
  if(result.coherent_windows<2) throw std::runtime_error("finite CFO lacks two coherent windows");
  return result;
}

inline FiniteCfoSelection select_finite_capture_cfo(
    const std::vector<std::array<std::complex<double>,2>>& samples) {
  return select_finite_capture_cfo_windows(samples.size(), [&](std::size_t at, auto& window) {
    std::copy_n(samples.begin()+static_cast<std::ptrdiff_t>(at),window.size(),window.begin());
  });
}
