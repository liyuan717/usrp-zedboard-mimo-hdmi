// G0 finite dual-CBX measurement endpoint; no legacy RF protocol/parser.
// Adapted from planning-commit host/windows_usrp/uhd_tx_windows.cpp transport only.
// A device is opened only with --enable-tx. Input is never loaded as a file-wide vector.
#include <uhd/types/metadata.hpp>
#include <uhd/types/tune_request.hpp>
#include <uhd/usrp/multi_usrp.hpp>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <condition_variable>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <cerrno>
#include <fcntl.h>
#include <poll.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

namespace {
constexpr double kRate = 4e6;
constexpr double kMinFreq = 5.5e9;
constexpr double kMaxFreq = 5.5e9;
constexpr size_t kBlockSamples = 16384;
constexpr float kInputComponentLimit = 0.12501F;

uint64_t monotonic_ns(bool raw=false) {
    timespec stamp{};
    if (clock_gettime(raw?CLOCK_MONOTONIC_RAW:CLOCK_MONOTONIC, &stamp) != 0) return 0;
    return uint64_t(stamp.tv_sec) * 1000000000 + stamp.tv_nsec;
}

struct Options {
    std::string iq_path;
    std::string address = "192.168.10.2";
    double frequency = 5.5e9;
    double gain = 0.0;
    std::optional<double> rf0_gain;
    double lo_offset = 0.0;
    uint64_t tx_max_samples = 0;
    bool enable_tx = false;
    bool observe_clock_end = false;
    bool raw_clock = false;
    bool swap_iq_channels = false;
};

Options parse(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string item = argv[i];
        if (item == "--enable-tx") {
            options.enable_tx = true;
        } else if (item == "--observe-clock-end") {
            options.observe_clock_end = true;
        } else if (item == "--raw-clock") {
            options.raw_clock = true;
        } else if (item == "--swap-iq-channels") {
            options.swap_iq_channels = true;
        } else if ((item == "--iq" || item == "--addr" || item == "--freq-hz" || item == "--gain-db" || item == "--rf0-gain-db" || item == "--tx-lo-offset-hz" || item == "--max-samples") && i + 1 < argc) {
            const std::string value = argv[++i];
            if (item == "--iq") options.iq_path = value;
            else if (item == "--addr") options.address = value;
            else if (item == "--freq-hz") options.frequency = std::stod(value);
            else if (item == "--gain-db") options.gain = std::stod(value);
            else if (item == "--rf0-gain-db") options.rf0_gain = std::stod(value);
            else if (item == "--tx-lo-offset-hz") options.lo_offset = std::stod(value);
            else options.tx_max_samples = std::stoull(value);
        } else {
            throw std::runtime_error("usage: g0_uhd_probe --iq FILE [--addr IP] [--freq-hz HZ] [--gain-db DB] [--rf0-gain-db DB] [--tx-lo-offset-hz HZ] [--max-samples N] [--observe-clock-end] [--swap-iq-channels] --enable-tx");
        }
    }
    if (options.iq_path.empty()) throw std::runtime_error("--iq is required");
    if (!std::isfinite(options.frequency) || options.frequency < kMinFreq || options.frequency > kMaxFreq) throw std::runtime_error("G0 frequency must equal user-confirmed 5.5 GHz");
    if (!std::isfinite(options.gain) || options.gain < 0.0 || options.gain > 18.0) throw std::runtime_error("G0 gain must be within user-confirmed 0..18 dB");
    if (options.rf0_gain && (!std::isfinite(*options.rf0_gain) || *options.rf0_gain < 0.0 || *options.rf0_gain > options.gain))
        throw std::runtime_error("RF0 gain must be within 0 dB and the requested shared gain");
    if (!std::isfinite(options.lo_offset) || std::abs(options.lo_offset) > 1e6) throw std::runtime_error("G0 TX LO offset must be within +/-1 MHz");
    if (options.enable_tx && (options.tx_max_samples == 0 || options.tx_max_samples > 256000000)) {
        throw std::runtime_error("--enable-tx requires explicit finite --max-samples");
    }
    return options;
}

class IQReader {
public:
    explicit IQReader(const std::string& path) {
        fd_ = ::open(path.c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC);
        if (fd_ < 0) throw std::runtime_error("cannot open IQ input");
        struct stat status{};
        if (fstat(fd_, &status) || (!S_ISREG(status.st_mode) && !S_ISFIFO(status.st_mode))) {
            ::close(fd_); fd_ = -1;
            throw std::runtime_error("IQ input must be a regular file or bounded producer FIFO");
        }
    }
    ~IQReader() { if (fd_ >= 0) ::close(fd_); }
    IQReader(const IQReader&) = delete;
    IQReader& operator=(const IQReader&) = delete;

