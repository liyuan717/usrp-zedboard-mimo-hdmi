#pragma once

#include "hdmi_presenter/receiver.h"

#include <memory>
#include <string>

namespace hdmi_presenter {

/* Shared codec-mode application lifecycle.  The production CLI uses this
 * directly; host tests may inject a PresentBackend without adding a fake
 * backend switch to the deployed command line. */
struct CodecSessionResult {
  int status{};
  ReceiverResult receiver{};
  Metrics metrics{};
  std::string error;
  std::string proof_status;
  std::string stage_timing_summary;
};

CodecSessionResult run_codec_presenter_session(std::unique_ptr<PresentBackend> backend,
                                               int codec_output_fd,
                                               const ReceiverConfig& config,
                                               const std::string& proof_path);

}  // namespace hdmi_presenter
