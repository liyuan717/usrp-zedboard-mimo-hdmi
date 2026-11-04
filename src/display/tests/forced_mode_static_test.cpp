#include "hdmi_presenter/presenter.h"

#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>

using namespace hdmi_presenter;

namespace {
void write_proof(const std::string& path, const std::string& json) {
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  output << json << '\n';
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 5 || std::string(argv[1]) != "--proof") return 2;
  const std::string proof_path = argv[2];
  const int hold_ms = std::stoi(argv[3]);
  const std::string card = argv[4];
  auto backend = make_drm_backend(card);
  if (!backend->supports_physical_presentation()) {
    write_proof(proof_path,
                "{\"schema\":\"forced-mode-static-hdmi-v1\",\"success\":false,"
                "\"failure\":\"" + backend->unavailable_reason() + "\"}");
    return 3;
  }
  uint8_t* pixels = backend->xrgb_slot(0, kXrgb8888Bytes);
  if (pixels == nullptr) {
    write_proof(proof_path,
                "{\"schema\":\"forced-mode-static-hdmi-v1\",\"success\":false,"
                "\"failure\":\"framebuffer slot unavailable\"}");
    return 4;
  }
  auto* words = reinterpret_cast<uint32_t*>(pixels);
  const uint32_t bars[] = {0x00ffffffU, 0x0000ffffU, 0x00ffff00U,
                           0x0000ff00U, 0x00ff00ffU, 0x00ff0000U};
  for (unsigned y = 0; y != kHeight; ++y) {
    for (unsigned x = 0; x != kWidth; ++x) {
      words[y * kWidth + x] = bars[(x * 6U) / kWidth];
    }
  }
  FrameIdentity identity{};
  identity.session_id = 0x46524345U;
  identity.frame_id = 1;
  identity.pts_ns = 0;
  identity.payload_crc32 = 0;
  SlotView view{pixels, kXrgb8888Bytes, identity, 0};
  std::string error;
  const auto submitted = backend->submit(view, 77U, &error);
  if (submitted != BackendSubmitResult::kAccepted) {
    write_proof(proof_path,
                "{\"schema\":\"forced-mode-static-hdmi-v1\",\"success\":false,"
                "\"submit_error\":\"" + error + "\"}");
    backend->shutdown();
    return 5;
  }
  PhysicalAck ack{};
  const auto acknowledged =
      backend->wait_for_physical_ack(&ack, 5000, &error);
  if (acknowledged != BackendAckResult::kAck) {
    write_proof(proof_path,
                "{\"schema\":\"forced-mode-static-hdmi-v1\",\"success\":false,"
                "\"ack_error\":\"" + error + "\"}");
    backend->shutdown();
    return 6;
  }
  write_proof(
      proof_path,
      "{\"schema\":\"forced-mode-static-hdmi-v1\",\"success\":true,"
      "\"physical_ack\":true,\"token\":" + std::to_string(ack.token) +
          ",\"drm_sequence\":" + std::to_string(ack.drm_sequence) +
          ",\"observer_session\":" + std::to_string(ack.observer_session) +
          ",\"mode_refresh_millihz\":" +
          std::to_string(backend->mode_refresh_millihz()) + "}");
  std::this_thread::sleep_for(std::chrono::milliseconds(hold_ms));
  backend->shutdown();
  std::cout << "FORCED_MODE_STATIC_HDMI_PASS token=" << ack.token << '\n';
  return 0;
}
