#pragma once
#include <array>
#include <complex>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <vector>

// Receive-only LO leakage estimator on the first 64 clean blocks of the
// *current* ADC window. No packet IDs, expected payload, or file input.
struct CurrentCfoEstimate {
  std::array<double, 2> hz{}, coherence{};
  double common_hz{};
};
inline CurrentCfoEstimate estimate_current_cfo(
    const std::vector<std::array<std::complex<double>, 2>>& samples) {
  if (samples.size() != 65536) throw std::runtime_error("current CFO requires 65536 new samples");
  constexpr double fs = 4000000.0, pi = 3.14159265358979323846;
  CurrentCfoEstimate out;
  for (unsigned lane = 0; lane < 2; ++lane) {
    std::complex<double> mean{};
    for (const auto& p : samples) mean += p[lane];
    mean /= static_cast<double>(samples.size());
    double previous = 0;
    for (unsigned lag : {8U, 128U, 4096U}) {
      std::complex<double> cross{}; double ea = 0, eb = 0;
      for (std::size_t n = lag; n < samples.size(); ++n) {
        const auto a = samples[n - lag][lane] - mean, b = samples[n][lane] - mean;
        cross += std::conj(a) * b; ea += std::norm(a); eb += std::norm(b);
      }
      if (ea < 1e-8 || eb < 1e-8) throw std::runtime_error("current CFO has no receive energy");
      const double coherence = std::abs(cross) / std::sqrt(ea * eb);
      if (!std::isfinite(coherence) || coherence < .75)
        throw std::runtime_error("current CFO requires coherent receive tone on both lanes");
      double hz = std::arg(cross) * fs / (2 * pi * lag);
      if (lag != 8) hz += std::round((previous - hz) / (fs / lag)) * (fs / lag);
      if (lag == 4096 && std::abs(hz - previous) > 100)
        throw std::runtime_error("current CFO lag estimates disagree");
      previous = hz; out.coherence[lane] = coherence;
    }
    out.hz[lane] = previous;
  }
  if (std::abs(out.hz[0] - out.hz[1]) > 16)
    throw std::runtime_error("current CFO receive lanes disagree");
  out.common_hz = (out.hz[0] + out.hz[1]) / 2;
  if (!std::isfinite(out.common_hz) || std::abs(out.common_hz) > 100000)
    throw std::runtime_error("current CFO outside diagnostic range");
  return out;
}
