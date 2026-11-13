// G0 native prototype: persistent decoder, direct presenter fill, physical ACK.
// This local fixture path is not an RF implementation or a G1 loss-recovery claim.
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/frame.h>
#include "yuv420p_rgb565.h"
}
#include "hdmi_presenter/presenter.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <time.h>
#include <sys/utsname.h>

using namespace hdmi_presenter;
using Clock = std::chrono::steady_clock;
static double us(Clock::time_point start) {
  return std::chrono::duration<double, std::micro>(Clock::now() - start).count();
}
static double cpu_seconds() {
  timespec t{};
  if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t)) throw std::runtime_error("process clock");
  return t.tv_sec + t.tv_nsec / 1e9;
}
static void stats(std::ostream& out, const std::vector<double>& values) {
  auto sorted = values;
  std::sort(sorted.begin(), sorted.end());
  double sum = 0;
  for (auto v : sorted) sum += v;
  auto q = [&](double p) { return sorted.empty() ? 0 : sorted[static_cast<size_t>(std::ceil(p * sorted.size())) - 1]; };
  out << "{\"count\":" << sorted.size() << ",\"mean\":" << (sorted.empty() ? 0 : sum / sorted.size())
      << ",\"p95\":" << q(.95) << ",\"p99\":" << q(.99) << ",\"max\":" << q(1) << "}";
}

struct Input { uint64_t id, capture_us; uint32_t known_damaged; std::vector<uint8_t> data; };
static std::vector<Input> read_input(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("input open");
  std::vector<Input> inputs;
  size_t total = 0;
  std::array<uint8_t, 32> h{};
  while (f.read(reinterpret_cast<char*>(h.data()), h.size())) {
    if (std::memcmp(h.data(), "V3AU", 4) || load_le16(h.data() + 4) != 1 || load_le16(h.data() + 6) != 32)
      throw std::runtime_error("fixture header");
    uint32_t n = load_le32(h.data() + 24);
    if (!n || n > (4U << 20) || inputs.size() >= 4096 || (total += n) > (64U << 20))
      throw std::runtime_error("fixture bound");
    const uint32_t known_damaged = load_le32(h.data() + 28);
    if (known_damaged > 1) throw std::runtime_error("fixture damage boolean");
    Input in{load_le64(h.data() + 8), load_le64(h.data() + 16), known_damaged, std::vector<uint8_t>(n)};
    if (!inputs.empty() && (in.id <= inputs.back().id || in.capture_us <= inputs.back().capture_us))
      throw std::runtime_error("fixture identity order");
    if (!f.read(reinterpret_cast<char*>(in.data.data()), n)) throw std::runtime_error("fixture truncation");
    inputs.push_back(std::move(in));
  }
  if (!f.eof() || f.gcount() != 0 || inputs.empty()) throw std::runtime_error("fixture tail");
  return inputs;
}

