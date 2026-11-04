/*
 * Finite offline-RXTH to real-IMBC diagnostic.
 *
 * This intentionally is not an RF capture program.  It consumes a bounded,
 * regular file of complete RXTH v1 blocks, but detector submissions use the
 * production rnt_transport Linux implementation against the real IMBC device.
 * It is deliberately serial and finite, therefore it is not the production
 * pipelined RXTH/DSP/IMBC runtime and makes no DMA or over-the-air claim.
 * There is no injected ioctl table, MOP preload, TX command, or software
 * detector fallback in this executable.
 */
#include "mrf1_native_frontend.h"
#include "rf_native_transport.h"

#include <array>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <limits>
#include <string>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

namespace {

constexpr char kImbcDevice[] = "/dev/zed-mimo-interleaved-candidate";
constexpr uint32_t kFramesToReceive = 20;
constexpr uint32_t kImbcTimeoutMs = 1000;
constexpr uint32_t kConfigId = 2;
constexpr uint32_t kIterations = 64;
constexpr int32_t kProposalStepRaw = 32768;
constexpr uint64_t kMaximumInputBlocks = 4096;
constexpr std::size_t kDeliveryHeaderBytes = 20;
static_assert(MRF1_RXTH_BLOCK_BYTES == 8224U, "RXTH replay ABI drift");
static_assert(MRF1_MRF_HEADER_BYTES * 2U == RNT_PUBLIC_HEADERS_BYTES, "MRF/RNT header ABI drift");
static_assert(static_cast<std::size_t>(MRF1_BATCH_TASK_BYTES) ==
              static_cast<std::size_t>(RNT_TASK_BUFFER_BYTES), "frontend/RNT task ABI drift");

struct Options {
  std::string rxth_path;
  std::string output_path;
  std::string expected_packets_path;
  std::string expected_packets_sha256;
  uint32_t expected_build_id = 0;
};

struct FileOwner {
  int fd = -1;
  ~FileOwner() { if (fd >= 0) (void)::close(fd); }
  FileOwner() = default;
  explicit FileOwner(int value) : fd(value) {}
  FileOwner(const FileOwner&) = delete;
  FileOwner& operator=(const FileOwner&) = delete;
};

struct FrontendOwner {
  mrf1_nf_frontend* value = nullptr;
  ~FrontendOwner() { if (value != nullptr) mrf1_nf_destroy(value); }
};

struct TransportOwner {
  rnt_transport* value = nullptr;
  ~TransportOwner() {
    if (value != nullptr) rnt_destroy(value);
  }
};

uint32_t crc32_update(uint32_t crc, const uint8_t* bytes, std::size_t length) {
  while (length-- != 0U) {
    crc ^= *bytes++;
    for (unsigned bit = 0; bit != 8U; ++bit) {
      crc = (crc >> 1U) ^ ((crc & 1U) != 0U ? 0xedb88320U : 0U);
    }
  }
  return crc;
}

void store_le32(uint8_t* bytes, uint32_t value) {
  for (unsigned index = 0; index != 4U; ++index) {
    bytes[index] = static_cast<uint8_t>(value >> (8U * index));
  }
}

bool write_all(int fd, const uint8_t* bytes, std::size_t length) {
  while (length != 0U) {
    const ssize_t written = ::write(fd, bytes, length);
    if (written < 0 && errno == EINTR) continue;
    if (written <= 0) return false;
    bytes += static_cast<std::size_t>(written);
    length -= static_cast<std::size_t>(written);
  }
  return true;
}

bool read_all(int fd, uint8_t* bytes, std::size_t length) {
  while (length != 0U) {
    const ssize_t received = ::read(fd, bytes, length);
    if (received < 0 && errno == EINTR) continue;
    if (received <= 0) return false;
    bytes += static_cast<std::size_t>(received);
    length -= static_cast<std::size_t>(received);
  }
  return true;
}

bool regular_file_size(int fd, uint64_t* bytes) {
  struct stat metadata{};
  if (::fstat(fd, &metadata) != 0 || !S_ISREG(metadata.st_mode) || metadata.st_size < 0) {
    return false;
  }
  *bytes = static_cast<uint64_t>(metadata.st_size);
  return true;
}

bool open_regular_readonly(const std::string& path, FileOwner* out, uint64_t* bytes) {
  const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (fd < 0 || !regular_file_size(fd, bytes)) {
    if (fd >= 0) (void)::close(fd);
    return false;
  }
  out->fd = fd;
  return true;
}

bool open_new_regular_output(const std::string& path, FileOwner* out) {
  const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  uint64_t ignored = 0;
  if (fd < 0 || !regular_file_size(fd, &ignored)) {
    if (fd >= 0) (void)::close(fd);
    return false;
  }
  out->fd = fd;
  return true;
}

bool parse_u32(const std::string& text, uint32_t* value) {
  if (text.empty()) return false;
  unsigned base = 10;
  std::size_t position = 0;
  if (text.size() > 2U && text[0] == '0' && (text[1] == 'x' || text[1] == 'X')) {
    base = 16;
    position = 2;
  }
  if (position == text.size()) return false;
  uint32_t result = 0;
  for (; position < text.size(); ++position) {
    const char character = text[position];
    unsigned digit = 16;
    if (character >= '0' && character <= '9') digit = static_cast<unsigned>(character - '0');
    else if (character >= 'a' && character <= 'f') digit = static_cast<unsigned>(character - 'a') + 10U;
    else if (character >= 'A' && character <= 'F') digit = static_cast<unsigned>(character - 'A') + 10U;
    if (digit >= base || result > (std::numeric_limits<uint32_t>::max() - digit) / base) return false;
    result = result * base + digit;
  }
  *value = result;
  return true;
}

bool valid_sha256_hex(const std::string& text) {
  if (text.size() != 64U) return false;
  for (const char character : text) {
    if (!((character >= '0' && character <= '9') ||
          (character >= 'a' && character <= 'f') ||
          (character >= 'A' && character <= 'F'))) return false;
  }
  return true;
}

bool argument_value(int argc, char** argv, const char* name, std::string* out) {
  bool found = false;
  for (int index = 1; index < argc; ++index) {
    if (std::string(argv[index]) == name) {
      if (found || index + 1 >= argc || argv[index + 1][0] == '-') return false;
      *out = argv[++index];
      found = true;
    }
  }
  return found;
}

bool parse_options(int argc, char** argv, Options* out) {
  std::string build_id;
  if (!argument_value(argc, argv, "--rxth-file", &out->rxth_path) ||
      !argument_value(argc, argv, "--output", &out->output_path) ||
      !argument_value(argc, argv, "--imbc-build-id", &build_id) ||
      !parse_u32(build_id, &out->expected_build_id) || out->expected_build_id == 0U) return false;

  bool expect_packets = false;
  bool expect_hash = false;
  for (int index = 1; index < argc; ++index) {
    const std::string argument(argv[index]);
    if (argument == "--rxth-file" || argument == "--output" || argument == "--imbc-build-id" ||
        argument == "--expected-packets" || argument == "--expected-packets-sha256") {
      if (index + 1 >= argc || argv[index + 1][0] == '-') return false;
      if (argument == "--expected-packets") {
        if (expect_packets) return false;
        out->expected_packets_path = argv[index + 1];
        expect_packets = true;
      } else if (argument == "--expected-packets-sha256") {
        if (expect_hash) return false;
        out->expected_packets_sha256 = argv[index + 1];
        expect_hash = true;
      }
      ++index;
    } else {
      return false;
    }
  }
  return (!expect_hash || (expect_packets && valid_sha256_hex(out->expected_packets_sha256)));
}

// Compact SHA-256 used only to pin an optional *comparison fixture*.  That
// fixture is read after the real detector returns and is never frontend input.
class Sha256 {
 public:
  void update(const uint8_t* bytes, std::size_t length) {
    total_bytes_ += length;
    while (length != 0U) {
      const std::size_t copied = length < (64U - used_) ? length : (64U - used_);
      std::memcpy(block_.data() + used_, bytes, copied);
      used_ += copied;
      bytes += copied;
      length -= copied;
      if (used_ == block_.size()) {
        transform(block_.data());
        used_ = 0;
      }
    }
  }

