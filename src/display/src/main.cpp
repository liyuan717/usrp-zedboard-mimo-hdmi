#include "hdmi_presenter/app_runner.h"

#include <cerrno>
#include <cctype>
#include <csignal>
#include <cstdlib>
#include <limits>
#include <iostream>
#include <memory>
#include <string>
#include <unistd.h>

namespace {
void usage() {
  std::cerr << "usage: hdmi-presenter --backend null|kms (--tcp PORT | --codec-output-fd FD) [--max-records N] [--proof-json NEW_FILE] [--read-timeout-ms N] [--queue-wait-ms N] [--codec-feedback-fd FD --feedback-timeout-ms N] [--drain-timeout-ms N] [--repeat-displayed-on-idle] [--source-pts-pacing] [--stage-timing] [--rgb-expand scalar|neon]\n";
}
bool positive(const char* value, int* out) { char* end=nullptr; const long v=std::strtol(value,&end,10); if(!value[0] || *end || v<=0 || v>65535) return false; *out=static_cast<int>(v); return true; }
bool positive_timeout(const char* value, int* out) {
  char* end = nullptr;
  errno = 0;
  const long long v = std::strtoll(value, &end, 10);
  if (!value || !value[0] || errno == ERANGE || !end || *end || v <= 0 ||
      v > std::numeric_limits<int>::max()) return false;
  *out = static_cast<int>(v);
  return true;
}
bool nonnegative_fd(const char* value, int* out) { char* end=nullptr; const long v=std::strtol(value,&end,10); if(!value[0] || *end || v<0 || v>0x7fffffffL) return false; *out=static_cast<int>(v); return true; }
bool nonnegative_u64_decimal(const char* value, uint64_t* out) {
  if (!value || !value[0] || !out) return false;
  for (const unsigned char* p = reinterpret_cast<const unsigned char*>(value); *p; ++p)
    if (!std::isdigit(*p)) return false;
  errno = 0;
  char* end = nullptr;
  const unsigned long long parsed = std::strtoull(value, &end, 10);
  if (errno == ERANGE || !end || *end) return false;
  *out = static_cast<uint64_t>(parsed);
  return true;
}
void report(const hdmi_presenter::Metrics& m) {
  std::cout << "{\"input_records\":" << m.input_records << ",\"crc_verified\":" << m.crc_verified
            << ",\"queued\":" << m.queued << ",\"kms_submitted\":" << m.kms_submitted
            << ",\"eof_completed\":" << m.eof_completed << ",\"presented_at_hdmi_boundary\":" << m.presented_at_hdmi_boundary
            << ",\"visual_acceptance\":false,\"rejected\":" << m.rejected << ",\"queue_full\":" << m.queue_full
            << ",\"queue_high_watermark\":" << m.queue_high_watermark << ",\"scanout_repeats\":" << m.scanout_repeats
            << ",\"backend_failures\":" << m.backend_failures << ",\"proof_records\":" << m.proof_records
            << ",\"proof_failures\":" << m.proof_failures << "}\n";
}
}  // namespace
int main(int argc, char** argv) {
  std::string backend="null", proof_path; int port=0; int codec_fd=-1; hdmi_presenter::ReceiverConfig config;
  for(int i=1;i<argc;++i) { const std::string arg=argv[i]; if(arg=="--backend" && i+1<argc) backend=argv[++i]; else if(arg=="--tcp" && i+1<argc) { if(!positive(argv[++i],&port)) { usage(); return 2; } } else if(arg=="--codec-output-fd" && i+1<argc) { if(!nonnegative_fd(argv[++i],&codec_fd)) { usage(); return 2; } } else if(arg=="--codec-feedback-fd" && i+1<argc) { if(!nonnegative_fd(argv[++i],&config.codec_feedback_fd)) { usage(); return 2; } } else if(arg=="--rgb-expand" && i+1<argc) { if(!hdmi_presenter::parse_rgb_expand_mode(argv[++i],&config.rgb_expand)) { usage(); return 2; } } else if(arg=="--max-records" && i+1<argc) { if(!nonnegative_u64_decimal(argv[++i],&config.max_records)) { usage(); return 2; } } else if(arg=="--proof-json" && i+1<argc) { proof_path=argv[++i]; if(proof_path.empty()) { usage(); return 2; } } else if(arg=="--read-timeout-ms" && i+1<argc) { if(!positive_timeout(argv[++i],&config.read_timeout_ms)) { usage(); return 2; } } else if(arg=="--queue-wait-ms" && i+1<argc) { if(!positive_timeout(argv[++i],&config.queue_wait_ms)) { usage(); return 2; } } else if(arg=="--feedback-timeout-ms" && i+1<argc) { if(!positive_timeout(argv[++i],&config.feedback_timeout_ms)) { usage(); return 2; } } else if(arg=="--drain-timeout-ms" && i+1<argc) { if(!positive_timeout(argv[++i],&config.drain_timeout_ms)) { usage(); return 2; } } else if(arg=="--repeat-displayed-on-idle") { config.repeat_displayed_on_idle=true; } else if(arg=="--source-pts-pacing") { config.source_pts_pacing=true; } else if(arg=="--stage-timing") { config.stage_timing=true; } else { usage(); return 2; } }
  if(config.source_pts_pacing && !config.repeat_displayed_on_idle) { std::cerr << "--source-pts-pacing requires --repeat-displayed-on-idle\n"; return 2; }
  if((port==0)==(codec_fd<0)) { usage(); return 2; }
  if (codec_fd >= 0 && config.max_records != 0) { std::cerr << "--max-records is invalid with --codec-output-fd; require helper STOP_ACK\n"; return 2; }
  if (codec_fd < 0 && config.codec_feedback_fd >= 0) { std::cerr << "--codec-feedback-fd requires --codec-output-fd\n"; return 2; }
  /* Feedback is a best-effort bounded pipe.  EPIPE must report a normal
   * receive failure, never terminate the presenter before fail-closed logic. */
  if (config.codec_feedback_fd >= 0) std::signal(SIGPIPE, SIG_IGN);
  std::unique_ptr<hdmi_presenter::PresentBackend> display = backend=="null" ? hdmi_presenter::make_null_backend() : backend=="kms" ? hdmi_presenter::make_drm_backend("/dev/dri/card0") : nullptr;
  if(!display) { usage(); return 2; }
  if(backend=="kms" && !display->supports_physical_presentation()) { std::cerr << display->unavailable_reason() << "\n"; return 3; }
  if (codec_fd >= 0) {
    const auto run = hdmi_presenter::run_codec_presenter_session(std::move(display), codec_fd, config, proof_path);
    report(run.metrics);
    if (!run.stage_timing_summary.empty()) std::cerr << run.stage_timing_summary << "\n";
    if (!run.error.empty()) std::cerr << "input error: " << run.error << "\n";
    if (!proof_path.empty() && run.proof_status != "written")
      std::cerr << "proof error: " << run.proof_status << "\n";
    return run.status == 0 ? 0 : 1;
  }
  hdmi_presenter::Presenter presenter(std::move(display));
  presenter.set_stage_timing_enabled(config.stage_timing);
  presenter.set_repeat_displayed_on_idle(config.repeat_displayed_on_idle);
  presenter.set_source_pts_pacing(config.source_pts_pacing);
  presenter.set_rgb_expand_mode(config.rgb_expand);
  std::string error;
  if(!proof_path.empty() && !presenter.set_proof_capture_path(proof_path,&error)) { std::cerr << error << "\n"; return 2; }
  presenter.start();
  int status=0;
  if(port) status=hdmi_presenter::serve_tcp(static_cast<uint16_t>(port),&presenter,config,&error);
  if (status == 0 && !presenter.drain(config.drain_timeout_ms, &error)) status = 1;
  /* A receiver/protocol/drain error is outside the display worker.  Mark a
   * requested proof invalid before stop serializes it, even if earlier DRM
   * records happened to look complete. */
  if (status != 0) presenter.invalidate_capture(error);
  presenter.stop(); report(presenter.metrics());
  if (config.stage_timing) std::cerr << presenter.stage_timing_summary_json() << "\n";
  if(!proof_path.empty() && presenter.proof_status() != "written") {
    std::cerr << "proof error: " << presenter.proof_status() << "\n";
    status=1;
  }
  if(!error.empty()) std::cerr << "input error: " << error << "\n";
  return status==0?0:1;
}
