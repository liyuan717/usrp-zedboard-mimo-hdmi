/*
 * LD_PRELOAD-only diagnostic capture edge for mrf1_native_rx_backend.
 *
 * The shim replaces exactly /dev/zed-rxth with a finite RXTH-v1 regular file.
 * It is an unpaced finite capture diagnostic, not the production RXTH DMA
 * path or a throughput/over-the-air measurement. Downstream threads are left
 * unchanged. In particular,
 * it does not intercept the IMBC device: rnt_open/rnt_submit remain the
 * production library's real libc open/ioctl calls to
 * /dev/zed-mimo-interleaved-candidate.
 */
#define _GNU_SOURCE

#include "zedboard_rx_transport.h"

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

enum {
  RXTH_SHIM_MAXIMUM_BLOCKS = 4096U,
  RXTH_REQUIRED_INFO_FLAGS = ZED_RXTH_INFO_DMAENGINE_OWNER |
      ZED_RXTH_INFO_EXCLUSIVE_OPEN | ZED_RXTH_INFO_FIXED_BLOCK |
      ZED_RXTH_INFO_NO_MMAP | ZED_RXTH_INFO_STICKY_IDLE_ABORT,
};

_Static_assert(RXTH_REQUIRED_INFO_FLAGS == 0x1fU, "RXTH v1 required-info flags drift");
_Static_assert(sizeof(struct zed_rxth_info) == 32U, "RXTH info ABI drift");
_Static_assert(sizeof(struct zed_rxth_timeout) == 8U, "RXTH timeout ABI drift");

static const char k_rxth_device[] = "/dev/zed-rxth";
static const char k_fixture_environment[] = "MRF1_VIRTUAL_RXTH_FILE";

typedef int (*open_function)(const char *, int, ...);
typedef ssize_t (*read_function)(int, void *, size_t);
typedef ssize_t (*pread_function)(int, void *, size_t, off_t);
typedef int (*ioctl_function)(int, unsigned long, ...);
typedef int (*close_function)(int);

_Static_assert(sizeof(void *) == sizeof(open_function), "POSIX dlsym function-pointer ABI required");
_Static_assert(sizeof(void *) == sizeof(read_function), "POSIX dlsym function-pointer ABI required");
_Static_assert(sizeof(void *) == sizeof(pread_function), "POSIX dlsym function-pointer ABI required");
_Static_assert(sizeof(void *) == sizeof(ioctl_function), "POSIX dlsym function-pointer ABI required");
_Static_assert(sizeof(void *) == sizeof(close_function), "POSIX dlsym function-pointer ABI required");

static open_function real_open_function;
static open_function real_open64_function;
static read_function real_read_function;
static pread_function real_pread_function;
static ioctl_function real_ioctl_function;
static close_function real_close_function;
static pthread_once_t resolve_once = PTHREAD_ONCE_INIT;
static int resolve_failed;

/* One original runtime capture thread owns this device.  A lock covers the
 * tracked descriptor, fixture lifetime, position and status fields.  The
 * abort latch is atomic so an idle ABORT becomes visible before a subsequent
 * read can start another virtual block delivery. */
struct capture_state {
  pthread_mutex_t mutex;
  int tracked_fd;
  uint64_t fixture_bytes;
  uint64_t fixture_offset;
  uint32_t timeout_ms;
  uint64_t submitted_blocks;
  uint64_t completed_blocks;
  uint64_t aborted_blocks;
  uint64_t timeout_blocks;
  uint64_t delivery_failures;
  int last_error;
  bool read_started;
  atomic_bool abort_latched;
};

static struct capture_state capture = {
    .mutex = PTHREAD_MUTEX_INITIALIZER,
    .tracked_fd = -1,
    .timeout_ms = 1000U,
    .abort_latched = ATOMIC_VAR_INIT(false),
};

static void copy_symbol(void *destination, size_t destination_size, const char *name) {
  void *symbol = dlsym(RTLD_NEXT, name);
  if (symbol == NULL || destination_size != sizeof(symbol)) {
    resolve_failed = 1;
    return;
  }
  memcpy(destination, &symbol, sizeof(symbol));
}

