#include "hdmi_presenter/app_runner.h"

namespace hdmi_presenter {

CodecSessionResult run_codec_presenter_session(std::unique_ptr<PresentBackend> backend,
                                               int codec_output_fd,
                                               const ReceiverConfig& config,
                                               const std::string& proof_path) {
  CodecSessionResult result;
  if (!backend) {
    result.status = 2;
    result.error = "presenter backend is null";
    return result;
  }
  Presenter presenter(std::move(backend));
  presenter.set_stage_timing_enabled(config.stage_timing);
  presenter.set_repeat_displayed_on_idle(config.repeat_displayed_on_idle);
  presenter.set_source_pts_pacing(config.source_pts_pacing);
  presenter.set_rgb_expand_mode(config.rgb_expand);
  if (!proof_path.empty() && !presenter.set_proof_capture_path(proof_path, &result.error)) {
    result.status = 2;
    result.metrics = presenter.metrics();
    result.proof_status = presenter.proof_status();
    return result;
  }
  presenter.start();
  result.receiver = receive_records(codec_output_fd, InputKind::kCodecOutputFd, &presenter, config);
  if (!result.receiver.error.empty()) {
    result.status = 1;
    result.error = result.receiver.error;
  } else if (!result.receiver.saw_stop_ack) {
    result.status = 1;
    result.error = "codec stream ended without STOP_ACK";
  } else if (!presenter.drain(config.drain_timeout_ms, &result.error)) {
    result.status = 1;
  }
  /* This is the application boundary: parser, helper-terminal, and drain
   * errors are not necessarily visible to the presenter worker. */
  if (result.status != 0) presenter.invalidate_capture(result.error);
  presenter.stop();
  result.metrics = presenter.metrics();
  result.proof_status = presenter.proof_status();
  if (config.stage_timing) result.stage_timing_summary = presenter.stage_timing_summary_json();
  if (!proof_path.empty() && result.proof_status != "written") {
    if (result.error.empty()) result.error = "proof error: " + result.proof_status;
    result.status = 1;
  }
  return result;
}

}  // namespace hdmi_presenter
