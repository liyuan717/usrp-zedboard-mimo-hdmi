#include "mrf1_native_frontend.h"
#include "repetition_bound.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <complex>
#if defined(MRF1_NF_ENABLE_PROFILE)
#include <chrono>
#endif
#include <cstring>
#include <cstdlib>
#include <limits>
#include <new>
#include <utility>
#include <vector>

using MRF1NativeComplex = std::complex<double>;

struct MRF1NativeHeader {
  uint32_t epoch = 0, sequence = 0, flags = 0, count = 0;
  uint64_t first = 0;
};

struct MRF1NativeChannel {
  std::array<MRF1NativeComplex, 4> v{};
  MRF1NativeComplex &at(unsigned r, unsigned c) { return v[r * 2U + c]; }
  const MRF1NativeComplex &at(unsigned r, unsigned c) const { return v[r * 2U + c]; }
};

// The ABI intentionally exposes only an incomplete type in the public header.
// Its concrete definition must have global linkage to match that declaration.
struct mrf1_nf_frontend {
  mrf1_nf_config cfg{};
  std::vector<uint8_t> pending;
  std::vector<std::array<MRF1NativeComplex, 2>> samples;
  std::vector<std::array<MRF1NativeComplex, 2>> unfiltered_samples;
  std::vector<MRF1NativeComplex> taps;
  std::vector<MRF1NativeComplex> preamble_template;
  std::vector<MRF1NativeComplex> training;
  std::vector<MRF1NativeComplex> pilots;
  std::array<uint8_t, 40> headers{};
  std::vector<uint8_t> tasks;
  // Allocated once at creation.  Tracking never grows these vectors or creates
  // per-symbol objects; contents are overwritten frame by frame.
  std::vector<std::array<MRF1NativeComplex, 2>> raw_work;
  std::vector<std::array<MRF1NativeComplex, 2>> matched_work;
#ifdef MRF1_NF_ENABLE_DIAG_DC_BLOCKER
  std::array<MRF1NativeComplex, 2> dc_previous_input{}, dc_previous_output{};
  MRF1NativeComplex coarse_phase{1.0,0.0}, coarse_step{1.0,0.0};
#endif
  // Keep disabled-prior defaults available in ordinary builds as well.
  bool coarse_prior=false, cadence_prior=false;
  double cadence_ppm=0.0;
  MRF1NativeHeader previous{};
  bool has_previous = false, gap = false, poisoned = false, locked = false, emitted = false;
  uint64_t sample_end = 0, buffer_first_sample = 0;
  uint64_t nco_reference_sample = 0;
  uint32_t buffer_first_sequence = 0;
  bool has_nco_reference = false;
  mrf1_nf_stats stats{};
  mrf1_nf_profile profile{};
  mrf1_nf_provenance provenance{};
  MRF1NativeChannel channel{};
  MRF1NativeChannel training_channel{};
  std::array<MRF1NativeComplex, 2> first_detector_y{};
  double first_pilot_phase = 0.0;
  double cfo_hz = 0.0, timing_offset = 0.0, sfo_ppm = 0.0, next_origin = 0.0;
  // The bounded cold acquisition establishes the local ambiguity cell for a
  // lock.  Pilot feedback may refine inside that cell but must not integrate
  // rare noisy pilot decisions into an unbounded timing/CFO/SFO walk.
  double cfo_anchor_hz = 0.0, timing_anchor = 0.0, sfo_anchor_ppm = 0.0;
  // Non-overlapping known-training timing innovations.  A completed window
  // may correct SFO once; a sample is never reused by a rolling controller.
  std::array<double, 16> sfo_innovation_window{};
  unsigned sfo_innovation_count = 0;
};

namespace {
using C = MRF1NativeComplex;
using Header = MRF1NativeHeader;
using Channel = MRF1NativeChannel;
constexpr uint32_t kRxthValid = 1U;
constexpr uint32_t kRxthResetEpoch = 1U << 4U;
constexpr uint32_t kRxthKnownFlags = 0x1ffU;
constexpr uint32_t kInputMagic = 0x3050494dU;  // MIP0
constexpr uint16_t kStreamVersion = 1;
constexpr uint16_t kQpsk = 1;
constexpr uint16_t kDetectorMhdg = 3;
constexpr uint16_t kInputBeats = 15;
constexpr uint16_t kOutputBeats = 8;
constexpr uint32_t kSamplesPerSymbol = 4;
constexpr uint32_t kRrcSpan = 10;
constexpr uint32_t kRrcTaps = kRrcSpan * kSamplesPerSymbol + 1;
constexpr uint32_t kGuard = 32;
constexpr uint32_t kSeparator = 12;
constexpr uint32_t kTraining = 64;
constexpr uint32_t kPreambleBlock = 32;
constexpr uint32_t kHeaderSymbols = 80;
constexpr uint32_t kPayloadSymbols = 528;
constexpr uint32_t kPilotInterval = 64;
constexpr uint32_t kFrameSamples = 3840;
constexpr double kPi = 3.141592653589793238462643383279502884;
constexpr double kInvSqrt2 = 0.707106781186547524400844362104849039;

uint16_t le16(const uint8_t *p) { return uint16_t(p[0]) | (uint16_t(p[1]) << 8U); }
uint32_t le32(const uint8_t *p) {
  return uint32_t(p[0]) | (uint32_t(p[1]) << 8U) | (uint32_t(p[2]) << 16U) |
         (uint32_t(p[3]) << 24U);
}
uint64_t le64(const uint8_t *p) {
  uint64_t v = 0;
  for (unsigned i = 0; i != 8; ++i) v |= uint64_t(p[i]) << (8U * i);
  return v;
}
void put16(uint8_t *p, uint16_t v) { p[0] = uint8_t(v); p[1] = uint8_t(v >> 8U); }
void put32(uint8_t *p, uint32_t v) {
  for (unsigned i = 0; i != 4; ++i) p[i] = uint8_t(v >> (8U * i));
}
void put64(uint8_t *p, uint64_t v) {
  for (unsigned i = 0; i != 8; ++i) p[i] = uint8_t(v >> (8U * i));
}

uint32_t crc32(const uint8_t *data, size_t n) {
  uint32_t c = 0xffffffffU;
  for (size_t i = 0; i != n; ++i) {
    c ^= data[i];
    for (unsigned b = 0; b != 8; ++b) c = (c >> 1U) ^ ((c & 1U) ? 0xedb88320U : 0U);
  }
  return c ^ 0xffffffffU;
}

// Small local SHA-256 implementation. It is only used for frozen public known
// words; no payload, seed, or transmitter-private input enters the receiver.
constexpr std::array<uint32_t, 64> kShaK = {
  0x428a2f98U,0x71374491U,0xb5c0fbcfU,0xe9b5dba5U,0x3956c25bU,0x59f111f1U,0x923f82a4U,0xab1c5ed5U,
  0xd807aa98U,0x12835b01U,0x243185beU,0x550c7dc3U,0x72be5d74U,0x80deb1feU,0x9bdc06a7U,0xc19bf174U,
  0xe49b69c1U,0xefbe4786U,0x0fc19dc6U,0x240ca1ccU,0x2de92c6fU,0x4a7484aaU,0x5cb0a9dcU,0x76f988daU,
  0x983e5152U,0xa831c66dU,0xb00327c8U,0xbf597fc7U,0xc6e00bf3U,0xd5a79147U,0x06ca6351U,0x14292967U,
  0x27b70a85U,0x2e1b2138U,0x4d2c6dfcU,0x53380d13U,0x650a7354U,0x766a0abbU,0x81c2c92eU,0x92722c85U,
  0xa2bfe8a1U,0xa81a664bU,0xc24b8b70U,0xc76c51a3U,0xd192e819U,0xd6990624U,0xf40e3585U,0x106aa070U,
  0x19a4c116U,0x1e376c08U,0x2748774cU,0x34b0bcb5U,0x391c0cb3U,0x4ed8aa4aU,0x5b9cca4fU,0x682e6ff3U,
  0x748f82eeU,0x78a5636fU,0x84c87814U,0x8cc70208U,0x90befffaU,0xa4506cebU,0xbef9a3f7U,0xc67178f2U};
uint32_t ror(uint32_t x, unsigned n) { return (x >> n) | (x << (32U - n)); }
std::array<uint8_t, 32> sha256(const std::vector<uint8_t> &input) {
  std::vector<uint8_t> data = input;
  const uint64_t bits = uint64_t(data.size()) * 8U;
  data.push_back(0x80U);
  while ((data.size() % 64U) != 56U) data.push_back(0U);
  for (int i = 7; i >= 0; --i) data.push_back(uint8_t(bits >> (8U * unsigned(i))));
  std::array<uint32_t, 8> h = {0x6a09e667U,0xbb67ae85U,0x3c6ef372U,0xa54ff53aU,
                                0x510e527fU,0x9b05688cU,0x1f83d9abU,0x5be0cd19U};
  for (size_t off = 0; off != data.size(); off += 64U) {
    std::array<uint32_t, 64> w{};
    for (unsigned i = 0; i != 16; ++i)
      w[i] = (uint32_t(data[off + 4U*i]) << 24U) | (uint32_t(data[off + 4U*i+1U]) << 16U) |
             (uint32_t(data[off + 4U*i+2U]) << 8U) | data[off + 4U*i+3U];
    for (unsigned i = 16; i != 64; ++i) {
      const uint32_t s0 = ror(w[i-15],7) ^ ror(w[i-15],18) ^ (w[i-15] >> 3U);
      const uint32_t s1 = ror(w[i-2],17) ^ ror(w[i-2],19) ^ (w[i-2] >> 10U);
      w[i] = w[i-16] + s0 + w[i-7] + s1;
    }
    uint32_t a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],hh=h[7];
    for (unsigned i=0;i!=64;++i) {
      const uint32_t s1=ror(e,6)^ror(e,11)^ror(e,25);
      const uint32_t ch=(e&f)^((~e)&g);
      const uint32_t t1=hh+s1+ch+kShaK[i]+w[i];
      const uint32_t s0=ror(a,2)^ror(a,13)^ror(a,22);
      const uint32_t maj=(a&b)^(a&c)^(b&c);
      const uint32_t t2=s0+maj;
      hh=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
    }
    h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=hh;
  }
  std::array<uint8_t,32> out{};
  for (unsigned i=0;i!=8;++i) for (unsigned b=0;b!=4;++b) out[4U*i+b]=uint8_t(h[i] >> (24U-8U*b));
  return out;
}

