#include "hdmi_presenter/drm_uapi_min.h"
#include "hdmi_presenter/presenter.h"

#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <poll.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>
#include <vector>

namespace hdmi_presenter {
namespace d = drm_uapi;
namespace {
bool io(int fd, unsigned long command, void* argument, std::string* error) {
  if (ioctl(fd, command, argument) == 0) return true;
  if (error) *error = std::strerror(errno);
  return false;
}

/* Accept the monitor-advertised 59.x Hz nominal-60 mode; report its exact
 * timing. This is mode eligibility, never a source-video frame-rate pass. */
bool acceptable_mode(const d::ModeInfo& mode) {
  return mode.hdisplay == kWidth && mode.vdisplay == kHeight &&
         !(mode.flags & d::kModeInterlace) && mode.clock != 0 &&
         mode.htotal != 0 && mode.vtotal != 0 &&
         static_cast<uint64_t>(mode.clock) * 1000U >=
             59U * static_cast<uint64_t>(mode.htotal) * mode.vtotal;
}

uint64_t refresh_millihz(const d::ModeInfo& mode) {
  return static_cast<uint64_t>(mode.clock) * 1000000U /
         (static_cast<uint64_t>(mode.htotal) * mode.vtotal);
}

bool closer_to_60hz(const d::ModeInfo& candidate, const d::ModeInfo& current) {
  constexpr uint64_t kTargetMillihz = 60000;
  const uint64_t candidate_rate = refresh_millihz(candidate);
  const uint64_t candidate_delta = candidate_rate >= kTargetMillihz ? candidate_rate - kTargetMillihz : kTargetMillihz - candidate_rate;
  const uint64_t current_rate = refresh_millihz(current);
  const uint64_t current_delta = current_rate >= kTargetMillihz ? current_rate - kTargetMillihz : kTargetMillihz - current_rate;
  return candidate_delta < current_delta ||
         (candidate_delta == current_delta && refresh_millihz(candidate) < refresh_millihz(current));
}
}  // namespace

class DrmBackend final : public PresentBackend {
  struct Buf {
    uint32_t handle{};
    uint32_t fb{};
    uint32_t pitch{};
    uint64_t size{};
    void* map{};
  };

 public:
  explicit DrmBackend(const std::string& path) { init(path); }
  ~DrmBackend() override { shutdown(); }

  bool supports_physical_presentation() const override { return ready_; }
  std::string unavailable_reason() const override { return reason_; }
  int initial_displayed_slot() const override { return ready_ ? kBlackSlot : -1; }
  uint64_t mode_refresh_millihz() const override { return mode_refresh_millihz_; }

  uint8_t* xrgb_slot(unsigned index, size_t bytes) override {
    return ready_ && index < buffers_.size() && bytes == kXrgb8888Bytes
               ? static_cast<uint8_t*>(buffers_[index].map)
               : nullptr;
  }

  BackendSubmitResult submit(const SlotView& view, uint64_t token,
                             std::string* error) override {
    if (!ready_ || token == 0 || view.slot_index >= buffers_.size()) {
      if (error) *error = ready_ ? "invalid page-flip token or slot" : reason_;
      return BackendSubmitResult::kFailure;
    }
    // Waiting for the first decoded RGB frame must not enable HDMI scanout.
    // A failed RF burst with zero complete AUs then has no CRTC to retire.
    if (!activated_) {
      activation_attempted_ = true;
      if (!activate_black(error)) return BackendSubmitResult::kFailure;
      /* The legacy ADI KMS driver reports the initial SetCrtc commit as an
       * unmapped pending page flip until a vblank retires it. Give it two
       * complete frames at the advertised ~59.8 Hz mode before submitting the
       * first decoded frame. This is a modeset settle delay, not a video pace
       * claim, and it occurs only once when scanout is first activated. */
      timespec settle{0, 50000000};
      while (nanosleep(&settle, &settle) != 0 && errno == EINTR) {}
      d::PresentationInfo running{};
      if (!presentation_info(&running, true, error)) return BackendSubmitResult::kFailure;
      session_ = running.session;
      activated_ = true;
    }
    d::PageFlip flip{};
    flip.crtc_id = crtc_;
    flip.fb_id = buffers_[view.slot_index].fb;
    flip.flags = d::kFlipEvent;
    flip.user_data = token;
    if (ioctl(fd_, d::kPageFlip, &flip) == 0) return BackendSubmitResult::kAccepted;
    if (errno == EBUSY || errno == EAGAIN) return BackendSubmitResult::kBusy;
    if (error) *error = std::string("DRM page flip: ") + std::strerror(errno);
    return BackendSubmitResult::kFailure;
  }

