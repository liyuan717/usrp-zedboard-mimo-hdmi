#include "hdmi_presenter/presenter.h"
namespace hdmi_presenter {
class NullBackend final : public PresentBackend {
 public:
  bool supports_physical_presentation() const override { return false; }
  std::string unavailable_reason() const override { return "null backend: no KMS, DMA EOF, vblank, or physical HDMI acknowledgement"; }
  int initial_displayed_slot() const override { return -1; }
  uint64_t mode_refresh_millihz() const override { return 0; }
  BackendSubmitResult submit(const SlotView&, uint64_t, std::string*) override { return BackendSubmitResult::kAccepted; }
  uint8_t* xrgb_slot(unsigned, size_t) override { return nullptr; }
  BackendAckResult wait_for_physical_ack(PhysicalAck*, int, std::string* e) override { if (e) *e="null backend never produces a physical acknowledgement"; return BackendAckResult::kFailure; }
  void shutdown() override {}
};
std::unique_ptr<PresentBackend> make_null_backend() { return std::make_unique<NullBackend>(); }
}  // namespace hdmi_presenter