std::vector<uint8_t> deterministic_bytes(const char *domain, size_t count) {
  std::vector<uint8_t> out;
  for (uint32_t counter=0; out.size()<count; ++counter) {
    std::vector<uint8_t> message;
    for (const char *p=domain; *p!='\0'; ++p) message.push_back(uint8_t(*p));
    message.push_back(0);
    // seed=frame=stream=0, then little-endian counter (the frozen Python contract).
    for (unsigned i=0;i!=16;++i) message.push_back(uint8_t(counter >> (8U*(i%4U))));
    const auto digest=sha256(message);
    out.insert(out.end(),digest.begin(),digest.end());
  }
  out.resize(count);
  return out;
}

C qpsk(uint8_t index) {
  switch (index & 3U) {
    case 0: return C(kInvSqrt2,kInvSqrt2);
    case 1: return C(-kInvSqrt2,kInvSqrt2);
    case 2: return C(-kInvSqrt2,-kInvSqrt2);
    default: return C(kInvSqrt2,-kInvSqrt2);
  }
}
std::vector<C> known_symbols(const char *domain, size_t count) {
  const auto bytes=deterministic_bytes(domain,(count+3U)/4U);
  std::vector<C> result; result.reserve(count);
  for (uint8_t byte:bytes) for (int shift=6;shift>=0 && result.size()<count;shift-=2) result.push_back(qpsk(uint8_t(byte >> unsigned(shift))));
  return result;
}
std::vector<C> rrc_taps() {
  std::vector<C> taps; taps.reserve(kRrcTaps);
  double norm=0.0;
  for (int n=-20;n<=20;++n) {
    const double t=double(n)/double(kSamplesPerSymbol), beta=0.35;
    double v;
    if (std::abs(t)<1e-12) v=1.0+beta*(4.0/kPi-1.0);
    else if (std::abs(std::abs(t)-1.0/(4.0*beta))<1e-12) {
      v=beta/std::sqrt(2.0)*((1.0+2.0/kPi)*std::sin(kPi/(4.0*beta))+(1.0-2.0/kPi)*std::cos(kPi/(4.0*beta)));
    } else v=(std::sin(kPi*t*(1.0-beta))+4.0*beta*t*std::cos(kPi*t*(1.0+beta)))/(kPi*t*(1.0-std::pow(4.0*beta*t,2)));
    taps.emplace_back(v,0.0); norm+=v*v;
  }
  norm=std::sqrt(norm); for (C &v:taps) v/=norm; return taps;
}
std::vector<C> pulse_shape(const std::vector<C> &symbols, const std::vector<C> &taps) {
  std::vector<C> out(symbols.size()*kSamplesPerSymbol+taps.size()-1U,C{});
  for (size_t s=0;s!=symbols.size();++s) for (size_t k=0;k!=taps.size();++k) out[s*kSamplesPerSymbol+k]+=symbols[s]*taps[k];
  return out;
}
double abs2(C v) { return std::norm(v); }

Channel inv2(const Channel &a, bool *ok) {
  const C det=a.at(0,0)*a.at(1,1)-a.at(0,1)*a.at(1,0);
  if (std::abs(det)<1e-18) { *ok=false; return {}; }
  *ok=true; Channel b; b.at(0,0)=a.at(1,1)/det;b.at(0,1)=-a.at(0,1)/det;b.at(1,0)=-a.at(1,0)/det;b.at(1,1)=a.at(0,0)/det;return b;
}
double condition2(const Channel &h) {
  bool ok=false;const Channel inv=inv2(h,&ok);if(!ok)return std::numeric_limits<double>::infinity();
  double a=0,b=0;for(C v:h.v)a+=abs2(v);for(C v:inv.v)b+=abs2(v);return std::sqrt(a*b);
}
void mul(const Channel &h, const C *x, C *y) { for(unsigned r=0;r!=2;++r)y[r]=h.at(r,0)*x[0]+h.at(r,1)*x[1]; }
void put_i32(uint8_t *p,int32_t v) { put32(p,static_cast<uint32_t>(v)); }
void put_i64(uint8_t *p,int64_t v) { put64(p,static_cast<uint64_t>(v)); }
bool q16(double v, int bits, int64_t *out) {
  const double scaled=v*65536.0;
  if(!std::isfinite(scaled))return false;
  const double r=std::nearbyint(scaled);
  const double lo=bits==32?double(std::numeric_limits<int32_t>::min()):double(std::numeric_limits<int64_t>::min());
  const double hi=bits==32?double(std::numeric_limits<int32_t>::max()):double(std::numeric_limits<int64_t>::max());
  if (r < lo || r > hi) return false;
  *out = static_cast<int64_t>(r);
  return true;
}

bool decode_rxth(const uint8_t *raw, Header *h, const uint8_t **iq) {
  if(std::memcmp(raw,"RXTH",4)!=0 || raw[4]!=1 || raw[5]!=32)return false;
  h->flags=le16(raw+6);h->epoch=le32(raw+8);h->sequence=le32(raw+12);h->first=le64(raw+16);h->count=le32(raw+24);
  if((h->flags & ~kRxthKnownFlags)!=0U || h->count>MRF1_RXTH_BLOCK_SAMPLES || crc32(raw,28)!=le32(raw+28))return false;
  *iq=raw+MRF1_RXTH_HEADER_BYTES;return true;
}

// Keep the SFO estimator's evidence strictly scoped to one successful lock.
// This is deliberately a small, testable state transition: callers must pass
// a valid convex known-training innovation, while a new acquisition, bounded
// recovery, or ambiguity in the training curve discards partial evidence.
double update_sfo_window(mrf1_nf_frontend *f, bool initial,
                         bool recovered_training, bool innovation_valid,
                         double training_innovation, double entry_sfo_ppm) {
  if (initial || recovered_training || !innovation_valid) {
    f->sfo_innovation_count = 0;
    return entry_sfo_ppm;
  }
  f->sfo_innovation_window[f->sfo_innovation_count++] = training_innovation;
  if (f->sfo_innovation_count != f->sfo_innovation_window.size()) return entry_sfo_ppm;
  double mean = 0.0;
  for (double value : f->sfo_innovation_window) mean += value;
  mean /= double(f->sfo_innovation_window.size());
  double variance = 0.0;
  for (double value : f->sfo_innovation_window) variance += (value - mean) * (value - mean);
  variance /= double(f->sfo_innovation_window.size() - 1U);
  const double standard_error = std::sqrt(variance / double(f->sfo_innovation_window.size()));
  // A non-overlapping 3-sigma mean gate separates persistent clock-rate
  // evidence from zero-mean timing measurement noise.  The correction sign
  // follows qref coordinates: positive late samples imply positive SFO.
  double next_sfo_ppm = entry_sfo_ppm;
  if (std::abs(mean) > 3.0 * standard_error && std::isfinite(standard_error)) {
    next_sfo_ppm = std::max(f->sfo_anchor_ppm - 200.0,
        std::min(f->sfo_anchor_ppm + 200.0,
            entry_sfo_ppm + 1e6 * mean / double(kFrameSamples)));
  }
  // Always clear after a completed non-overlapping window, even if rejected.
  f->sfo_innovation_count = 0;
  return next_sfo_ppm;
}