  BackendAckResult wait_for_physical_ack(PhysicalAck* acknowledgement, int timeout_ms,
                                          std::string* error) override {
    if (!acknowledgement || timeout_ms < 0 || !ready_) {
      if (error) *error = "invalid DRM acknowledgement wait";
      return BackendAckResult::kFailure;
    }
    if (!events_.empty()) return dequeue_verified_ack(acknowledgement, error);
    pollfd descriptor{fd_, POLLIN, 0};
    int result = 0;
    do { result = poll(&descriptor, 1, timeout_ms); } while (result < 0 && errno == EINTR);
    if (result == 0) return BackendAckResult::kTimeout;
    if (result < 0 || (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL))) {
      if (error) *error = result < 0 ? std::strerror(errno) : "DRM event descriptor failure";
      return BackendAckResult::kFailure;
    }
    std::array<uint8_t, 4096> raw{};
    const ssize_t count = read(fd_, raw.data(), raw.size());
    if (count <= 0) {
      if (error) *error = count < 0 ? std::strerror(errno) : "DRM event EOF";
      return BackendAckResult::kFailure;
    }
    size_t offset = 0;
    while (offset < static_cast<size_t>(count)) {
      d::Event header{};
      if (static_cast<size_t>(count) - offset < sizeof(header)) {
        if (error) *error = "truncated DRM event header";
        return BackendAckResult::kFailure;
      }
      std::memcpy(&header, raw.data() + offset, sizeof(header));
      if (header.length < sizeof(header) || header.length > static_cast<size_t>(count) - offset) {
        if (error) *error = "malformed DRM event length";
        return BackendAckResult::kFailure;
      }
      if (header.type == d::kEventFlipComplete) {
        d::EventVBlank flip{};
        if (header.length < sizeof(flip)) {
          if (error) *error = "short DRM page-flip event";
          return BackendAckResult::kFailure;
        }
        std::memcpy(&flip, raw.data() + offset, sizeof(flip));
        if (flip.user_data == 0) {
          if (error) *error = "token zero is reserved for initial black";
          return BackendAckResult::kFailure;
        }
        events_.push_back({flip.user_data, flip.sequence, flip.tv_sec, flip.tv_usec, session_});
      }
      offset += header.length;
    }
    if (events_.empty()) return BackendAckResult::kTimeout;
    return dequeue_verified_ack(acknowledgement, error);
  }

  void shutdown() override {
    if (fd_ < 0) return;
    /* This synchronous CRTC replacement is the teardown boundary: it removes
     * all application FBs from scanout before userspace drops their handles. */
    if (activation_attempted_ && have_old_crtc_) {
      d::Crtc restore = old_crtc_;
      if (restore.mode_valid) {
        restore.set_connectors_ptr = reinterpret_cast<uint64_t>(&connector_);
        restore.count_connectors = 1;
      } else {
        restore.set_connectors_ptr = 0;
        restore.count_connectors = 0;
        restore.fb_id = 0;
        restore.mode_valid = 0;
      }
      (void)ioctl(fd_, d::kSetCrtc, &restore);
    }
    for (auto& buffer : buffers_) {
      if (buffer.map) munmap(buffer.map, buffer.size);
      if (buffer.fb) { uint32_t id = buffer.fb; (void)ioctl(fd_, d::kRmFb, &id); }
      if (buffer.handle) { d::DestroyDumb destroy{buffer.handle}; (void)ioctl(fd_, d::kDestroyDumb, &destroy); }
      buffer = {};
    }
    if (master_) (void)ioctl(fd_, d::kDropMaster, nullptr);
    close(fd_);
    fd_ = -1;
    master_ = false;
    ready_ = false;
    activated_ = false; activation_attempted_ = false;
  }