    size_t next(std::array<float, 4>* output, size_t capacity) {
        const size_t wanted = capacity * sizeof(std::array<float, 4>);
        size_t bytes = 0;
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
        while (bytes < wanted) {
            const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now()).count();
            if (left <= 0) throw std::runtime_error("IQ producer stalled for30seconds");
            struct pollfd ready{fd_, POLLIN, 0};
            const int n = ::poll(&ready, 1, static_cast<int>(left));
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0 || (ready.revents & (POLLERR | POLLNVAL))) throw std::runtime_error("IQ input poll failed or timed out");
            const ssize_t got = ::read(fd_, reinterpret_cast<char*>(output) + bytes, wanted - bytes);
            if (got < 0 && (errno == EINTR || errno == EAGAIN)) continue;
            if (got < 0) throw std::runtime_error("IQ input read failed");
            if (!got) break;
            bytes += static_cast<size_t>(got);
        }
        if (!bytes) return 0;
        if (bytes % sizeof(std::array<float, 4>) != 0) {
            throw std::runtime_error("IQ input ends inside an I0,Q0,I1,Q1 float32 sample");
        }
        const size_t samples = static_cast<size_t>(bytes / static_cast<std::streamsize>(sizeof(std::array<float, 4>)));
        for (size_t i = 0; i < samples; ++i) for (const float value : output[i]) {
            if (!std::isfinite(value) || std::abs(value) > kInputComponentLimit) {
                throw std::runtime_error("IQ input contains non-finite or out-of-contract component");
            }
        }
        return samples;
    }

private:
    int fd_ = -1;
};

bool fatal_async_event(const uhd::async_metadata_t::event_code_t event) {
    return event == uhd::async_metadata_t::EVENT_CODE_UNDERFLOW
        || event == uhd::async_metadata_t::EVENT_CODE_UNDERFLOW_IN_PACKET
        || event == uhd::async_metadata_t::EVENT_CODE_TIME_ERROR
        || event == uhd::async_metadata_t::EVENT_CODE_SEQ_ERROR
        || event == uhd::async_metadata_t::EVENT_CODE_SEQ_ERROR_IN_BURST;
}

const char* async_event_name(const uhd::async_metadata_t::event_code_t event) {
    switch (event) {
    case uhd::async_metadata_t::EVENT_CODE_UNDERFLOW: return "underflow";
    case uhd::async_metadata_t::EVENT_CODE_UNDERFLOW_IN_PACKET: return "underflow-in-packet";
    case uhd::async_metadata_t::EVENT_CODE_TIME_ERROR: return "time-error";
    case uhd::async_metadata_t::EVENT_CODE_SEQ_ERROR: return "sequence-error";
    case uhd::async_metadata_t::EVENT_CODE_SEQ_ERROR_IN_BURST: return "sequence-error-in-burst";
    default: return "unknown";
    }
}

class AsyncMonitor {
public:
    explicit AsyncMonitor(uhd::tx_streamer::sptr streamer) : streamer_(std::move(streamer)) {
        thread_ = std::thread([this] {
            while (!stop_.load()) {
                uhd::async_metadata_t metadata;
                if (!streamer_->recv_async_msg(metadata, 0.1)) continue;
                if (fatal_async_event(metadata.event_code)) {
                    std::lock_guard<std::mutex> guard(lock_);
                    failure_ = std::string("UHD asynchronous TX failure: ") + async_event_name(metadata.event_code);
                    completed_.notify_all();
                    return;
                }
                if (metadata.event_code == uhd::async_metadata_t::EVENT_CODE_BURST_ACK) {
                    std::lock_guard<std::mutex> guard(lock_);
                    if (metadata.channel < 2) ack_mask_ |= 1U << metadata.channel;
                    completed_.notify_all();
                }
            }
        });
    }
    ~AsyncMonitor() { stop_and_join(); }
    void stop_and_join() {
        stop_.store(true);
        if (thread_.joinable()) thread_.join();
    }
    void throw_if_failed() const {
        std::lock_guard<std::mutex> guard(lock_);
        if (!failure_.empty()) throw std::runtime_error(failure_);
    }
    void wait_for_burst_ack(double timeout_seconds) {
        std::unique_lock<std::mutex> guard(lock_);
        const bool done = completed_.wait_for(guard, std::chrono::duration<double>(timeout_seconds),
            [this] { return ack_mask_ == 3U || !failure_.empty(); });
        if (!failure_.empty()) throw std::runtime_error(failure_);
        if (!done) throw std::runtime_error("UHD burst completion not acknowledged on both channels");
        std::cout << "TX_BURST_ACK channel_mask=" << ack_mask_ << std::endl;
    }

private:
    uhd::tx_streamer::sptr streamer_;
    std::atomic<bool> stop_{false};
    mutable std::mutex lock_;
    std::condition_variable completed_;
    unsigned ack_mask_ = 0;
    std::string failure_;
    std::thread thread_;
};