mrf1_nf_status append_block(mrf1_nf_frontend *f,const uint8_t *raw) {
  Header h{};const uint8_t *iq=nullptr;if(!decode_rxth(raw,&h,&iq))return MRF1_NF_TRANSPORT;
  if(f->has_previous) {
    if(h.epoch<f->previous.epoch || (h.epoch==f->previous.epoch && h.sequence<=f->previous.sequence))return MRF1_NF_TRANSPORT;
    if(h.epoch==f->previous.epoch && (h.flags&kRxthResetEpoch)!=0U)return MRF1_NF_TRANSPORT;
  }
  const bool usable=(h.flags&kRxthValid)!=0U && (h.flags & (kRxthKnownFlags & ~(kRxthValid|kRxthResetEpoch)))==0U;
  const bool contig=f->has_previous && h.epoch==f->previous.epoch && h.sequence==f->previous.sequence+1U && h.first==f->sample_end && (h.flags&kRxthResetEpoch)==0U && !f->gap;
  f->stats.rxth_blocks++;
  if(!usable || h.count!=MRF1_RXTH_BLOCK_SAMPLES || h.first+h.count<h.first) {
    f->stats.rejected_blocks++; f->gap=true; f->locked=false; f->sfo_innovation_count=0; f->samples.clear();f->unfiltered_samples.clear(); f->has_previous=false; f->stats.continuity_resets++; return MRF1_NF_REACQUIRING;
  }
  if(f->has_previous && !contig) { f->samples.clear();f->unfiltered_samples.clear();f->locked=false;f->sfo_innovation_count=0;f->gap=true;f->stats.continuity_resets++; }
  if(f->samples.empty()) { f->buffer_first_sample=h.first; f->buffer_first_sequence=h.sequence; }
#ifdef MRF1_NF_ENABLE_DIAG_DC_BLOCKER
  // Fixed first-order pole: about 3.2 kHz at the frozen 4 MS/s. No payload,
  // CRC or transmitter reference controls this receive-only preprocessing.
  if (!contig) { f->dc_previous_input = {}; f->dc_previous_output = {}; f->coarse_phase={1.0,0.0}; }
#endif
  for(size_t i=0;i<MRF1_RXTH_BLOCK_SAMPLES;++i) {
    const int16_t i0=static_cast<int16_t>(le16(iq+8U*i));const int16_t q0=static_cast<int16_t>(le16(iq+8U*i+2U));
    const int16_t i1=static_cast<int16_t>(le16(iq+8U*i+4U));const int16_t q1=static_cast<int16_t>(le16(iq+8U*i+6U));
    if((uint16_t(i0)&15U)||(uint16_t(q0)&15U)||(uint16_t(i1)&15U)||(uint16_t(q1)&15U))return MRF1_NF_TRANSPORT;
    std::array<C,2> sample{C(double(i0)/32768.0,double(q0)/32768.0),C(double(i1)/32768.0,double(q1)/32768.0)};
#ifdef MRF1_NF_ENABLE_DIAG_DC_BLOCKER
    for(C &v:sample) v*=f->coarse_phase;
    f->coarse_phase*=f->coarse_step;
    if((i&255U)==255U) f->coarse_phase/=std::abs(f->coarse_phase);
#endif
    f->unfiltered_samples.push_back(sample);
#ifdef MRF1_NF_ENABLE_DIAG_DC_BLOCKER
    for (unsigned r=0; r!=2; ++r) {
      const C filtered=sample[r]-f->dc_previous_input[r]+0.995*f->dc_previous_output[r];
      f->dc_previous_input[r]=sample[r]; f->dc_previous_output[r]=filtered; sample[r]=filtered;
    }
#endif
    f->samples.push_back(sample);
  }
  f->previous=h;f->has_previous=true;f->sample_end=h.first+h.count;f->gap=false;f->stats.current_epoch=h.epoch;
  if(f->samples.size()>f->cfg.max_buffer_samples) { const size_t trimmed=f->samples.size()-f->cfg.max_buffer_samples; f->samples.erase(f->samples.begin(),f->samples.begin()+ptrdiff_t(trimmed));f->unfiltered_samples.erase(f->unfiltered_samples.begin(),f->unfiltered_samples.begin()+ptrdiff_t(trimmed)); f->buffer_first_sample+=trimmed; f->locked=false;f->sfo_innovation_count=0;f->stats.continuity_resets++; }
  f->stats.buffered_samples=f->samples.size();return MRF1_NF_OK;
}

// Return best public preamble start and a bounded common-CFO estimate.  This
// searches receive IQ only; the header/payload is never examined here.
bool acquire(mrf1_nf_frontend *f, size_t *best_start, double *omega) {
  const size_t n=f->samples.size(), templ=f->preamble_template.size(); if(n<templ)return false;
  const size_t lag=kPreambleBlock*kSamplesPerSymbol; const size_t width=lag-(kRrcTaps-1U); if(width==0||n<templ+lag)return false;
  double best=-1.0;size_t at=0;double best_w=0.0;
  // This is invariant for all start/CFO candidates.  Keeping the exact
  // accumulation order but moving it out of q removes 33 needless passes.
  double template_energy=0.0;
  for (C sample : f->preamble_template) template_energy += abs2(sample);
  const double limit=std::nextafter(kPi/double(lag),0.0);
  const double fine_step=limit/8192.0;
  // A coarse prior fixes the residual grid at the same 33 frequencies for
  // every candidate position. Build exactly the original basis recurrence
  // once per acquire call; header gates and the search order stay unchanged.
  // With no coarse prior, the receive-derived grid is position dependent and
  // uses the original path below. Extra bounded storage is 33 * templ complex.
  std::array<std::vector<C>,33> cached_basis;
  if (f->coarse_prior) {
    for (unsigned q=0;q!=33;++q) {
      const double w=std::max(-limit,std::min(limit,(double(q)-16.0)*fine_step));
      cached_basis[q].resize(templ);
      #if defined(MRF1_NF_FORCE_DIRECT_ACQUISITION_OSC)
      for (size_t k=0;k<templ;++k)
        cached_basis[q][k]=std::conj(f->preamble_template[k])*std::exp(C(0,-w*double(k)));
      #else
      C oscillator{1.0,0.0};
      const C step=std::exp(C(0,-w));
      for (size_t k=0;k<templ;++k) {
        cached_basis[q][k]=std::conj(f->preamble_template[k])*oscillator;
        oscillator*=step;
        if ((k&255U)==255U) oscillator/=std::abs(oscillator);
      }
      #endif
    }
  }
  double max_component=0;
  for(const auto& sample:f->samples) for(const C z:sample)
    max_component=std::max(max_component,std::max(std::abs(z.real()),std::abs(z.imag())));
  // The original sum has 2*width terms, each component bounded by 2*M*M.
  // 64*eps*width^2*max(1,M*M) conservatively exceeds its sequential-add
  // rounding bound (including component arithmetic), for width=96 and the
  // normalized finite ADC inputs. Rolling update error is enclosed separately
  // by outward rounding at EVERY update, even after long quiet tails.
  const double direct_rounding_bound=64.0*std::numeric_limits<double>::epsilon()*
      double(width)*double(width)*std::max(1.0,max_component*max_component);
  repetition_bound::Window repeated;
  const auto update_repeated=[&](size_t k,double sign) {
    for(unsigned r=0;r!=2;++r) {
      const C a=f->samples[k][r],b=f->samples[k+lag][r];
      const C product=std::conj(a)*b;
      repeated.add(sign*product.real(),sign*product.imag(),sign*abs2(a),sign*abs2(b));
    }
  };
  const size_t first=kRrcTaps-1U;
  for(size_t k=0;k<width;++k) update_repeated(first+k,1.0);
  for(size_t s=0;s+templ<=n;++s) {
    if(s!=0) {
      update_repeated(first+s-1U,-1.0);
      update_repeated(first+s+width-1U,1.0);
    }
    if(repeated.upper(direct_rounding_bound)<f->cfg.repetition_threshold)continue;
    // Possible passes, all ties and numerically uncertain cases take the
    // exact original summation and CFO estimate. No threshold is relaxed.
    C prod{};double e1=0,e2=0;
    for(size_t k=0;k<width;++k) for(unsigned r=0;r!=2;++r) { const C a=f->samples[s+(kRrcTaps-1U)+k][r],b=f->samples[s+(kRrcTaps-1U)+k+lag][r];prod+=std::conj(a)*b;e1+=abs2(a);e2+=abs2(b); }
    const double rep=abs2(prod)/std::max(e1*e2,1e-30);if(rep<f->cfg.repetition_threshold)continue;
    const double initial=f->coarse_prior?0.0:std::arg(prod)/double(lag);
    #if defined(MRF1_NF_ENABLE_PROFILE)
    ++f->profile.repetition_candidates;
    #endif
    // The repeated preamble directly supplies a CFO estimate.  Refining around
    // it preserves sub-grid accuracy: scanning the whole unambiguous interval
    // with only 33 points previously snapped a +500 Hz signal to +976.56 Hz,
    // leaving enough residual phase to lose lock several frames later.
    // The search remains bounded by the repeated-block ambiguity interval.
    // 33 bins at limit/8192 correspond to about 1.9 Hz/bin at 4 MS/s,
    // while still covering +/-30 Hz around the repeated-preamble estimate.
    // The older ~30 Hz bins left a high-CFO residual that accumulated across
    // the long header and made modest AWGN tracking needlessly fragile.
    double energy=0;
    // Identical accumulation order to the old loop, independent of q.
    for(size_t k=0;k<templ;++k)
      for(unsigned r=0;r!=2;++r) energy+=abs2(f->samples[s+k][r]);
    for(unsigned q=0;q!=33;++q) {
      const double requested=initial+(double(q)-16.0)*fine_step;
      const double w=std::max(-limit,std::min(limit,requested)); C corr[2]{};
      #if defined(MRF1_NF_ENABLE_PROFILE)
      ++f->profile.preamble_refinement_candidates;
      #endif
      if (f->coarse_prior) {
        for(size_t k=0;k<templ;++k)
          for(unsigned r=0;r!=2;++r) corr[r]+=cached_basis[q][k]*f->samples[s+k][r];
      } else {
      #if defined(MRF1_NF_FORCE_DIRECT_ACQUISITION_OSC)
      for(size_t k=0;k<templ;++k) {
        const C basis=std::conj(f->preamble_template[k])*std::exp(C(0,-w*double(k)));
        for(unsigned r=0;r!=2;++r) corr[r]+=basis*f->samples[s+k][r];
      }
      #else
      C oscillator{1.0, 0.0};
      const C oscillator_step=std::exp(C(0,-w));
      for(size_t k=0;k<templ;++k) {
        const C basis=std::conj(f->preamble_template[k])*oscillator;
        for(unsigned r=0;r!=2;++r) corr[r]+=basis*f->samples[s+k][r];
        oscillator *= oscillator_step;
        if ((k & 255U) == 255U) oscillator /= std::abs(oscillator);
      }
      #endif
      }
      const double corrected=(abs2(corr[0])+abs2(corr[1]))/std::max(energy*template_energy,1e-30);
      if(corrected>best) {best=corrected;at=s;best_w=w;}
    }
  }
  if (best < f->cfg.preamble_threshold) return false;
  *best_start = at;
  *omega = best_w;
  return true;
}

