#include "hdmi_presenter/receiver.h"
#include <array>
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cstring>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
using namespace hdmi_presenter;
namespace {
std::array<uint8_t,kHdv1HeaderBytes> header(uint64_t frame, const uint8_t* payload) { std::array<uint8_t,kHdv1HeaderBytes> h{}; std::memcpy(h.data(),"HDV1",4); store_le16(h.data()+4,1); store_le16(h.data()+6,64); store_le32(h.data()+8,1); store_le32(h.data()+12,kWidth); store_le32(h.data()+16,kHeight); store_le32(h.data()+20,kRgb565Bytes); store_le64(h.data()+24,7); store_le64(h.data()+32,frame); store_le64(h.data()+40,frame*100); store_le32(h.data()+48,ieee_crc32(payload,kRgb565Bytes)); store_le32(h.data()+60,ieee_crc32(h.data(),60)); return h; }
}
int main() {
  int fds[2]; assert(socketpair(AF_UNIX,SOCK_STREAM,0,fds)==0); Presenter presenter(make_null_backend()); presenter.start(); std::array<uint8_t,kRgb565Bytes> payload{}; const auto h=header(1,payload.data());
  std::thread writer([&] { for(size_t i=0;i<h.size();i+=7) { const size_t n=std::min<size_t>(7,h.size()-i); assert(write(fds[1],h.data()+i,n)==static_cast<ssize_t>(n)); } for(size_t i=0;i<payload.size();i+=8191) { const size_t n=std::min<size_t>(8191,payload.size()-i); assert(write(fds[1],payload.data()+i,n)==static_cast<ssize_t>(n)); } close(fds[1]); });
  ReceiverConfig config{}; config.read_timeout_ms=1000; config.queue_wait_ms=1000;
  const ReceiverResult got=receive_records(fds[0],InputKind::kHdv1Tcp,&presenter,config); close(fds[0]); writer.join(); presenter.stop();
  assert(got.error.empty() && got.accepted==1); const Metrics m=presenter.metrics(); assert(m.input_records==1 && m.crc_verified==1 && m.queued==1);
  int bad[2]; assert(socketpair(AF_UNIX,SOCK_STREAM,0,bad)==0); Presenter corrupt(make_null_backend()); corrupt.start(); auto bad_header=header(1,payload.data()); bad_header[48]^=1; assert(write(bad[1],bad_header.data(),bad_header.size())==static_cast<ssize_t>(bad_header.size())); close(bad[1]); const ReceiverResult rejected=receive_records(bad[0],InputKind::kHdv1Tcp,&corrupt,config); close(bad[0]); corrupt.stop(); assert(rejected.accepted==0 && rejected.error=="bad input header: bad_header_crc");
  return 0;
}