void send_file(const Options& options) {
    std::cout << std::setprecision(15);
    IQReader reader(options.iq_path);
    auto usrp = uhd::usrp::multi_usrp::make("type=x300,addr=" + options.address);
    usrp->set_tx_subdev_spec(uhd::usrp::subdev_spec_t("A:0 B:0"));
    if (usrp->get_tx_num_channels() < 2) throw std::runtime_error("two X310 TX channels are required");
    for (size_t channel = 0; channel < 2; ++channel) {
        usrp->set_tx_rate(kRate, channel);
        const auto tune = usrp->set_tx_freq(uhd::tune_request_t(options.frequency, options.lo_offset), channel);
        if (std::abs(usrp->get_tx_freq(channel) - options.frequency) > 1.0)
            throw std::runtime_error("actual RF/DSP target differs from authorized 5.5GHz");
        std::cout << "TX_TUNE channel=" << channel
                  << " target_hz=" << options.frequency
                  << " rf_hz=" << tune.actual_rf_freq
                  << " dsp_hz=" << tune.actual_dsp_freq << std::endl;
        usrp->set_tx_bandwidth(40e6, channel);
        const double requested_gain = channel == 0 ? options.rf0_gain.value_or(options.gain) : options.gain;
        usrp->set_tx_gain(requested_gain, channel);
        usrp->set_tx_antenna("TX/RX", channel);
        std::cout << "TX_GAIN channel=" << channel
                  << " requested_db=" << requested_gain
                  << " actual_db=" << usrp->get_tx_gain(channel)
                  << " antenna=" << usrp->get_tx_antenna(channel)
                  << " lo_locked=" << usrp->get_tx_sensor("lo_locked", channel).to_bool()
                  << std::endl;
    }
    for (size_t channel = 0; channel < 2; ++channel) {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
        while (!usrp->get_tx_sensor("lo_locked", channel).to_bool()) {
            if (std::chrono::steady_clock::now() >= deadline)
                throw std::runtime_error("CBX TX LO did not lock before finite probe");
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        std::cout << "G0_LO_LOCKED channel=" << channel << std::endl;
    }
    uhd::stream_args_t args("fc32", "sc16");
    args.channels = {0, 1};
    auto tx = usrp->get_tx_stream(args);
    AsyncMonitor async(tx);
    std::array<std::array<float, 4>, kBlockSamples> raw{};
    std::array<std::complex<float>, kBlockSamples> lane0{}, lane1{};
    const std::array<const std::complex<float>*, 2> rf_lanes = options.swap_iq_channels
        ? std::array<const std::complex<float>*, 2>{lane1.data(), lane0.data()}
        : std::array<const std::complex<float>*, 2>{lane0.data(), lane1.data()};
    std::vector<const void*> buffers(2);
    bool first = true;
    std::cout << "G0_TX_IQ_MAPPING rf0=" << int(options.swap_iq_channels)
              << " rf1=" << int(!options.swap_iq_channels) << std::endl;
    std::cout << "G0_READY WAIT_GO" << std::endl;
    std::string go;
    if (!std::getline(std::cin, go) || go != "GO") throw std::runtime_error("explicit GO not received; no samples transmitted");
    // One hardware start time for both RFNoC TX channels. Untimed SOB lets
    // Ethernet packet arrival skew become an inter-antenna sample offset.
    const auto before_ns = monotonic_ns(options.raw_clock);
    const auto observed = usrp->get_time_now();
    const auto after_ns = monotonic_ns(options.raw_clock);
    const auto observed_ticks = observed.to_ticks(kRate);
    const auto start_ticks = observed_ticks + 1000000; // 250ms at 4MSPS.
    const auto tx_start = uhd::time_spec_t::from_ticks(start_ticks, kRate);
    std::cout << "G0_ARMED lead_s=0.25 device_start_s=" << tx_start.get_real_secs() << std::endl;
    if (before_ns && after_ns >= before_ns) {
        std::cout << (options.raw_clock?"G0_TX_CLOCK_RAW ":"G0_TX_CLOCK ") << before_ns << ' ' << after_ns << ' '
                  << observed_ticks << ' ' << start_ticks << std::endl;
    }
    uint64_t sent = 0;
    bool stopped_at_limit = false;
    try {
        while (sent < options.tx_max_samples) {
            const size_t samples = reader.next(raw.data(), raw.size());
            if (samples == 0) break;
            for (size_t i = 0; i < samples; ++i) {
                lane0[i] = {raw[i][0], raw[i][1]};
                lane1[i] = {raw[i][2], raw[i][3]};
            }
            const size_t sendable = static_cast<size_t>(std::min<uint64_t>(samples, options.tx_max_samples - sent));
            for (size_t offset = 0; offset < sendable;) {
                async.throw_if_failed();
                const size_t request = sendable - offset;
                buffers[0] = rf_lanes[0] + offset;
                buffers[1] = rf_lanes[1] + offset;
                uhd::tx_metadata_t metadata;
                metadata.start_of_burst = first;
                metadata.has_time_spec = first;
                metadata.time_spec = tx_start;
                const size_t accepted = tx->send(buffers, request, metadata, 2.0);
                if (accepted == 0 || accepted > request) throw std::runtime_error("invalid UHD TX write count");
                offset += accepted;
                sent += accepted;
                if (first) {
                    // Coordinate a short passive ADC capture without guessing
                    // how long X310 discovery/tuning took. This is a host
                    // acceptance marker, not proof of an RF waveform on air.
                    std::cout << "TX_FIRST_SAMPLES_ACCEPTED " << accepted << std::endl;
                }
                first = false;
            }
            if (sendable < samples) stopped_at_limit = true;
        }
        if (first) throw std::runtime_error("empty IQ input");
        uhd::tx_metadata_t end;
        end.end_of_burst = true;
        tx->send(std::vector<const void*>{}, 0, end, 1.0);
        // send() acknowledges host/device queue admission, not RF completion.
        // Preserve the streamer until each timed channel reports BURST_ACK.
        async.wait_for_burst_ack(0.25 + static_cast<double>(sent) / kRate + 2.0);
        async.throw_if_failed();
        async.stop_and_join();
    } catch (...) {
        if (!first) {
            uhd::tx_metadata_t end;
            end.end_of_burst = true;
            try { tx->send(std::vector<const void*>{}, 0, end, 1.0); }
            catch (...) { /* Preserve the original stream failure. */ }
        }
        async.stop_and_join();
        throw;
    }
    if (options.observe_clock_end) {
        // Keep device time RPCs out of the streaming loop. The second
        // bracket is optional evidence after both EOB acknowledgements.
        try {
            const auto before = monotonic_ns(options.raw_clock);
            const auto stamp = usrp->get_time_now();
            const auto after = monotonic_ns(options.raw_clock);
            if (before && after >= before)
                std::cout << (options.raw_clock?"G0_TX_CLOCK_END_RAW ":"G0_TX_CLOCK_END ") << before << ' ' << after << ' '
                          << stamp.to_ticks(kRate) << std::endl;
        } catch (const std::exception& error) {
            std::cout << "G0_TX_CLOCK_END_UNAVAILABLE " << error.what() << std::endl;
        }
    }
    std::cout << "TX completed: " << sent << " dual-stream samples at 4 MSPS"
              << (stopped_at_limit ? " (limited by --max-samples).\n" : ".\n");
}
} // namespace

int main(int argc, char** argv) {
    try {
        const Options options = parse(argc, argv);
        if (!options.enable_tx) {
            IQReader reader(options.iq_path);
            std::array<std::array<float, 4>, kBlockSamples> scratch{};
            uint64_t samples = 0;
            while (const size_t n = reader.next(scratch.data(), scratch.size())) samples += n;
            std::cout << "IQ input valid: " << samples << " dual-stream samples. TX disabled; pass --enable-tx to transmit.\n";
            return 0;
        }
        send_file(options);
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "g0_uhd_probe: " << error.what() << "\n";
        return 2;
    }
}