 private:
  BackendAckResult dequeue_verified_ack(PhysicalAck* acknowledgement, std::string* error) {
    if (events_.empty()) return BackendAckResult::kTimeout;
    /* The raw DRM event gives the token and timing, but not its observer
     * generation. Sample GETINFO once per physical event (never per repeat or
     * framebuffer read) so a driver restart cannot be mistaken for a valid
     * acknowledgement from the earlier presentation session. */
    d::PresentationInfo current{};
    if (!presentation_info(&current, true, error)) return BackendAckResult::kFailure;
    if (!d::running_session_unchanged(current, session_)) {
      if (error) *error = "presentation observer session changed after page-flip event";
      return BackendAckResult::kFailure;
    }
    *acknowledgement = events_.front();
    acknowledgement->observer_session = current.session;
    events_.pop_front();
    return BackendAckResult::kAck;
  }

  bool presentation_info(d::PresentationInfo* info, bool require_ready, std::string* error) {
    *info = {};
    info->size = sizeof(*info);
    if (!io(fd_, d::kGetPresentationInfo, info, error)) return false;
    constexpr uint32_t required_caps = d::kCapRealVsyncEof | d::kCapTokenEvent | d::kCapRepeatFlag;
    if (info->abi_major != d::kAbiMajor || info->driver_magic != d::kMagic ||
        info->observer_id != d::kObserver || (info->capabilities & required_caps) != required_caps ||
        (info->status & d::kError) || (require_ready && (!(info->status & d::kReady) || info->session == 0))) {
      if (error) *error = "ADI HDMI presentation ABI/capability/state gate rejected DRM driver";
      return false;
    }
    return true;
  }

  bool discover(std::string* error) {
    d::CardRes resources{};
    if (!io(fd_, d::kGetResources, &resources, error)) return false;
    std::vector<uint32_t> fbs(resources.count_fbs);
    std::vector<uint32_t> encoders_all(resources.count_encoders);
    std::vector<uint32_t> connectors(resources.count_connectors);
    std::vector<uint32_t> crtcs(resources.count_crtcs);
    resources.fb_id_ptr = reinterpret_cast<uint64_t>(fbs.data());
    resources.encoder_id_ptr = reinterpret_cast<uint64_t>(encoders_all.data());
    resources.connector_id_ptr = reinterpret_cast<uint64_t>(connectors.data());
    resources.crtc_id_ptr = reinterpret_cast<uint64_t>(crtcs.data());
    if (!io(fd_, d::kGetResources, &resources, error)) return false;
    bool selected = false;
    for (uint32_t connector_id : connectors) {
      d::Connector connector{};
      connector.connector_id = connector_id;
      if (!io(fd_, d::kGetConnector, &connector, error) || connector.connection != d::kConnected) continue;
      std::vector<d::ModeInfo> modes(connector.count_modes);
      std::vector<uint32_t> encoders(connector.count_encoders);
      std::vector<uint32_t> properties(connector.count_props);
      std::vector<uint64_t> property_values(connector.count_props);
      connector.props_ptr = reinterpret_cast<uint64_t>(properties.data());
      connector.prop_values_ptr = reinterpret_cast<uint64_t>(property_values.data());
      connector.modes_ptr = reinterpret_cast<uint64_t>(modes.data());
      connector.encoders_ptr = reinterpret_cast<uint64_t>(encoders.data());
      if (!io(fd_, d::kGetConnector, &connector, error)) continue;
      std::vector<d::ModeInfo> candidates = modes;
      if (candidates.empty()) {
        /* Some sinks stop exposing EDID while HDMI is inactive. The runtime
         * owner explicitly authorized this only-under-training-wheels
         * fallback from the previously captured EDID of the same display. */
        d::ModeInfo fallback{};
        fallback.clock = 50250;
        fallback.hdisplay = 1024;
        fallback.hsync_start = 1068;
        fallback.hsync_end = 1156;
        fallback.htotal = 1344;
        fallback.vdisplay = 600;
        fallback.vsync_start = 603;
        fallback.vsync_end = 609;
        fallback.vtotal = 625;
        fallback.vrefresh = 60;
        fallback.flags = 5;
        fallback.type = d::kModePreferred;
        std::strncpy(fallback.name, "1024x600-forced", sizeof(fallback.name) - 1U);
        candidates.push_back(fallback);
      }
      for (const auto& candidate : candidates) {
        if (!acceptable_mode(candidate) || (selected && !closer_to_60hz(candidate, mode_))) continue;
        const uint32_t encoder_id = connector.encoder_id ? connector.encoder_id :
                                    (encoders.empty() ? 0 : encoders.front());
        if (!encoder_id) continue;
        d::Encoder encoder{};
        encoder.encoder_id = encoder_id;
        if (!io(fd_, d::kGetEncoder, &encoder, error)) continue;
        uint32_t selected_crtc = encoder.crtc_id;
        if (!selected_crtc) {
          for (size_t i = 0; i < crtcs.size(); ++i)
            if (encoder.possible_crtcs & (1U << i)) { selected_crtc = crtcs[i]; break; }
        }
        if (!selected_crtc) continue;
        d::Crtc crtc{};
        crtc.crtc_id = selected_crtc;
        if (!io(fd_, d::kGetCrtc, &crtc, error)) continue;
        connector_ = connector_id;
        crtc_ = selected_crtc;
        old_crtc_ = crtc;
        have_old_crtc_ = true;
        mode_ = candidate;
        mode_refresh_millihz_ = refresh_millihz(candidate);
        selected = true;
      }
    }
    if (!selected && error) *error = "no connected progressive EDID mode exactly 1024x600 at >=59 Hz for one-flip diagnostic path";
    return selected;
  }