int main(int argc, char** argv) {
  AVCodecContext* ctx = nullptr;
  AVPacket* packet = nullptr;
  AVFrame* frame = nullptr;
  try {
    if (argc != 8) throw std::runtime_error("usage: g0-decode-display INPUT REPORT THREADS slice|frame drm|null decode|pixels|display paced|fast");
    const auto inputs = read_input(argv[1]);
    int threads = std::stoi(argv[3]);
    if (threads < 1 || threads > 2) throw std::runtime_error("threads must be 1 or 2");
    const bool physical = std::string(argv[5]) == "drm";
    const std::string mode = argv[6];
    const bool pixels = mode != "decode", display = mode == "display", paced = std::string(argv[7]) == "paced";
    if ((std::string(argv[4]) != "slice" && std::string(argv[4]) != "frame") ||
        (std::string(argv[5]) != "drm" && std::string(argv[5]) != "null") ||
        (mode != "decode" && mode != "pixels" && mode != "display") ||
        (std::string(argv[7]) != "paced" && std::string(argv[7]) != "fast") || (physical && !display))
      throw std::runtime_error("invalid mode");
    ctx = avcodec_alloc_context3(avcodec_find_decoder(AV_CODEC_ID_H264));
    if (!ctx) throw std::runtime_error("decoder allocation");
    ctx->thread_count = threads;
    ctx->thread_type = std::string(argv[4]) == "slice" ? FF_THREAD_SLICE : FF_THREAD_FRAME;
    ctx->flags |= AV_CODEC_FLAG_OUTPUT_CORRUPT;
    ctx->err_recognition = AV_EF_CAREFUL;
    if (avcodec_open2(ctx, ctx->codec, nullptr) < 0) throw std::runtime_error("decoder open");
    packet = av_packet_alloc(); frame = av_frame_alloc();
    if (!packet || !frame) throw std::runtime_error("frame allocation");
    Presenter presenter(physical ? make_drm_backend("/dev/dri/card0") : make_null_backend());
    std::string error;
    if (display) {
      presenter.set_rgb_expand_mode(RgbExpandMode::kNeon);
      presenter.set_repeat_displayed_on_idle(true);
      presenter.set_stage_timing_enabled(true);
      if (!presenter.set_proof_capture_path(std::string(argv[2]) + ".physical.json", &error))
        throw std::runtime_error(error);
      presenter.start();
    }
    std::vector<uint8_t> rgb(pixels && !display ? kRgb565Bytes : 0);
    std::vector<double> decode_us, convert_us, queue_us, crc_us;
    std::vector<std::pair<uint64_t, uint32_t>> output_crc;
    unsigned decoded = 0, damaged = 0, decode_errors = 0;
    unsigned known_damaged_aus = 0, packet_corrupt_marked_aus = 0, frame_corrupt_flagged = 0;
    auto start = Clock::now();
    double cpu_start = cpu_seconds();
    auto receive = [&]() {
      for (;;) {
        auto t = Clock::now();
        int ret = avcodec_receive_frame(ctx, frame);
        decode_us.push_back(us(t));
        if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) break;
        if (ret < 0) { ++decode_errors; break; }
        if (frame->width != 1024 || frame->height != 600 || frame->format != AV_PIX_FMT_YUV420P)
          throw std::runtime_error("unnegotiated video format");
        if (frame->pts < 0) throw std::runtime_error("lost source identity");
        ++decoded;
        damaged += frame->decode_error_flags != 0;
        frame_corrupt_flagged += (frame->flags & AV_FRAME_FLAG_CORRUPT) != 0;
        if (pixels) {
          FillSlot fill{};
          t = Clock::now();
          if (display && presenter.begin_fill(250, &fill, &error) != SubmitResult::kQueued)
            throw std::runtime_error("presenter fill: " + error);
          queue_us.push_back(us(t));
          uint8_t* dst = display ? fill.rgb565 : rgb.data();
          yuv420p_rgb565_image image{frame->data[0], frame->data[1], frame->data[2],
            static_cast<size_t>(frame->linesize[0]), static_cast<size_t>(frame->linesize[1]),
            static_cast<size_t>(frame->linesize[2]), 1024, 600};
          t = Clock::now();
          if (yuv420p_to_rgb565_neon(&image, dst, 2048) != YUV420P_RGB565_OK)
            throw std::runtime_error("NEON conversion");
          convert_us.push_back(us(t));
          t = Clock::now();
          uint32_t crc = ieee_crc32(dst, kRgb565Bytes);
          crc_us.push_back(us(t));
          output_crc.emplace_back(static_cast<uint64_t>(frame->pts), crc);
          if (display) {
            presenter.note_input_record();
            auto found = std::find_if(inputs.begin(), inputs.end(), [&](const Input& in) {
              return in.id == static_cast<uint64_t>(frame->pts);
            });
            if (found == inputs.end()) throw std::runtime_error("unknown frame id");
            FrameIdentity id{3, found->id, found->capture_us * 1000, crc};
            if (presenter.finish_fill(fill, id, &error) != SubmitResult::kQueued)
              throw std::runtime_error("presenter finish: " + error);
          }
        }
        av_frame_unref(frame);
      }
    };
    for (const auto& in : inputs) {
      if (paced) std::this_thread::sleep_until(start + std::chrono::microseconds(in.capture_us - inputs.front().capture_us));
      if (av_new_packet(packet, static_cast<int>(in.data.size())) < 0) throw std::runtime_error("packet allocation");
      std::memcpy(packet->data, in.data.data(), in.data.size());
      packet->pts = static_cast<int64_t>(in.id);
      if (in.known_damaged) {
        ++known_damaged_aus;
        packet->flags |= AV_PKT_FLAG_CORRUPT;
        packet_corrupt_marked_aus += (packet->flags & AV_PKT_FLAG_CORRUPT) != 0;
      }
      auto t = Clock::now();
      int ret = avcodec_send_packet(ctx, packet);
      decode_us.push_back(us(t));
      if (ret == AVERROR(EAGAIN)) {
        receive(); t = Clock::now(); ret = avcodec_send_packet(ctx, packet); decode_us.push_back(us(t));
      }
      av_packet_unref(packet);
      if (ret < 0) ++decode_errors;
      receive();
    }
    if (avcodec_send_packet(ctx, nullptr) < 0) throw std::runtime_error("decoder flush");
    receive();
    if (display && !presenter.drain(3000, &error)) throw std::runtime_error("presenter drain: " + error);
    const double wall_seconds = us(start) / 1e6, cpu_used = cpu_seconds() - cpu_start;
    const auto metrics = presenter.metrics();
    if (display && !presenter.stop(&error)) throw std::runtime_error("presenter stop: " + error);
    utsname machine{}; uname(&machine);
    std::ofstream out(argv[2]);
    out << "{\"schema\":\"v3-g0-native-v1\",\"machine\":\"" << machine.machine
        << "\",\"kernel\":\"" << machine.release << "\",\"libavcodec_version\":" << avcodec_version()
        << ",\"threads\":" << threads << ",\"active_thread_type\":" << ctx->active_thread_type
        << ",\"physical\":" << (physical ? "true" : "false")
        << ",\"paced\":" << (paced ? "true" : "false") << ",\"input_aus\":" << inputs.size()
        << ",\"decoded_frames\":" << decoded << ",\"damaged_frames\":" << damaged
        << ",\"known_damaged_input_aus\":" << known_damaged_aus
        << ",\"packet_corrupt_marked_aus\":" << packet_corrupt_marked_aus
        << ",\"frame_corrupt_flagged\":" << frame_corrupt_flagged
        << ",\"input_struct_bytes\":" << sizeof(Input)
        << ",\"decode_errors\":" << decode_errors << ",\"wall_seconds\":" << wall_seconds
        << ",\"cpu_seconds\":" << cpu_used << ",\"cpu_one_core_percent\":" << cpu_used / wall_seconds * 100
        << ",\"decoded_fps\":" << decoded / wall_seconds
        << ",\"physical_frames\":" << metrics.presented_at_hdmi_boundary
        << ",\"physical_span_fps\":";
    if (metrics.presented_at_hdmi_boundary > 1 && metrics.last_physical_ack_ns > metrics.first_physical_ack_ns)
      out << (metrics.presented_at_hdmi_boundary - 1) * 1e9 / (metrics.last_physical_ack_ns - metrics.first_physical_ack_ns);
    else out << "null";
    out << ",\"queue_high_watermark\":" << metrics.queue_high_watermark << ",\"decode_call_us\":"; stats(out, decode_us);
    out << ",\"convert_us\":"; stats(out, convert_us);
    out << ",\"fill_wait_us\":"; stats(out, queue_us);
    out << ",\"pixel_crc_us\":"; stats(out, crc_us);
    out << ",\"presenter_timing\":" << presenter.stage_timing_summary_json() << ",\"pixels\":[";
    for (size_t i = 0; i < output_crc.size(); ++i) {
      if (i) out << ',';
      out << "{\"capture_id\":" << output_crc[i].first << ",\"rgb565_crc32\":" << output_crc[i].second << '}';
    }
    out << "],\"g0_passed\":false}\n";
    if (!out) throw std::runtime_error("report write");
    av_frame_free(&frame); av_packet_free(&packet); avcodec_free_context(&ctx);
    return decoded == inputs.size() && !decode_errors ? 0 : 2;
  } catch (const std::exception& e) {
    std::cerr << "G0_ERROR: " << e.what() << '\n';
    av_frame_free(&frame); av_packet_free(&packet); avcodec_free_context(&ctx);
    return 1;
  }
}