bool sample_linear(const std::vector<std::array<C,2>> &v,double x,std::array<C,2> *out) {
  if (x < 0.0) return false;
  const size_t a = size_t(std::floor(x));
  if (a + 1U >= v.size()) return false;
  const double t = x - double(a);
  for (unsigned r = 0; r != 2; ++r) (*out)[r] = v[a][r] * (1.0 - t) + v[a + 1U][r] * t;
  return true;
}
bool packet_header(const uint8_t *h,unsigned stream,uint32_t *frame) {
  if (crc32(h,16)!=le32(h+16)||std::memcmp(h,"MRF1",4)!=0||h[4]!=1||h[5]!=stream||h[6]!=1||h[7]!=0||le32(h+12)!=128U) return false;
  *frame = le32(h + 8);
  return true;
}

bool build_batch(mrf1_nf_frontend *f,double frame_origin,double omega,bool initial,
                 uint8_t *header_output, uint8_t *task_output) {
  // Do all baseband operations in a frame-local work buffer.  The outer DMA
  // ring remains bounded and no symbol is converted into a Python object.
  if (frame_origin < 0.0) return false;
  const size_t raw_start = size_t(std::floor(frame_origin));
  if (raw_start + kFrameSamples > f->samples.size()) return false;
  if (initial || !f->has_nco_reference) {
    // Keep a physical, non-compacting NCO reference.  Buffer consumption is
    // allowed after this frame, so a local vector index is not phase-stable.
    f->nco_reference_sample = f->buffer_first_sample;
    f->has_nco_reference = true;
  }
  #if defined(MRF1_NF_ENABLE_PROFILE)
  const auto nco_begin = std::chrono::steady_clock::now();
  #endif
  auto &raw = f->raw_work;
  auto &mf = f->matched_work;
  std::fill(raw.begin(), raw.end(), std::array<C, 2>{});
  // Locked-path CFO removal is a complex recurrence, not 3,840 libm
  // exp()/sin()/cos() calls per frame.  Double precision is retained; a
  // deterministic periodic normalisation bounds accumulated magnitude error.
  // `MRF1_NF_FORCE_DIRECT_NCO` remains a regression-only exact formulation
  // for comparing task CRCs, never a production default.
  const uint64_t first_absolute = f->buffer_first_sample + raw_start;
  #if defined(MRF1_NF_FORCE_DIRECT_NCO)
  for(size_t i=0;i<kFrameSamples;++i) {
    const uint64_t absolute = first_absolute + i;
    const double ph = -omega * double(absolute - f->nco_reference_sample);
    for(unsigned r=0;r!=2;++r) raw[i][r]=f->unfiltered_samples[raw_start+i][r]*std::exp(C(0,ph));
  }
  #else
  C rotation = std::exp(C(0, -omega * double(first_absolute - f->nco_reference_sample)));
  const C rotation_step = std::exp(C(0, -omega));
  for(size_t i=0;i<kFrameSamples;++i) {
    for(unsigned r=0;r!=2;++r) raw[i][r]=f->unfiltered_samples[raw_start+i][r]*rotation;
    rotation *= rotation_step;
    if ((i & 255U) == 255U) rotation /= std::abs(rotation);
  }
  #endif
  // Acquisition alone uses the high-pass copy. Its baseline wander must not
  // distort the long biased public header or the first payload interval.
  // Estimate additive carrier/DC only from standard zero guard interiors,
  // after frame-local NCO, and interpolate the complex baseline across the
  // untouched receive frame. Neither payload decisions nor TX bytes enter.
  std::array<C,2> guard_left{},guard_right{};
  for(size_t i=32U;i!=96U;++i) for(unsigned rx=0;rx!=2;++rx) {
    guard_left[rx]+=raw[i][rx]/64.0;
    guard_right[rx]+=raw[kFrameSamples-128U+i][rx]/64.0;
  }
  for(size_t i=0;i!=kFrameSamples;++i) {
    const double weight=(double(i)-63.5)/double(kFrameSamples-128U);
    for(unsigned rx=0;rx!=2;++rx) raw[i][rx]-=guard_left[rx]+weight*(guard_right[rx]-guard_left[rx]);
  }
  // Power-of-two baseband gain before Gram quantization improves Q16
  // precision. H and y receive the same gain: the continuous ML objective
  // is multiplied by1024; ADC IQ, constellation, payload and CRC are unchanged.
  for(auto &sample:raw) for(C &v:sample) v*=32.0;
  #if defined(MRF1_NF_ENABLE_PROFILE)
  f->profile.nco_ns += uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now() - nco_begin).count());
  const auto rrc_begin = std::chrono::steady_clock::now();
  #endif
  #if defined(MRF1_NF_FORCE_DENSE_TRACKING)
  constexpr bool kDenseTracking = true;
  #else
  constexpr bool kDenseTracking = false;
  #endif
  if (initial || kDenseTracking) {
    // Initial acquisition pays one dense RRC evaluation because its bounded
    // timing/SFO grid probes a wide set of coordinates.  Locked tracking uses
    // the sparse equivalent below, never this whole-frame convolution.
    std::fill(mf.begin(), mf.end(), std::array<C, 2>{});
    for(size_t n=0;n!=raw.size();++n)for(size_t k=0;k!=f->taps.size();++k)for(unsigned r=0;r!=2;++r)mf[n+k][r]+=raw[n][r]*f->taps[k];
  }
  #if defined(MRF1_NF_ENABLE_PROFILE)
  f->profile.rrc_ns += uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now() - rrc_begin).count());
  const auto training_header_begin = std::chrono::steady_clock::now();
  #endif
  const unsigned first=kGuard+3U*kPreambleBlock+kSeparator; const unsigned second=first+kTraining+kSeparator; const unsigned header_start=second+kTraining+kSeparator;
  const auto get=[&](double symbol,double off,double ppm,std::array<C,2>*out)->bool {
    const double ref=(double((first+4U)+(first+kTraining-5U)+(second+4U)+(second+kTraining-5U))/4.0)*4.0;
    // `raw` begins at floor(frame_origin); retain the fractional remainder
    // across tracked frames instead of silently snapping it at each boundary.
    const double pos=(frame_origin-double(raw_start))+double(kRrcTaps-1U)+symbol*4.0+off+ppm*1e-6*(symbol*4.0-ref);
    if (initial || kDenseTracking) return sample_linear(mf,pos,out);
    // Sparse exact-integer RRC convolution plus the same linear fractional
    // interpolation as the dense reference.  `pos` is always after the RRC
    // startup transient for training/header/pilot/payload observations.
    if (pos < double(kRrcTaps-1U)) return false;
    const size_t at = size_t(std::floor(pos));
    if (at + 1U >= raw.size() + kRrcTaps-1U) return false;
    const double fraction = pos-double(at);
    for (unsigned r=0;r!=2;++r) {
      C a{}, b{};
      for (size_t tap=0;tap<f->taps.size();++tap) {
        const size_t ia=at-tap;
        const size_t ib=at+1U-tap;
        if (ia<raw.size()) a+=raw[ia][r]*f->taps[tap];
        if (ib<raw.size()) b+=raw[ib][r]*f->taps[tap];
      }
      (*out)[r]=a*(1.0-fraction)+b*fraction;
    }
    return true;
  };
  double best_cost=std::numeric_limits<double>::infinity(),best_off=initial ? 0.0 : f->timing_offset,best_ppm=initial ? (f->cadence_prior?f->cadence_ppm:0.0) : f->sfo_ppm;Channel best_h{};
  const double timing_prediction = best_off;
  std::array<double, 3> locked_training_costs{
      std::numeric_limits<double>::infinity(), std::numeric_limits<double>::infinity(),
      std::numeric_limits<double>::infinity()};
  Channel locked_training_center{};
  double locked_training_center_cost = std::numeric_limits<double>::infinity();
  double training_innovation = 0.0;
  bool training_innovation_valid = false;
  bool recovered_training = false;
  // The full 9x17 bounded timing/SFO search is acquisition-only.  After
  // lock, known training is sampled once at the carried loop state; pilots
  // below update phase/channel.  This is deliberately not a per-frame grid.
  const int ppm_first = initial ? (f->cadence_prior?0:-4) : int(std::llround(best_ppm / 250.0));
  const int ppm_last = initial ? (f->cadence_prior?0:4) : ppm_first;
  const int off_first = initial ? -8 : -1;
  const int off_last = initial ? 8 : 1;
  for(int pi=ppm_first;pi<=ppm_last;++pi) for(int oi=off_first;oi<=off_last;++oi) {
    // Once locked, use the fractional loop state itself.  Quantising it back
    // to the acquisition grid would throw away the correction learned from
    // prior pilot blocks every frame.
    const double ppm=initial ? (f->cadence_prior?f->cadence_ppm:double(pi)*250.0) : best_ppm;
    const double off=initial ? double(oi)*0.125 :
        std::max(f->timing_anchor-1.0,std::min(f->timing_anchor+1.0,
            timing_prediction+0.0625*double(oi)));
    // One known-training fit previously evaluated the same 112 sparse RRC
    // coordinates twice: once for H and once for residual NMSE.  Keep the
    // values in this fixed stack array; it removes those repeated 41-tap
    // endpoint convolutions without changing a single sampling coordinate or
    // floating-point accumulation order.  There is no frame allocation.
    constexpr unsigned kTrainingUsed = kTraining - 8U;
    std::array<std::array<C,2>, 2U * kTrainingUsed> training_y{};
    Channel h{};double den=0;
    for(unsigned idx=4;idx<kTraining-4U;++idx) {
      std::array<C,2> y{};if(!get(first+idx,off,ppm,&y))return false;
      training_y[idx-4U]=y;
      const C x=f->training[idx];for(unsigned r=0;r!=2;++r)h.at(r,0)+=y[r]*std::conj(x);den+=abs2(x);
    }
    for(unsigned idx=4;idx<kTraining-4U;++idx) {
      std::array<C,2> y{};if(!get(second+idx,off,ppm,&y))return false;
      training_y[kTrainingUsed+idx-4U]=y;
      const C x=f->training[idx];for(unsigned r=0;r!=2;++r)h.at(r,1)+=y[r]*std::conj(x);
    }
    for (C &v : h.v) v /= den;
    double err = 0, ene = 0;
    for(unsigned idx=4;idx<kTraining-4U;++idx) {
      const auto &first_y=training_y[idx-4U];
      const auto &second_y=training_y[kTrainingUsed+idx-4U];
      for(unsigned r=0;r!=2;++r){err+=abs2(first_y[r]-h.at(r,0)*f->training[idx]);ene+=abs2(first_y[r]);}
      for(unsigned r=0;r!=2;++r){err+=abs2(second_y[r]-h.at(r,1)*f->training[idx]);ene+=abs2(second_y[r]);}
    }
    const double cost=err/std::max(ene,1e-30);
    if (!initial) {
      locked_training_costs[unsigned(oi + 1)] = cost;
      if (oi == 0) { locked_training_center = h; locked_training_center_cost = cost; }
    }
    if(cost<best_cost){best_cost=cost;best_off=off;best_ppm=ppm;best_h=h;}
  }
  if (!initial) {
    // A local training timing measurement is accepted only when its sampled
    // three-point profile is convex.  If noise leaves an unobservable or
    // concave profile, retain the prediction and its independently fitted H;
    // do not turn an ambiguous innovation into a clock/origin update.
    const double curvature = locked_training_costs[0] - 2.0 * locked_training_costs[1] +
                             locked_training_costs[2];
    if (!(curvature > 0.0) || !std::isfinite(curvature)) {
      best_off = timing_prediction;
      best_h = locked_training_center;
      best_cost = locked_training_center_cost;
    } else {
      training_innovation = std::max(-0.0625, std::min(0.0625,
          0.5 * 0.0625 * (locked_training_costs[0] - locked_training_costs[2]) /
              curvature));
      training_innovation_valid = std::isfinite(training_innovation);
    }
  }
  if (!initial && (best_cost > 0.20 || condition2(best_h) > f->cfg.max_channel_condition)) {
    recovered_training = true;
    // A noisy but contiguous locked frame gets one bounded *known-training*
    // recovery before we discard lock.  This is not the cold 9x17 search and
    // never looks at payload decisions: 5 timing x 5 SFO candidates around
    // the carried loop state, executed only after the ordinary fit rejects.
    const auto local_training_fit = [&](double off, double ppm, Channel *out_h) -> double {
      Channel h{}; double denominator = 0.0;
      for (unsigned idx=4; idx<kTraining-4U; ++idx) {
        std::array<C,2> y{}; if (!get(first+idx,off,ppm,&y)) return std::numeric_limits<double>::infinity();
        const C x=f->training[idx]; for (unsigned r=0;r!=2;++r) h.at(r,0)+=y[r]*std::conj(x); denominator+=abs2(x);
      }
      for (unsigned idx=4; idx<kTraining-4U; ++idx) {
        std::array<C,2> y{}; if (!get(second+idx,off,ppm,&y)) return std::numeric_limits<double>::infinity();
        const C x=f->training[idx]; for (unsigned r=0;r!=2;++r) h.at(r,1)+=y[r]*std::conj(x);
      }
      for (C &value:h.v) value/=denominator;
      double error=0.0, energy=0.0;
      for (unsigned idx=4; idx<kTraining-4U; ++idx) {
        std::array<C,2> y{}; if (!get(first+idx,off,ppm,&y)) return std::numeric_limits<double>::infinity();
        for (unsigned r=0;r!=2;++r) { error+=abs2(y[r]-h.at(r,0)*f->training[idx]); energy+=abs2(y[r]); }
        if (!get(second+idx,off,ppm,&y)) return std::numeric_limits<double>::infinity();
        for (unsigned r=0;r!=2;++r) { error+=abs2(y[r]-h.at(r,1)*f->training[idx]); energy+=abs2(y[r]); }
      }
      *out_h=h;
      return error/std::max(energy,1e-30);
    };
    double recovered_cost = best_cost;
    double recovered_off = best_off, recovered_ppm = best_ppm;
    Channel recovered_h = best_h;
    for (int offset_step=-2;offset_step<=2;++offset_step) for (int ppm_step=-2;ppm_step<=2;++ppm_step) {
      const double trial_off = best_off + 0.125*double(offset_step);
      const double trial_ppm = std::max(-1000.0,std::min(1000.0,best_ppm + 50.0*double(ppm_step)));
      Channel h{}; const double cost=local_training_fit(trial_off,trial_ppm,&h);
      if (cost<recovered_cost) { recovered_cost=cost; recovered_off=trial_off; recovered_ppm=trial_ppm; recovered_h=h; }
    }
    best_cost=recovered_cost; best_off=recovered_off; best_ppm=recovered_ppm; best_h=recovered_h;
  }
  // Keep the exact coarse-grid zero fixed point: this is both the public
  // oracle's no-impairment reference and avoids magnifying ADC quantisation
  // residue into a made-up fractional timing/SFO correction.  A non-zero
  // bounded coarse observation is refined below before the first header.
  if (initial && (std::abs(best_off) > 1e-15 || std::abs(best_ppm) > 1e-15)) {
    // Acquisition-only coordinate refinement: alternating one-dimensional
    // training fits avoid carrying the coarse 0.125 sample/250 ppm grid into
    // the first header/data observations, without putting a grid in lock.
    const auto training_fit = [&](double off, double ppm, Channel *out_h) -> double {
      Channel h{}; double den = 0.0;
      for (unsigned idx=4;idx<kTraining-4U;++idx) {
        std::array<C,2> y{}; if (!get(first+idx,off,ppm,&y)) return std::numeric_limits<double>::infinity();
        const C x=f->training[idx]; for(unsigned r=0;r!=2;++r)h.at(r,0)+=y[r]*std::conj(x); den+=abs2(x);
      }
      for (unsigned idx=4;idx<kTraining-4U;++idx) {
        std::array<C,2> y{}; if (!get(second+idx,off,ppm,&y)) return std::numeric_limits<double>::infinity();
        const C x=f->training[idx]; for(unsigned r=0;r!=2;++r)h.at(r,1)+=y[r]*std::conj(x);
      }
      for (C &v:h.v) v/=den;
      double err=0.0,ene=0.0;
      for (unsigned idx=4;idx<kTraining-4U;++idx) {
        std::array<C,2> y{}; if (!get(first+idx,off,ppm,&y)) return std::numeric_limits<double>::infinity();
        for(unsigned r=0;r!=2;++r){err+=abs2(y[r]-h.at(r,0)*f->training[idx]);ene+=abs2(y[r]);}
        if (!get(second+idx,off,ppm,&y)) return std::numeric_limits<double>::infinity();
        for(unsigned r=0;r!=2;++r){err+=abs2(y[r]-h.at(r,1)*f->training[idx]);ene+=abs2(y[r]);}
      }
      *out_h=h; return err/std::max(ene,1e-30);
    };
    // Match the bounded coordinate-refinement schedule used by the Python
    // timing-v2 oracle: each one-dimensional grid includes the incumbent and
    // uses first-minimum ties.  In particular this avoids letting ADC
    // quantisation move an ideal zero-SFO fixture merely because a tiny
    // 9-point sub-grid happened to have a lower floating-point residue.
    struct RefineLevel { double offset_radius; double ppm_radius; };
    constexpr RefineLevel levels[] = {
      {1.2, 1000.0}, {0.2, 200.0}, {0.04, 40.0}, {0.008, 8.0},
    };
    for (const RefineLevel level : levels) {
      for (unsigned pass = 0; pass != 2U; ++pass) {
        double selected_cost = std::numeric_limits<double>::infinity();
        double selected_offset = best_off;
        Channel selected_h{};
        for (unsigned i = 0; i != 13U; ++i) {
          const double trial = best_off - level.offset_radius +
                               (2.0 * level.offset_radius * double(i) / 12.0);
          Channel h{}; const double cost = training_fit(trial, best_ppm, &h);
          if (cost < selected_cost) { selected_cost = cost; selected_offset = trial; selected_h = h; }
        }
        best_off = selected_offset; best_cost = selected_cost; best_h = selected_h;
        selected_cost = std::numeric_limits<double>::infinity();
        double selected_ppm = best_ppm;
        for (unsigned i = 0; i != 11U; ++i) {
          const double low = f->cadence_prior?f->cadence_ppm:std::max(-1000.0, best_ppm - level.ppm_radius);
          const double high = f->cadence_prior?f->cadence_ppm:std::min(1000.0, best_ppm + level.ppm_radius);
          const double trial = low + ((high - low) * double(i) / 10.0);
          Channel h{}; const double cost = training_fit(best_off, trial, &h);
          if (cost < selected_cost) { selected_cost = cost; selected_ppm = trial; selected_h = h; }
        }
        best_ppm = selected_ppm; best_cost = selected_cost; best_h = selected_h;
      }
    }
  }
  if (best_cost > 0.20 || condition2(best_h) > f->cfg.max_channel_condition) {
    #if defined(MRF1_NF_ENABLE_PROFILE)
    ++f->profile.tracking_rejects;
    #endif
    return false;
  }
  bool invertible = false;
  (void)inv2(best_h, &invertible);
  if (!invertible) return false;
  std::array<uint8_t,40> headers{};
  std::array<std::array<uint8_t, 20>, 2> header_bytes{};
  // Joint 2x2 QPSK ML for the public header avoids ZF noise enhancement.
  // Payload detection remains the real fixed-point PL MHGD path. Both public
  // header CRCs and identities below remain mandatory; no bit repair or truth.
  for(unsigned symbol=0;symbol<kHeaderSymbols;++symbol) {
    std::array<C,2> y{};if(!get(header_start+symbol,best_off,best_ppm,&y))return false;
    unsigned selected[2]={0,0};double best_distance=std::numeric_limits<double>::infinity();
    for(unsigned a=0;a!=4;++a) for(unsigned b=0;b!=4;++b) {
      const C x[2]={qpsk(uint8_t(a)),qpsk(uint8_t(b))};C predicted[2]{};mul(best_h,x,predicted);
      const double distance=abs2(y[0]-predicted[0])+abs2(y[1]-predicted[1]);
      if(distance<best_distance) {best_distance=distance;selected[0]=a;selected[1]=b;}
    }
    for(unsigned stream=0;stream!=2;++stream) header_bytes[stream][symbol/4U]|=uint8_t(selected[stream]<<(6U-2U*(symbol%4U)));
  }
  uint32_t rf_frame=0;
  if(!packet_header(header_bytes[0].data(),0,&rf_frame)) {
    #if defined(MRF1_NF_ENABLE_PROFILE)
    ++f->profile.header_rejects;
    #endif
    return false;
  }
  uint32_t frame1=0;
  if(!packet_header(header_bytes[1].data(),1,&frame1)||frame1!=rf_frame) {
    #if defined(MRF1_NF_ENABLE_PROFILE)
    ++f->profile.header_rejects;
    #endif
    return false;
  }
  std::memcpy(headers.data(),header_bytes[0].data(),20);std::memcpy(headers.data()+20,header_bytes[1].data(),20);
  #if defined(MRF1_NF_ENABLE_PROFILE)
  f->profile.training_header_ns += uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now() - training_header_begin).count());
  const auto pilot_task_begin = std::chrono::steady_clock::now();
  #endif
  // Preserve the state used to sample this frame. Pilot feedback below is
  // deliberately applied only to the next frame.
  const double entry_timing = best_off;
  const double entry_sfo_ppm = best_ppm;
  const double entry_cfo_hz = omega * double(f->cfg.sample_rate_hz) / (2.0 * kPi);
  std::array<double, 9> pilot_phases{};
  std::array<double, 9> pilot_centers{};
  unsigned pilot_count = 0;
  // Use the channel estimated from the current frame training.
  Channel h=best_h;
  // Fit one receive-only phase slope per TX from all nine public pilot pairs.
  // The training estimate anchors each TX column at its training center. This
  // avoids propagating a noisy single-pilot phase or an old NCO phase into H.
  constexpr unsigned kPilotPairs=(kPayloadSymbols+kPilotInterval-1U)/kPilotInterval;
  const double training_center[2]={double(first)+31.5,double(second)+31.5};
  double phase_sum[2]={0.0,0.0}, time_energy[2]={0.0,0.0};
  double previous_phase[2]={0.0,0.0};
  for(unsigned pair=0;pair!=kPilotPairs;++pair) {
    const unsigned position=header_start+kHeaderSymbols+pair*(kPilotInterval+2U);
    C common{};
    for(unsigned tx=0;tx!=2;++tx) {
      std::array<C,2> y{};if(!get(position+tx,best_off,best_ppm,&y))return false;
      C product{};
      for(unsigned rx=0;rx!=2;++rx) product+=std::conj(best_h.at(rx,tx)*f->pilots[tx])*y[rx];
      common+=product;
      double phase=std::arg(product);
      if(pair!=0U) phase=previous_phase[tx]+std::atan2(std::sin(phase-previous_phase[tx]),std::cos(phase-previous_phase[tx]));
      previous_phase[tx]=phase;
      const double dt=double(position+tx)-training_center[tx];
      phase_sum[tx]+=dt*phase;time_energy[tx]+=dt*dt;
    }
    if(pair==0U)f->first_pilot_phase=std::arg(common);
    if(!initial && pilot_count<pilot_phases.size()) {
      pilot_phases[pilot_count]=std::arg(common);
      pilot_centers[pilot_count]=(double(position)+0.5)*double(kSamplesPerSymbol);++pilot_count;
    }
  }
  const double slope[2]={phase_sum[0]/time_energy[0],phase_sum[1]/time_energy[1]};
  for(double value:slope) if(!std::isfinite(value)||std::abs(value)>2.0*kPi*500.0/1000000.0)return false;
  unsigned task=0,position=header_start+kHeaderSymbols;
  for(unsigned begin=0;begin<kPayloadSymbols;begin+=kPilotInterval) {
    const unsigned length=std::min(kPilotInterval,kPayloadSymbols-begin);
    for(unsigned k=0;k!=length;++k) {
      std::array<C,2> y{};const double symbol=double(position+2U+k);
      if(!get(symbol,best_off,best_ppm,&y))return false;
      double hraw[8]{};double yraw[4]={y[0].real(),y[0].imag(),y[1].real(),y[1].imag()};
      for(unsigned rx=0;rx!=2;++rx)for(unsigned tx=0;tx!=2;++tx) {
        const C value=best_h.at(rx,tx)*std::exp(C(0,slope[tx]*(symbol-training_center[tx])));
        hraw[2U*(rx*2U+tx)]=value.real();hraw[2U*(rx*2U+tx)+1U]=value.imag();
      }
      if(task==0U)f->first_detector_y=y;
      if(mrf1_nf_pack_q16_task(task+1U,f->cfg.config_id,uint16_t(f->cfg.task_iterations),(task+1U)|1U,f->cfg.proposal_step_raw,hraw,yraw,task_output+size_t(task)*MRF1_TASK_BYTES)!=MRF1_NF_OK)return false;
      ++task;
    }
    position+=2U+length;
  }
  if (task != MRF1_TASK_COUNT) return false;
  double next_cfo_hz = entry_cfo_hz;
  double next_sfo_ppm = entry_sfo_ppm;
  if (!initial && pilot_count >= 2U) {
    std::array<double, 8> residuals{};
    unsigned residual_count = 0;
    for (unsigned i = 1; i != pilot_count; ++i) {
      const double elapsed = pilot_centers[i] - pilot_centers[i - 1U];
      double delta = pilot_phases[i] - pilot_phases[i - 1U];
      delta = std::atan2(std::sin(delta), std::cos(delta));
      const double residual_hz = delta * double(f->cfg.sample_rate_hz) / (2.0 * kPi * elapsed);
      if (std::abs(residual_hz) > 5000.0) return false;
      residuals[residual_count++] = residual_hz;
    }
    std::sort(residuals.begin(), residuals.begin() + residual_count);
    const double median = residuals[residual_count / 2U];
    next_cfo_hz = std::max(f->cfo_anchor_hz - 500.0,
                           std::min(f->cfo_anchor_hz + 500.0,
                                    next_cfo_hz + 0.04 * median));
  }
  next_sfo_ppm = f->cadence_prior?f->cadence_ppm:update_sfo_window(f, initial, recovered_training,
                                   training_innovation_valid,
                                   training_innovation, entry_sfo_ppm);
  std::memcpy(header_output, headers.data(), headers.size());
  f->channel = h;
  f->training_channel = best_h;
  f->cfo_hz = next_cfo_hz;
  // The three-point known-training result is a frame-local measurement.  A
  // small alpha update moves the physical frame origin, while tau/SFO remain
  // reference-state coordinates.  Pilot pairs never mutate these states.
  const double timing_innovation = initial ? 0.0 : training_innovation;
  const double timing_alpha = 1.0;
  f->timing_offset = initial ? best_off : timing_prediction;
  f->sfo_ppm = next_sfo_ppm;
  f->provenance.epoch = f->previous.epoch;
  f->provenance.first_sequence = f->buffer_first_sequence;
  f->provenance.absolute_first_sample = f->buffer_first_sample + raw_start;
  f->provenance.absolute_end_exclusive = f->provenance.absolute_first_sample + kFrameSamples;
  f->provenance.frame_id = rf_frame;
  f->provenance.flags = initial ? 3U : 1U;
  f->provenance.cfo_hz = f->cfo_hz;
  f->provenance.timing_offset_samples = best_off;
  f->provenance.sfo_ppm = next_sfo_ppm;
  f->locked = true;
  if (initial) {
    f->cfo_anchor_hz = f->cfo_hz;
    f->timing_anchor = f->timing_offset;
    f->sfo_anchor_ppm = f->sfo_ppm;
  }
  // `best_ppm` is an RX-sample-coordinate slope (the same sign used in
  // `get()` above), so the next raw frame origin advances by the affine frame
  // duration, not by a fixed 3840 samples.  Keeping this fractional value is
  // essential: rounding here would reintroduce a deterministic ±100 ppm drift
  // across frames.  Pilots currently preserve this acquisition estimate; a
  // later loop update must use this same coordinate convention.
  const double reference = (double((first+4U)+(first+kTraining-5U)+(second+4U)+(second+kTraining-5U))/4.0)*double(kSamplesPerSymbol);
  const double physical_start = frame_origin + entry_timing - entry_sfo_ppm * 1e-6 * reference;
  const double physical_end = physical_start + double(kFrameSamples) * (1.0 + entry_sfo_ppm * 1e-6);
  // Re-express the physical end with the state chosen for the next frame;
  // using origin += frame_length after changing tau/epsilon double-counts or
  // loses timing corrections.
  // Initial acquisition promotes its fitted tau to the persistent state, so
  // the usual physical re-expression applies.  Locked frames keep tau as a
  // reference coordinate: correct the same-qref physical origin exactly
  // once by the training innovation.  Do not combine the two forms, which
  // would apply the measured delta twice.
  if (initial) {
    f->next_origin = physical_end - f->timing_offset + f->sfo_ppm * 1e-6 * reference;
  } else {
    f->next_origin = frame_origin + double(kFrameSamples) *
        (1.0 + next_sfo_ppm * 1e-6) + timing_alpha * timing_innovation;
  }
  // Bound the continuous RX ring after a completed frame while retaining the
  // floor(next_origin) sample for the next fractional interpolation.  Rebase
  // the relative origin together with the physical buffer coordinate; the
  // NCO reference intentionally remains absolute.
  const size_t consume = std::min<size_t>(size_t(std::floor(f->next_origin)), f->samples.size());
  if (consume != 0) {
    f->samples.erase(f->samples.begin(), f->samples.begin() + ptrdiff_t(consume));
    f->unfiltered_samples.erase(f->unfiltered_samples.begin(),f->unfiltered_samples.begin()+ptrdiff_t(consume));
    f->buffer_first_sample += consume;
    f->next_origin -= double(consume);
  }
  f->stats.emitted_batches++;
  #if defined(MRF1_NF_ENABLE_PROFILE)
  f->profile.pilot_task_ns += uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now() - pilot_task_begin).count());
  f->profile.completed_frames++;
  #endif
  f->emitted = true;
  return true;
}