static void resolve_real_libc(void) {
  copy_symbol(&real_open_function, sizeof(real_open_function), "open");
  /* open64 is a compatibility spelling on 32-bit glibc.  The target runtime
   * uses open(), but catch either spelling without touching another pathname. */
  void *open64_symbol = dlsym(RTLD_NEXT, "open64");
  if (open64_symbol != NULL) memcpy(&real_open64_function, &open64_symbol, sizeof(open64_symbol));
  else real_open64_function = real_open_function;
  copy_symbol(&real_read_function, sizeof(real_read_function), "read");
  copy_symbol(&real_pread_function, sizeof(real_pread_function), "pread");
  copy_symbol(&real_ioctl_function, sizeof(real_ioctl_function), "ioctl");
  copy_symbol(&real_close_function, sizeof(real_close_function), "close");
}

static int resolve_or_errno(void) {
  (void)pthread_once(&resolve_once, resolve_real_libc);
  if (resolve_failed != 0 || real_open_function == NULL || real_open64_function == NULL ||
      real_read_function == NULL || real_pread_function == NULL ||
      real_ioctl_function == NULL || real_close_function == NULL) {
    errno = ENOSYS;
    return -1;
  }
  return 0;
}

static bool mode_is_required(int flags) {
  if ((flags & O_CREAT) != 0) return true;
#ifdef O_TMPFILE
  if ((flags & O_TMPFILE) == O_TMPFILE) return true;
#endif
  return false;
}

static int call_real_open(open_function function, const char *path, int flags, mode_t mode) {
  return mode_is_required(flags) ? function(path, flags, mode) : function(path, flags);
}

static bool is_exact_rxth_path(const char *path) {
  return path != NULL && strcmp(path, k_rxth_device) == 0;
}

static int refuse_virtual_capture(int error) {
  errno = error;
  return -1;
}

