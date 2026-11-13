// Finite clock observations only. No stream creation, tuning or time setting.
#include <uhd/usrp/multi_usrp.hpp>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <time.h>

static uint64_t nanoseconds(clockid_t clock) {
    timespec value{};
    if (clock_gettime(clock, &value)) throw std::runtime_error("clock_gettime");
    return uint64_t(value.tv_sec) * 1000000000 + value.tv_nsec;
}

int main() {
    try {
        const uhd::device_addr_t address(std::string("addr=192.168.10.2"));
        auto usrp = uhd::usrp::multi_usrp::make(address);
        const auto start = std::chrono::steady_clock::now();
        for (unsigned sample = 0; sample < 33; ++sample) {
            std::this_thread::sleep_until(start + std::chrono::milliseconds(sample * 250));
            const auto mono_before = nanoseconds(CLOCK_MONOTONIC);
            const auto raw_before = nanoseconds(CLOCK_MONOTONIC_RAW);
            const auto ticks = usrp->get_time_now().to_ticks(4000000);
            const auto raw_after = nanoseconds(CLOCK_MONOTONIC_RAW);
            const auto mono_after = nanoseconds(CLOCK_MONOTONIC);
            std::cout << "CLOCK_PAIR " << mono_before << ' ' << raw_before << ' '
                      << ticks << ' ' << raw_after << ' ' << mono_after << std::endl;
        }
        std::cout << "CLOCK_PAIRS_DONE RF_STREAMS_CREATED=0" << std::endl;
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << std::endl;
        return 1;
    }
}