void set_defaults(mrf1_nf_config *c) { if(c->sample_rate_hz==0)c->sample_rate_hz=4000000;if(c->samples_per_symbol==0)c->samples_per_symbol=4;if(c->task_iterations==0)c->task_iterations=64;if(c->config_id==0)c->config_id=2;if(c->proposal_step_raw==0)c->proposal_step_raw=32768;if(c->max_buffer_samples==0)c->max_buffer_samples=8192;if(c->repetition_threshold==0)c->repetition_threshold=.55;if(c->preamble_threshold==0)c->preamble_threshold=.75;if(c->max_channel_condition==0)c->max_channel_condition=1000.; }
bool valid_config(const mrf1_nf_config &c) { return c.sample_rate_hz==4000000&&c.samples_per_symbol==4&&c.task_iterations>0&&c.task_iterations<=65535&&c.config_id!=0&&c.proposal_step_raw<=65535&&c.max_buffer_samples>=4096&&c.max_buffer_samples<=262144&&c.repetition_threshold>0&&c.repetition_threshold<=1&&c.preamble_threshold>0&&c.preamble_threshold<=1&&c.max_channel_condition>1&&std::isfinite(c.max_channel_condition); }
} // namespace

extern "C" mrf1_nf_status mrf1_nf_pack_q16_task(uint32_t frame_id,uint32_t config_id,uint16_t iterations,uint32_t seed,uint32_t proposal_step_raw,const double *hraw,const double *yraw,uint8_t out[MRF1_TASK_BYTES]) {
  if(!hraw||!yraw||!out||frame_id==0||config_id==0||iterations==0||seed==0||proposal_step_raw>65535U)return MRF1_NF_ARGUMENT;
  Channel h{};C y[2]{};for(unsigned r=0;r!=2;++r)for(unsigned c=0;c!=2;++c){const double re=hraw[2U*(r*2U+c)],im=hraw[2U*(r*2U+c)+1U];if(!std::isfinite(re)||!std::isfinite(im))return MRF1_NF_ARGUMENT;h.at(r,c)=C(re,im);}for(unsigned r=0;r!=2;++r){if(!std::isfinite(yraw[2U*r])||!std::isfinite(yraw[2U*r+1U]))return MRF1_NF_ARGUMENT;y[r]=C(yraw[2U*r],yraw[2U*r+1U]);}
  Channel g{};C b[2]{};double c=0;for(unsigned i=0;i!=2;++i){for(unsigned j=0;j!=2;++j)for(unsigned r=0;r!=2;++r)g.at(i,j)+=std::conj(h.at(r,i))*h.at(r,j);for(unsigned r=0;r!=2;++r)b[i]+=std::conj(h.at(r,i))*y[r];c+=abs2(y[i]);}
  std::memset(out,0,MRF1_TASK_BYTES);put32(out,kInputMagic);put16(out+4,kStreamVersion);out[6]=1;out[7]=8;put32(out+8,frame_id);put32(out+12,config_id);out[16]=2;out[17]=2;out[18]=kQpsk;out[19]=kDetectorMhdg;put16(out+20,iterations);put32(out+24,seed);put16(out+28,0);put16(out+30,0);put16(out+32,kInputBeats);put16(out+34,kOutputBeats);put16(out+36,iterations);put16(out+38,iterations);put_i32(out+40,0);put_i32(out+48,0);put_i32(out+52,static_cast<int32_t>(proposal_step_raw));
  size_t at=64;for(unsigned i=0;i!=2;++i)for(unsigned j=0;j!=2;++j){int64_t re=0,im=0;if(!q16(g.at(i,j).real()/2.0,32,&re)||!q16(g.at(i,j).imag()/2.0,32,&im))return MRF1_NF_QUANTIZATION;put_i32(out+at,static_cast<int32_t>(re));put_i32(out+at+4,static_cast<int32_t>(im));at+=8;}
  for(unsigned i=0;i!=2;++i){int64_t re=0,im=0;if(!q16(b[i].real()*kInvSqrt2,32,&re)||!q16(b[i].imag()*kInvSqrt2,32,&im))return MRF1_NF_QUANTIZATION;put_i32(out+at,static_cast<int32_t>(re));put_i32(out+at+4,static_cast<int32_t>(im));at+=8;}int64_t rawc=0;if(!q16(c,64,&rawc))return MRF1_NF_QUANTIZATION;put_i64(out+112,rawc);return MRF1_NF_OK;
}