static int open_virtual_capture(void) {
  const char *fixture_path = getenv(k_fixture_environment);
  if (fixture_path == NULL || fixture_path[0] == '\0') return refuse_virtual_capture(EINVAL);

  struct stat link_metadata;
  if (lstat(fixture_path, &link_metadata) != 0) return -1;
  if (S_ISLNK(link_metadata.st_mode)) return refuse_virtual_capture(ELOOP);

  const int fixture_fd = call_real_open(real_open_function, fixture_path,
                                        O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
  if (fixture_fd < 0) return -1;
  struct stat metadata;
  const int metadata_result = fstat(fixture_fd, &metadata);
  const bool malformed = metadata_result != 0 || !S_ISREG(metadata.st_mode) || metadata.st_size <= 0 ||
      ((uint64_t)metadata.st_size % ZED_RXTH_BLOCK_BYTES) != 0U ||
      ((uint64_t)metadata.st_size / ZED_RXTH_BLOCK_BYTES) > RXTH_SHIM_MAXIMUM_BLOCKS;
  if (malformed) {
    const int saved_errno = metadata_result == 0 ? EINVAL : errno;
    (void)real_close_function(fixture_fd);
    return refuse_virtual_capture(saved_errno);
  }

  if (pthread_mutex_lock(&capture.mutex) != 0) {
    (void)real_close_function(fixture_fd);
    return refuse_virtual_capture(EBUSY);
  }
  if (capture.tracked_fd >= 0) {
    (void)pthread_mutex_unlock(&capture.mutex);
    (void)real_close_function(fixture_fd);
    return refuse_virtual_capture(EBUSY);
  }
  capture.tracked_fd = fixture_fd;
  capture.fixture_bytes = (uint64_t)metadata.st_size;
  capture.fixture_offset = 0U;
  capture.timeout_ms = 1000U;
  capture.submitted_blocks = 0U;
  capture.completed_blocks = 0U;
  capture.aborted_blocks = 0U;
  capture.timeout_blocks = 0U;
  capture.delivery_failures = 0U;
  capture.last_error = 0;
  capture.read_started = false;
  atomic_store_explicit(&capture.abort_latched, false, memory_order_release);
  (void)pthread_mutex_unlock(&capture.mutex);
  return fixture_fd;
}

static bool fd_is_tracked_locked(int fd) {
  return capture.tracked_fd >= 0 && capture.tracked_fd == fd;
}

static int virtual_ioctl_locked(unsigned long request, void *argument) {
  if (request == ZED_RXTH_IOC_GET_INFO) {
    if (argument == NULL) return refuse_virtual_capture(EFAULT);
    const struct zed_rxth_info info = {
        .abi_version = ZED_RXTH_ABI_VERSION,
        .format_version = ZED_RXTH_FORMAT_VERSION,
        .block_bytes = ZED_RXTH_BLOCK_BYTES,
        .header_bytes = ZED_RXTH_HEADER_BYTES,
        .samples_per_block = ZED_RXTH_SAMPLES_PER_BLOCK,
        .bytes_per_sample = ZED_RXTH_BYTES_PER_SAMPLE,
        .flags = RXTH_REQUIRED_INFO_FLAGS,
        .reserved = 0U,
    };
    memcpy(argument, &info, sizeof(info));
    return 0;
  }
  if (request == ZED_RXTH_IOC_SET_TIMEOUT) {
    if (argument == NULL) return refuse_virtual_capture(EFAULT);
    struct zed_rxth_timeout timeout;
    memcpy(&timeout, argument, sizeof(timeout));
    if (timeout.timeout_ms == 0U || timeout.timeout_ms > ZED_RXTH_TIMEOUT_MAX_MS || timeout.reserved != 0U)
      return refuse_virtual_capture(EINVAL);
    if (capture.read_started) return refuse_virtual_capture(EBUSY);
    capture.timeout_ms = timeout.timeout_ms;
    return 0;
  }
  if (request == ZED_RXTH_IOC_GET_STATUS) {
    if (argument == NULL) return refuse_virtual_capture(EFAULT);
    const struct zed_rxth_status status = {
        .submitted_blocks = capture.submitted_blocks,
        .completed_blocks = capture.completed_blocks,
        .aborted_blocks = capture.aborted_blocks,
        .timeout_blocks = capture.timeout_blocks,
        .delivery_failures = capture.delivery_failures,
        .last_error = capture.last_error,
        .state = atomic_load_explicit(&capture.abort_latched, memory_order_acquire) ?
            ZED_RXTH_STATE_IDLE : (capture.read_started ? ZED_RXTH_STATE_ACTIVE : ZED_RXTH_STATE_IDLE),
        .timeout_ms = capture.timeout_ms,
        .reserved = 0U,
    };
    memcpy(argument, &status, sizeof(status));
    return 0;
  }
  if (request == ZED_RXTH_IOC_ABORT) {
    /* This is intentionally owner-sticky even while idle, matching the RXTH
     * ABI: close/reopen is the only normal way to clear it. */
    atomic_store_explicit(&capture.abort_latched, true, memory_order_release);
    capture.read_started = false;
    capture.last_error = -ECANCELED;
    ++capture.aborted_blocks;
    return 0;
  }
  return 1;  /* Caller forwards a non-RXTH ioctl unchanged. */
}

static bool ioctl_has_argument(unsigned long request) {
  return _IOC_DIR(request) != _IOC_NONE || _IOC_SIZE(request) != 0U;
}

__attribute__((constructor)) static void rxth_file_capture_shim_constructor(void) {
  (void)resolve_or_errno();
  const char *fixture_path = getenv(k_fixture_environment);
  if (fixture_path == NULL || fixture_path[0] == '\0') {
    (void)dprintf(STDERR_FILENO,
                  "rxth_file_capture_shim: virtual_capture=true rxth_dma=false source=missing (will refuse /dev/zed-rxth)\n");
  } else {
    (void)dprintf(STDERR_FILENO,
                  "rxth_file_capture_shim: virtual_capture=true rxth_dma=false source=MRF1_VIRTUAL_RXTH_FILE (unpaced file capture)\n");
  }
}

int open(const char *path, int flags, ...) {
  mode_t mode = 0;
  if (mode_is_required(flags)) {
    va_list arguments;
    va_start(arguments, flags);
    mode = va_arg(arguments, mode_t);
    va_end(arguments);
  }
  if (resolve_or_errno() != 0) return -1;
  if (is_exact_rxth_path(path)) return open_virtual_capture();
  return call_real_open(real_open_function, path, flags, mode);
}

int open64(const char *path, int flags, ...) {
  mode_t mode = 0;
  if (mode_is_required(flags)) {
    va_list arguments;
    va_start(arguments, flags);
    mode = va_arg(arguments, mode_t);
    va_end(arguments);
  }
  if (resolve_or_errno() != 0) return -1;
  if (is_exact_rxth_path(path)) return open_virtual_capture();
  return call_real_open(real_open64_function, path, flags, mode);
}

ssize_t read(int fd, void *buffer, size_t count) {
  if (resolve_or_errno() != 0) return -1;
  if (pthread_mutex_lock(&capture.mutex) != 0) {
    errno = EBUSY;
    return -1;
  }
  if (!fd_is_tracked_locked(fd)) {
    (void)pthread_mutex_unlock(&capture.mutex);
    return real_read_function(fd, buffer, count);
  }
  if (count < ZED_RXTH_BLOCK_BYTES) {
    (void)pthread_mutex_unlock(&capture.mutex);
    errno = EMSGSIZE;
    return -1;
  }
  if (atomic_load_explicit(&capture.abort_latched, memory_order_acquire)) {
    (void)pthread_mutex_unlock(&capture.mutex);
    errno = ECANCELED;
    return -1;
  }
  if (capture.fixture_offset + ZED_RXTH_BLOCK_BYTES > capture.fixture_bytes) {
    capture.last_error = -EIO;
    ++capture.delivery_failures;
    (void)pthread_mutex_unlock(&capture.mutex);
    errno = EIO;
    return -1;
  }

  /* No allocation, logging, or filesystem pathname lookup is performed here.
   * Holding this lock through pread prevents close/reuse of the tracked fd. */
  capture.read_started = true;
  ++capture.submitted_blocks;
  const ssize_t received = real_pread_function(fd, buffer, ZED_RXTH_BLOCK_BYTES,
                                                (off_t)capture.fixture_offset);
  if (received != (ssize_t)ZED_RXTH_BLOCK_BYTES ||
      atomic_load_explicit(&capture.abort_latched, memory_order_acquire)) {
    const int saved_errno = atomic_load_explicit(&capture.abort_latched, memory_order_acquire) ?
        ECANCELED : (received < 0 ? errno : EIO);
    capture.last_error = -saved_errno;
    ++capture.delivery_failures;
    (void)pthread_mutex_unlock(&capture.mutex);
    errno = saved_errno;
    return -1;
  }
  capture.fixture_offset += ZED_RXTH_BLOCK_BYTES;
  ++capture.completed_blocks;
  (void)pthread_mutex_unlock(&capture.mutex);
  return (ssize_t)ZED_RXTH_BLOCK_BYTES;
}

int ioctl(int fd, unsigned long request, ...) {
  void *argument = NULL;
  if (ioctl_has_argument(request)) {
    va_list arguments;
    va_start(arguments, request);
    argument = va_arg(arguments, void *);
    va_end(arguments);
  }
  if (resolve_or_errno() != 0) return -1;
  if (pthread_mutex_lock(&capture.mutex) != 0) {
    errno = EBUSY;
    return -1;
  }
  if (!fd_is_tracked_locked(fd)) {
    (void)pthread_mutex_unlock(&capture.mutex);
    return ioctl_has_argument(request) ? real_ioctl_function(fd, request, argument) :
        real_ioctl_function(fd, request);
  }
  const int result = virtual_ioctl_locked(request, argument);
  if (result == 1) {
    /* Only the four RXTH ABI requests are emulated.  Preserve every other
     * request by forwarding it to libc while the descriptor lifetime is held. */
    const int forwarded = ioctl_has_argument(request) ? real_ioctl_function(fd, request, argument) :
        real_ioctl_function(fd, request);
    (void)pthread_mutex_unlock(&capture.mutex);
    return forwarded;
  }
  (void)pthread_mutex_unlock(&capture.mutex);
  return result;
}

int close(int fd) {
  if (resolve_or_errno() != 0) return -1;
  if (pthread_mutex_lock(&capture.mutex) != 0) {
    errno = EBUSY;
    return -1;
  }
  if (!fd_is_tracked_locked(fd)) {
    (void)pthread_mutex_unlock(&capture.mutex);
    return real_close_function(fd);
  }
  /* A concurrent read checks this atomic latch after pread before reporting a
   * virtual block.  Thus close cannot permit a post-close block publication. */
  atomic_store_explicit(&capture.abort_latched, true, memory_order_release);
  capture.tracked_fd = -1;
  capture.read_started = false;
  const int result = real_close_function(fd);
  (void)pthread_mutex_unlock(&capture.mutex);
  return result;
}
