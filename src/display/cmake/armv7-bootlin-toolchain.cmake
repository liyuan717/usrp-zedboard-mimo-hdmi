# Usage: cmake -S ... -B ... -DCMAKE_TOOLCHAIN_FILE=cmake/armv7-bootlin-toolchain.cmake
# The prefix can be overridden for a different, ABI-compatible Bootlin SDK.
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)
set(HDMI_PRESENTER_ARM_TOOLCHAIN_PREFIX
    "/home/builder/mimo/build/zedboard/toolchains/bootlin-armv7/bin/arm-buildroot-linux-gnueabihf-"
    CACHE STRING "Cross compiler prefix")
set(CMAKE_C_COMPILER "${HDMI_PRESENTER_ARM_TOOLCHAIN_PREFIX}gcc")
set(CMAKE_CXX_COMPILER "${HDMI_PRESENTER_ARM_TOOLCHAIN_PREFIX}g++")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