extern "C" mrf1_nf_status mrf1_nf_create(const mrf1_nf_config *config,mrf1_nf_frontend **out) { if(!out)return MRF1_NF_ARGUMENT;*out=nullptr;mrf1_nf_config c{};if(config)c=*config;set_defaults(&c);if(!valid_config(c))return MRF1_NF_ARGUMENT;try{auto *f=new mrf1_nf_frontend();
#ifdef MRF1_NF_ENABLE_DIAG_DC_BLOCKER
// Explicit receive-IQ-derived coarse frequency prior for finite diagnostics.
// No expected packet, payload decision or CRC enters this preprocessing.
const char *text=std::getenv("LINK_DIAGNOSTIC_COARSE_CFO_HZ");
if(text) {
  char *end=nullptr;const double hz=std::strtod(text,&end);
  if(end==text || *end!='\0' || !std::isfinite(hz) || std::abs(hz)>500000.0) {delete f;return MRF1_NF_ARGUMENT;}
  f->coarse_step=std::exp(C(0,-2.0*kPi*hz/4000000.0));f->coarse_prior=true;
}
const char *cadence=std::getenv("LINK_DIAGNOSTIC_SFO_PPM");
if(cadence) {
  char *end=nullptr;const double ppm=std::strtod(cadence,&end);
  if(end==cadence || *end!='\0' || !std::isfinite(ppm) || std::abs(ppm)>1000.0) {delete f;return MRF1_NF_ARGUMENT;}
  f->cadence_prior=true;f->cadence_ppm=ppm;
}
#endif
f->cfg=c;f->pending.reserve(MRF1_RXTH_BLOCK_BYTES);f->samples.reserve(c.max_buffer_samples);f->unfiltered_samples.reserve(c.max_buffer_samples);f->tasks.resize(MRF1_BATCH_TASK_BYTES);f->raw_work.resize(3840);f->matched_work.resize(3840+kRrcTaps-1U);f->taps=rrc_taps();const auto preamble_block=known_symbols("MRF1-PREAMBLE",32);auto pre=preamble_block;pre.reserve(96);pre.insert(pre.end(),preamble_block.begin(),preamble_block.end());const auto sfd=known_symbols("MRF1-SFD",32);pre.insert(pre.end(),sfd.begin(),sfd.end());f->preamble_template=pulse_shape(pre,f->taps);f->training=known_symbols("MRF1-TRAINING",64);f->pilots=known_symbols("MRF1-PILOT",2);*out=f;return MRF1_NF_OK;}catch(const std::bad_alloc&){return MRF1_NF_CAPACITY;} }
extern "C" void mrf1_nf_destroy(mrf1_nf_frontend *f){delete f;}
extern "C" void mrf1_nf_reset(mrf1_nf_frontend *f){if(!f)return;
#ifdef MRF1_NF_ENABLE_DIAG_DC_BLOCKER
f->dc_previous_input={};f->dc_previous_output={};f->coarse_phase={1.0,0.0};
#endif
f->pending.clear();f->samples.clear();f->unfiltered_samples.clear();f->tasks.assign(MRF1_BATCH_TASK_BYTES,0);f->headers.fill(0);f->has_previous=false;f->gap=false;f->poisoned=false;f->locked=false;f->emitted=false;f->sample_end=0;f->buffer_first_sample=0;f->nco_reference_sample=0;f->buffer_first_sequence=0;f->has_nco_reference=false;f->stats={};f->profile={};f->provenance={};f->channel={};f->sfo_innovation_window.fill(0.0);f->sfo_innovation_count=0;f->cfo_hz=f->timing_offset=f->sfo_ppm=f->next_origin=f->cfo_anchor_hz=f->timing_anchor=f->sfo_anchor_ppm=0.0;}
extern "C" void mrf1_nf_get_stats(const mrf1_nf_frontend *f,mrf1_nf_stats *out){if(!f||!out)return;*out=f->stats;out->buffered_samples=f->samples.size();out->state=f->locked?1U:0U;}
extern "C" void mrf1_nf_get_profile(const mrf1_nf_frontend *f,mrf1_nf_profile *out){if(!f||!out)return;*out=f->profile;}
extern "C" void mrf1_nf_get_debug_snapshot(const mrf1_nf_frontend *f,mrf1_nf_debug_snapshot *out){
  if(!f||!out)return;
  out->cfo_hz=f->cfo_hz;out->timing_offset_samples=f->timing_offset;out->sfo_ppm=f->sfo_ppm;
  for(unsigned r=0;r!=2;++r)for(unsigned c=0;c!=2;++c){const C v=f->training_channel.at(r,c);out->training_h_re_im_rowmajor[2U*(r*2U+c)]=v.real();out->training_h_re_im_rowmajor[2U*(r*2U+c)+1U]=v.imag();}
  for(unsigned r=0;r!=2;++r){out->first_detector_y_re_im[2U*r]=f->first_detector_y[r].real();out->first_detector_y_re_im[2U*r+1U]=f->first_detector_y[r].imag();}
  out->first_pilot_phase_radians=f->first_pilot_phase;
}
namespace {
mrf1_nf_status produce(mrf1_nf_frontend *f, const mrf1_nf_output_slot *slot,
                       mrf1_nf_batch_view *out) {
  if (f->samples.size() < 3840U) return MRF1_NF_NEED_MORE;
  bool done = false;
  if (f->locked) {
    done = build_batch(f, f->next_origin,
                       f->cfo_hz * 2.0 * kPi / double(f->cfg.sample_rate_hz), false,
                       slot->headers, slot->task_bytes);
  } else {
    size_t start = 0;
    double omega = 0.0;
    f->stats.acquisition_attempts++;
    #if defined(MRF1_NF_ENABLE_PROFILE)
    const auto acquisition_begin = std::chrono::steady_clock::now();
    #endif
    if (acquire(f, &start, &omega)) {
      #if defined(MRF1_NF_ENABLE_PROFILE)
      f->profile.acquisition_ns += uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now() - acquisition_begin).count());
      #endif
      done = build_batch(f, double(start) - double(kGuard * kSamplesPerSymbol), omega, true,
                         slot->headers, slot->task_bytes);
    } else {
      #if defined(MRF1_NF_ENABLE_PROFILE)
      f->profile.acquisition_ns += uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now() - acquisition_begin).count());
      #endif
      f->stats.acquisition_failures++;
    }
  }
  if (!done) return MRF1_NF_REACQUIRING;
  out->provenance = f->provenance;
  out->headers = slot->headers;
  out->task_bytes = slot->task_bytes;
  out->header_bytes = 40;
  out->task_bytes_len = MRF1_BATCH_TASK_BYTES;
  return MRF1_NF_OK;
}
}  // namespace

