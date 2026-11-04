#include "hdmi_presenter/drm_uapi_min.h"

#include <cassert>

int main() {
  using namespace hdmi_presenter::drm_uapi;
  PresentationInfo info{};
  info.session = 41;
  info.status = kReady;
  assert(running_session_unchanged(info, 41));

  /* Mock observer restart: the event may still be readable, but its old
   * token must not release a scanout buffer once the generation changes. */
  info.session = 42;
  assert(!running_session_unchanged(info, 41));
  info.session = 41;
  info.status = 0;
  assert(!running_session_unchanged(info, 41));
  info.status = kReady | kError;
  assert(!running_session_unchanged(info, 41));
  info.status = kReady;
  assert(!running_session_unchanged(info, 0));
  return 0;
}