  std::string finish_hex() {
    const uint64_t bits = total_bytes_ * 8U;
    const uint8_t one = 0x80U;
    update(&one, 1U);
    const uint8_t zero = 0;
    while (used_ != 56U) update(&zero, 1U);
    uint8_t length[8]{};
    for (unsigned index = 0; index != 8U; ++index) length[7U - index] = static_cast<uint8_t>(bits >> (8U * index));
    update(length, sizeof(length));
    static constexpr char kHex[] = "0123456789abcdef";
    std::string result;
    result.reserve(64U);
    for (uint32_t word : state_) {
      for (int shift = 28; shift >= 0; shift -= 4) result.push_back(kHex[(word >> shift) & 0xfU]);
    }
    return result;
  }

 private:
  static uint32_t rotr(uint32_t value, unsigned count) { return (value >> count) | (value << (32U - count)); }
  static uint32_t load_be32(const uint8_t* bytes) {
    return (static_cast<uint32_t>(bytes[0]) << 24U) | (static_cast<uint32_t>(bytes[1]) << 16U) |
           (static_cast<uint32_t>(bytes[2]) << 8U) | bytes[3];
  }
  void transform(const uint8_t* bytes) {
    static constexpr uint32_t k[64] = {
      0x428a2f98U,0x71374491U,0xb5c0fbcfU,0xe9b5dba5U,0x3956c25bU,0x59f111f1U,0x923f82a4U,0xab1c5ed5U,
      0xd807aa98U,0x12835b01U,0x243185beU,0x550c7dc3U,0x72be5d74U,0x80deb1feU,0x9bdc06a7U,0xc19bf174U,
      0xe49b69c1U,0xefbe4786U,0x0fc19dc6U,0x240ca1ccU,0x2de92c6fU,0x4a7484aaU,0x5cb0a9dcU,0x76f988daU,
      0x983e5152U,0xa831c66dU,0xb00327c8U,0xbf597fc7U,0xc6e00bf3U,0xd5a79147U,0x06ca6351U,0x14292967U,
      0x27b70a85U,0x2e1b2138U,0x4d2c6dfcU,0x53380d13U,0x650a7354U,0x766a0abbU,0x81c2c92eU,0x92722c85U,
      0xa2bfe8a1U,0xa81a664bU,0xc24b8b70U,0xc76c51a3U,0xd192e819U,0xd6990624U,0xf40e3585U,0x106aa070U,
      0x19a4c116U,0x1e376c08U,0x2748774cU,0x34b0bcb5U,0x391c0cb3U,0x4ed8aa4aU,0x5b9cca4fU,0x682e6ff3U,
      0x748f82eeU,0x78a5636fU,0x84c87814U,0x8cc70208U,0x90befffaU,0xa4506cebU,0xbef9a3f7U,0xc67178f2U };
    std::array<uint32_t, 64> schedule{};
    for (unsigned index = 0; index != 16U; ++index) schedule[index] = load_be32(bytes + 4U * index);
    for (unsigned index = 16; index != 64U; ++index) {
      const uint32_t s0 = rotr(schedule[index - 15U], 7U) ^ rotr(schedule[index - 15U], 18U) ^ (schedule[index - 15U] >> 3U);
      const uint32_t s1 = rotr(schedule[index - 2U], 17U) ^ rotr(schedule[index - 2U], 19U) ^ (schedule[index - 2U] >> 10U);
      schedule[index] = schedule[index - 16U] + s0 + schedule[index - 7U] + s1;
    }
    uint32_t a = state_[0], b = state_[1], c = state_[2], d = state_[3], e = state_[4], f = state_[5], g = state_[6], h = state_[7];
    for (unsigned index = 0; index != 64U; ++index) {
      const uint32_t s1 = rotr(e, 6U) ^ rotr(e, 11U) ^ rotr(e, 25U);
      const uint32_t choice = (e & f) ^ (~e & g);
      const uint32_t t1 = h + s1 + choice + k[index] + schedule[index];
      const uint32_t s0 = rotr(a, 2U) ^ rotr(a, 13U) ^ rotr(a, 22U);
      const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
      const uint32_t t2 = s0 + majority;
      h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    state_[0] += a; state_[1] += b; state_[2] += c; state_[3] += d;
    state_[4] += e; state_[5] += f; state_[6] += g; state_[7] += h;
  }
  std::array<uint32_t, 8> state_{{0x6a09e667U,0xbb67ae85U,0x3c6ef372U,0xa54ff53aU,0x510e527fU,0x9b05688cU,0x1f83d9abU,0x5be0cd19U}};
  std::array<uint8_t, 64> block_{};
  uint64_t total_bytes_ = 0;
  std::size_t used_ = 0;
};

bool lower_hex_equal(const std::string& left, const std::string& right) {
  if (left.size() != right.size()) return false;
  for (std::size_t index = 0; index != left.size(); ++index) {
    char character = right[index];
    if (character >= 'A' && character <= 'F') character = static_cast<char>(character - 'A' + 'a');
    if (left[index] != character) return false;
  }
  return true;
}

bool sha256_known_vector_test() {
  static constexpr uint8_t kAbc[] = {'a', 'b', 'c'};
  Sha256 hash;
  hash.update(kAbc, sizeof(kAbc));
  return hash.finish_hex() ==
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
}

int fail(const char* message) {
  std::cerr << "board_iq_replay: " << message << '\n';
  return 2;
}

void usage() {
  std::cerr << "usage: board_iq_replay --rxth-file INPUT --output NEW_RFD1 --imbc-build-id ID"
            << " [--expected-packets PACKETS_20x304] [--expected-packets-sha256 SHA256]\n";
}

}  // namespace

int main(int argc, char** argv) {
  Options options{};
  if (!parse_options(argc, argv, &options)) {
    usage();
    return 2;
  }
  if (!sha256_known_vector_test()) return fail("internal SHA-256 known-vector check failed");

  FileOwner input;
  uint64_t input_bytes = 0;
  if (!open_regular_readonly(options.rxth_path, &input, &input_bytes) || input_bytes == 0U ||
      input_bytes % MRF1_RXTH_BLOCK_BYTES != 0U ||
      input_bytes / MRF1_RXTH_BLOCK_BYTES > kMaximumInputBlocks) {
    return fail("RXTH input must be a nonempty regular file of at most 4096 complete 8224-byte blocks");
  }

  FileOwner expected;
  uint64_t expected_bytes = 0;
  if (!options.expected_packets_path.empty()) {
    const uint64_t required = static_cast<uint64_t>(kFramesToReceive) * RNT_PACKETS_BYTES;
    if (!open_regular_readonly(options.expected_packets_path, &expected, &expected_bytes) || expected_bytes != required) {
      return fail("expected-packets must be a regular file containing exactly 20 received 304-byte packet pairs");
    }
    if (!options.expected_packets_sha256.empty()) {
      Sha256 hash;
      std::array<uint8_t, 4096> buffer{};
      uint64_t remaining = expected_bytes;
      while (remaining != 0U) {
        const std::size_t count = remaining < buffer.size() ? static_cast<std::size_t>(remaining) : buffer.size();
        if (!read_all(expected.fd, buffer.data(), count)) return fail("could not hash expected-packets");
        hash.update(buffer.data(), count);
        remaining -= count;
      }
      if (!lower_hex_equal(hash.finish_hex(), options.expected_packets_sha256) || ::lseek(expected.fd, 0, SEEK_SET) != 0) {
        return fail("expected-packets SHA-256 mismatch");
      }
    }
  }

  FileOwner output;
  if (!open_new_regular_output(options.output_path, &output)) {
    return fail("output must be a new regular file (O_EXCL, no symlink)");
  }

  mrf1_nf_config frontend_config{};
  frontend_config.sample_rate_hz = 4000000U;
  frontend_config.samples_per_symbol = 4U;
  frontend_config.task_iterations = kIterations;
  frontend_config.config_id = kConfigId;
  frontend_config.proposal_step_raw = static_cast<uint32_t>(kProposalStepRaw);
  FrontendOwner frontend;
  if (mrf1_nf_create(&frontend_config, &frontend.value) != MRF1_NF_OK) {
    return fail("could not create production native frontend");
  }

  // NULL is deliberate: rnt_open selects its Linux /dev ioctl implementation.
  TransportOwner transport;
  const rnt_code open_code = rnt_open(kImbcDevice, nullptr, &transport.value);
  if (open_code != RNT_OK) {
    std::cerr << "board_iq_replay: rnt_open(" << kImbcDevice << ") failed: " << rnt_code_string(open_code) << '\n';
    return 3;
  }

  std::array<uint8_t, MRF1_RXTH_BLOCK_BYTES> rxth_block{};
  std::array<uint8_t, RNT_PUBLIC_HEADERS_BYTES> headers{};
  std::array<uint8_t, RNT_TASK_BUFFER_BYTES> tasks{};
  std::array<uint8_t, RNT_PACKETS_BYTES> expected_packets{};
  const mrf1_nf_output_slot slot{headers.data(), headers.size(), tasks.data(), tasks.size()};
  const auto started = std::chrono::steady_clock::now();
  uint32_t submitted_frames = 0;
  uint32_t received_frames = 0;
  uint32_t dual_crc_frames = 0;
  uint32_t crc_rejected_frames = 0;
  bool expected_packets_match = true;

  const uint64_t blocks = input_bytes / MRF1_RXTH_BLOCK_BYTES;
  for (uint64_t index = 0; index != blocks && received_frames != kFramesToReceive; ++index) {
    if (!read_all(input.fd, rxth_block.data(), rxth_block.size())) return fail("short read from validated RXTH file");
    mrf1_nf_batch_view batch{};
    const mrf1_nf_status frontend_code = mrf1_nf_feed_into(frontend.value, rxth_block.data(), rxth_block.size(), &slot, &batch);
    if (frontend_code == MRF1_NF_NEED_MORE || frontend_code == MRF1_NF_REACQUIRING || frontend_code == MRF1_NF_NO_FRAME) continue;
    if (frontend_code != MRF1_NF_OK || batch.headers != headers.data() || batch.task_bytes != tasks.data() ||
        batch.header_bytes != RNT_PUBLIC_HEADERS_BYTES || batch.task_bytes_len != RNT_TASK_BUFFER_BYTES) {
      std::cerr << "board_iq_replay: frontend failed: " << mrf1_nf_status_string(frontend_code) << '\n';
      return 4;
    }

    rnt_submit_config submit{};
    submit.rf_frame_id = batch.provenance.frame_id;
    submit.expected_build_id = options.expected_build_id;
    submit.timeout_ms = kImbcTimeoutMs;
    submit.required_config_id = kConfigId;
    submit.required_iterations = kIterations;
    submit.required_proposal_step_raw = kProposalStepRaw;
    rnt_frame_result result{};
    ++submitted_frames;
    const rnt_code submit_code = rnt_submit_mrf_frame(transport.value, tasks.data(), tasks.size(), headers.data(), headers.size(), &submit, &result);
    if (submit_code == RNT_E_CRC_REJECTED) {
      ++crc_rejected_frames;
      continue;
    }
    if (submit_code != RNT_OK) {
      std::cerr << "board_iq_replay: rnt_submit_mrf_frame failed: " << rnt_code_string(submit_code) << '\n';
      return 5;
    }

    // RNT_OK means its production parser/reorder path accepted both MRF1 CRCs.
    ++dual_crc_frames;
    if (expected.fd >= 0) {
      if (!read_all(expected.fd, expected_packets.data(), expected_packets.size()) ||
          std::memcmp(expected_packets.data(), result.packets, sizeof(result.packets)) != 0) {
        expected_packets_match = false;
      }
    }

    std::array<uint8_t, kDeliveryHeaderBytes> delivery_header{};
    std::memcpy(delivery_header.data(), "RFD1", 4U);
    store_le32(delivery_header.data() + 4U, batch.provenance.frame_id);
    store_le32(delivery_header.data() + 8U, result.metadata.build_id);
    store_le32(delivery_header.data() + 12U, RNT_PACKETS_BYTES);
    uint32_t delivery_crc = crc32_update(0xffffffffU, delivery_header.data(), 16U);
    delivery_crc = crc32_update(delivery_crc, result.packets, sizeof(result.packets));
    store_le32(delivery_header.data() + 16U, ~delivery_crc);
    if (!write_all(output.fd, delivery_header.data(), delivery_header.size()) ||
        !write_all(output.fd, result.packets, sizeof(result.packets))) {
      return fail("could not write RFD1 result record");
    }
    ++received_frames;
  }

  mrf1_nf_stats stats{};
  mrf1_nf_get_stats(frontend.value, &stats);
  const uint64_t elapsed_ns = static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now() - started).count());
  std::cout << "{\"mode\":\"serial_finite_file_rxth_to_real_imbc\",\"production_pipelined\":false,\"requested_frames\":" << kFramesToReceive
            << ",\"input_rxth_blocks\":" << blocks
            << ",\"frontend_emitted_frames\":" << stats.emitted_batches
            << ",\"submitted_frames\":" << submitted_frames
            << ",\"received_rfd1_frames\":" << received_frames
            << ",\"dual_crc_valid_frames\":" << dual_crc_frames
            << ",\"payload_crc_rejected_frames\":" << crc_rejected_frames
            << ",\"fixture_expected_packets_verified\":" << (!options.expected_packets_path.empty() ? "true" : "false")
            << ",\"fixture_expected_packets_match\":" << (expected_packets_match ? "true" : "false")
            << ",\"elapsed_ns\":" << elapsed_ns
            << ",\"hardware_detector\":true,\"physical_rf\":false,\"rxth_dma\":false}\n";

  if (received_frames != kFramesToReceive) {
    std::cerr << "board_iq_replay: finite file did not yield 20 CRC-valid RFD1 records\n";
    return 6;
  }
  if (crc_rejected_frames != 0U) {
    std::cerr << "board_iq_replay: clean fixture replay observed a payload CRC rejection\n";
    return 6;
  }
  if (!expected_packets_match) {
    std::cerr << "board_iq_replay: received packets differ from expected comparison fixture\n";
    return 7;
  }
  return 0;
}