extern "C" mrf1_nf_status mrf1_nf_feed(mrf1_nf_frontend *f, const uint8_t *data,
                                         size_t n, mrf1_nf_batch_view *out) {
  if (!f) return MRF1_NF_ARGUMENT;
  const mrf1_nf_output_slot legacy{f->headers.data(), f->headers.size(), f->tasks.data(), f->tasks.size()};
  return mrf1_nf_feed_into(f, data, n, &legacy, out);
}
extern "C" mrf1_nf_status mrf1_nf_feed_into(mrf1_nf_frontend *f, const uint8_t *data,
                                              size_t n, const mrf1_nf_output_slot *slot,
                                              mrf1_nf_batch_view *out) {
  if (!f || !data || !slot || !out || !slot->headers || !slot->task_bytes ||
      slot->headers_capacity < 40 || slot->task_bytes_capacity < MRF1_BATCH_TASK_BYTES ||
      n == 0 || n > 1024U * 1024U) return MRF1_NF_ARGUMENT;
  if (f->poisoned) return MRF1_NF_TRANSPORT;
  f->emitted = false;
  f->pending.insert(f->pending.end(), data, data + n);
  while (f->pending.size() >= MRF1_RXTH_BLOCK_BYTES) {
    const mrf1_nf_status status = append_block(f, f->pending.data());
    f->pending.erase(f->pending.begin(), f->pending.begin() + MRF1_RXTH_BLOCK_BYTES);
    if (status < 0) { f->poisoned = true; return status; }
  }
  return produce(f, slot, out);
}
extern "C" mrf1_nf_status mrf1_nf_finish(mrf1_nf_frontend *f, mrf1_nf_batch_view *out) {
  if (!f || !out) return MRF1_NF_ARGUMENT;
  if (!f->pending.empty()) return MRF1_NF_TRANSPORT;
  const mrf1_nf_output_slot legacy{f->headers.data(), f->headers.size(), f->tasks.data(), f->tasks.size()};
  return produce(f, &legacy, out);
}
extern "C" const char *mrf1_nf_status_string(mrf1_nf_status s){switch(s){case MRF1_NF_OK:return "ok";case MRF1_NF_NEED_MORE:return "need_more";case MRF1_NF_REACQUIRING:return "reacquiring";case MRF1_NF_NO_FRAME:return "no_frame";case MRF1_NF_ARGUMENT:return "argument";case MRF1_NF_TRANSPORT:return "transport";case MRF1_NF_UNSUPPORTED:return "unsupported";case MRF1_NF_ACQUISITION:return "acquisition";case MRF1_NF_CHANNEL:return "channel";case MRF1_NF_HEADER:return "header";case MRF1_NF_QUANTIZATION:return "quantization";case MRF1_NF_CAPACITY:return "capacity";default:return "internal";}}