  bool allocate(std::string* error) {
    constexpr uint32_t expected_pitch = kWidth * 4;
    for (auto& buffer : buffers_) {
      d::CreateDumb create{};
      create.width = kWidth;
      create.height = kHeight;
      create.bpp = 32;
      if (!io(fd_, d::kCreateDumb, &create, error)) return false;
      if (create.pitch != expected_pitch || create.size < kXrgb8888Bytes) {
        if (error) *error = "DRM dumb-buffer pitch is not the required packed 4096-byte stride";
        return false;
      }
      buffer.handle = create.handle;
      buffer.pitch = create.pitch;
      buffer.size = create.size;
      d::FbCmd2 fb{};
      fb.width = kWidth;
      fb.height = kHeight;
      fb.pixel_format = d::kXrgb8888;
      fb.handles[0] = create.handle;
      fb.pitches[0] = create.pitch;
      if (!io(fd_, d::kAddFb2, &fb, error)) return false;
      buffer.fb = fb.fb_id;
      d::MapDumb map{};
      map.handle = create.handle;
      if (!io(fd_, d::kMapDumb, &map, error)) return false;
      buffer.map = mmap(nullptr, buffer.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, map.offset);
      if (buffer.map == MAP_FAILED) {
        buffer.map = nullptr;
        if (error) *error = std::strerror(errno);
        return false;
      }
      std::memset(buffer.map, 0, buffer.size);
    }
    return true;
  }

  bool activate_black(std::string* error) {
    d::Crtc set{};
    set.crtc_id = crtc_;
    set.fb_id = buffers_[kBlackSlot].fb;
    set.set_connectors_ptr = reinterpret_cast<uint64_t>(&connector_);
    set.count_connectors = 1;
    set.mode_valid = 1;
    set.mode = mode_;
    return io(fd_, d::kSetCrtc, &set, error);
  }

  void init(const std::string& path) {
    fd_ = open(path.c_str(), O_RDWR | O_CLOEXEC);
    if (fd_ < 0) { reason_ = "open " + path + ": " + std::strerror(errno); return; }
    if (ioctl(fd_, d::kSetMaster, nullptr) != 0) {
      reason_ = std::string("cannot acquire DRM master: ") + std::strerror(errno);
      shutdown();
      return;
    }
    master_ = true;
    d::PresentationInfo discovery{};
    /* OFF/PREPARED is valid here: capability discovery must precede the
     * first black modeset. READY/session are sampled only afterwards. */
    if (!presentation_info(&discovery, false, &reason_) || !discover(&reason_) ||
        !allocate(&reason_)) {
      shutdown();
      return;
    }
    // Capability/EDID/allocated slots are ready; physical READY/session must
    // still be checked at the first actual frame submission and every ACK.
    ready_ = true;
  }

  static constexpr unsigned kBlackSlot = 2;
  int fd_{-1};
  bool ready_{false};
  bool activated_{false}, activation_attempted_{false};
  bool master_{false};
  bool have_old_crtc_{false};
  std::string reason_{"not initialized"};
  uint32_t connector_{};
  uint32_t crtc_{};
  d::Crtc old_crtc_{};
  d::ModeInfo mode_{};
  uint64_t mode_refresh_millihz_{};
  uint64_t session_{};
  std::array<Buf, 3> buffers_{};
  std::deque<PhysicalAck> events_{};
};

std::unique_ptr<PresentBackend> make_drm_backend(const std::string& path) {
  return std::make_unique<DrmBackend>(path);
}
}  // namespace hdmi_presenter
