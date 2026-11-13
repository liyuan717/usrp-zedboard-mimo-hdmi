// Bounded video receive pipeline. Optional SOURCE DMA/physical HDMI, no RF TX.
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/buffer.h>
#include "yuv420p_rgb565.h"
#include "include/g0_video_fragment.h"
#include "include/g0_video_au.h"
}
#include "include/g0_au_decode_queue.hpp"
#include "include/g0_source_dma_ingress.hpp"
#include "include/g0_source_video_ingress.hpp"
#include "hdmi_presenter/presenter.h"
#include <fstream>
#include <iostream>
#include <thread>
#include <stdexcept>
#include <csignal>
#include <sys/utsname.h>
#include <time.h>
using namespace v3decode;
using namespace hdmi_presenter;
struct Output {uint64_t id;uint32_t crc;};
struct AckSample {uint64_t id,pts_ns,board_ns;uint32_t uncertainty_us;};
struct AckCapture {
 static constexpr size_t limit=1200;
 SourceAge* age{};
 uint64_t flip_budget_ns{};
 uint64_t checked_id{},checked_ns{},max_flip_delay_ns{},flip_budget_overruns{};
 std::vector<AckSample> samples;
 unsigned omitted{};
};
static_assert(std::atomic<bool>::is_always_lock_free,"signal flag must be lock-free");
static std::atomic<bool> stop_requested{false};
static void stop_signal(int){stop_requested.store(true,std::memory_order_relaxed);}
struct ReaderStats {uint64_t inputs=0,damaged=0,source_events=0,source_records=0,source_session_rejected=0,source_sessions_adopted=0;bool error=false,source_session_auto=false;v3vf_stats fragments{};v3au_stats aus{};SourceDmaStats dma{};};
static double cpu_seconds(){timespec t{};clock_gettime(CLOCK_PROCESS_CPUTIME_ID,&t);return t.tv_sec+t.tv_nsec/1e9;}
static uint64_t monotonic_ns(){
 timespec t{};
 return clock_gettime(CLOCK_MONOTONIC,&t)==0?uint64_t(t.tv_sec)*1000000000+t.tv_nsec:0;
}
struct Join {Queue& queue;std::thread& thread;~Join(){queue.cancel();if(thread.joinable())thread.join();}};
struct FillOwner {
 Presenter* presenter;FillSlot slot{};bool active=false;
 ~FillOwner(){if(active)presenter->abandon_fill(slot);}
};
static void feed(Queue& q,const char* path,bool paced,ReaderStats& stats){
 try{
  std::ifstream file(path,std::ios::binary);if(!file)throw std::runtime_error("input open");
  std::vector<uint8_t> scratch(AU_MAX);std::array<uint8_t,32> h{};size_t total=0;
  auto start=Clock::now();uint64_t first_capture=0,last_capture=0;
  while(!q.cancelled()&&file.read(reinterpret_cast<char*>(h.data()),h.size())){
   if(std::memcmp(h.data(),"V3AU",4)||load_le16(h.data()+4)!=1||load_le16(h.data()+6)!=32)throw std::runtime_error("fixture header");
   uint32_t bytes=load_le32(h.data()+24);Info info{load_le64(h.data()+8),load_le64(h.data()+16),load_le32(h.data()+28)};
   if(!bytes||bytes>AU_MAX||info.damaged>1||stats.inputs>=4096||(total+=bytes)>(64U<<20))throw std::runtime_error("fixture bound");
   if(stats.inputs&&info.capture_us<=last_capture)throw std::runtime_error("fixture time order");
   if(!stats.inputs)first_capture=info.capture_us;
   if(info.capture_us-first_capture>205000000)throw std::runtime_error("finite fixture duration");
   last_capture=info.capture_us;++stats.inputs;stats.damaged+=info.damaged;
   if(!file.read(reinterpret_cast<char*>(scratch.data()),bytes))throw std::runtime_error("fixture truncation");
   if(paced&&!q.pace(start+std::chrono::microseconds(info.capture_us-first_capture)))break;
   q.push(info,scratch.data(),bytes);
  }
  if(!q.cancelled()&&(!file.eof()||file.gcount()))throw std::runtime_error("fixture tail");
 }catch(const std::exception&){stats.error=true;}
 q.close();
}
using SourceIngress=SourceVideoIngress<ReaderStats>;
static void feed_sources(Queue& q,const char* path,bool paced,ReaderStats& stats){
 SourceIngress s{q,stats,v3vf_create(),v3au_create()};
 try{
  if(!s.fragments||!s.au||!v3vf_set_ordered(s.fragments,1))throw std::runtime_error("source contexts");
  std::ifstream file(path,std::ios::binary);if(!file)throw std::runtime_error("source input");
  std::array<uint8_t,544> e{};uint64_t origin=0,previous=0;auto begin=Clock::now();
  while(!q.cancelled()&&file.read(reinterpret_cast<char*>(e.data()),e.size())){
   uint64_t target=load_le64(e.data());uint32_t sid=load_le32(e.data()+16),cmd=load_le32(e.data()+20);
   if(stats.source_events++>=60000||target<previous||load_le32(e.data()+28))throw std::runtime_error("source fixture bound");
   if(stats.source_events==1)origin=target;
   if(target-origin>205000000)throw std::runtime_error("source fixture duration");
   previous=target;
   if(paced){
    const auto deadline=begin+std::chrono::microseconds(target-origin);
    while(Clock::now()<deadline&&!q.cancelled()){
     q.pace(std::min(deadline,Clock::now()+std::chrono::milliseconds(10)));
     s.tick(origin+std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-begin).count());
    }
   }
   s.local=paced?origin+std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-begin).count():target;
   if(cmd==0)(void)v3vf_push(s.fragments,sid,e.data()+32,512,s.local,0,0,SourceIngress::output_nal,&s);
   else if(cmd==1)s.tick(s.local);
   else if(cmd==2){v3vf_session(s.fragments,sid);v3au_session(s.au,sid);q.admit(sid);}
   else if(cmd==3)s.drop_nals=load_le32(e.data()+32);
   else if(cmd==4){if(!v3vf_set_ordered(s.fragments,1))throw std::runtime_error("ordered fixture command");}
   else throw std::runtime_error("source fixture command");
   s.tick(s.local);
  }
  if(!q.cancelled()&&(!file.eof()||file.gcount()))throw std::runtime_error("source fixture tail");
  // EOF is finite-fixture finalization, not evidence of real RF idle timing.
  // Live pacing above ticks every10ms without a shared source-clock claim.
  s.tick(s.local+V3VF_LOCAL_TTL_US+V3VF_REORDER_WAIT_US+1);
  s.tick(s.local+V3AU_IDLE_US+1);
  v3vf_get_stats(s.fragments,&stats.fragments);v3au_get_stats(s.au,&stats.aus);
 }catch(const std::exception&){stats.error=true;}
 v3vf_destroy(s.fragments);v3au_destroy(s.au);q.close();
}
static void feed_dma(Queue& q,uint32_t sid,bool automatic,uint32_t batches,uint32_t wait_ms,ReaderStats& stats,SourceAge* age){
 SourceIngress s{q,stats,v3vf_create(),v3au_create()};s.admission=SourceSession(automatic,sid);stats.source_session_auto=automatic;
 s.age=age;
 try{
  if(!s.fragments||!s.au||!v3vf_set_ordered(s.fragments,1))throw std::runtime_error("source contexts");
  // Auto mode learns only from PL-validated SOURCE; no UART session value.
  // Pinned mode remains a finite diagnostic control, never a fallback.
  if(!automatic){v3vf_session(s.fragments,sid);v3au_session(s.au,sid);q.admit(sid);}
  auto tick=[&](int fd){s.clock_fd=fd;s.tick(std::chrono::duration_cast<std::chrono::microseconds>(Clock::now().time_since_epoch()).count());};
  source_dma(q,batches,wait_ms,SourceIngress::output_record,&s,tick,[]{return stop_requested.load(std::memory_order_relaxed);},stats.dma);
  stats.error=stats.dma.lease_error||stats.dma.timed_out;
  // Real monotonic idle, never virtual EOF timestamps for physical DMA.
  const auto end=Clock::now()+std::chrono::seconds(1);
  while(!q.cancelled()&&Clock::now()<end){q.pace(std::min(end,Clock::now()+std::chrono::milliseconds(10)));tick(-1);}
  v3vf_get_stats(s.fragments,&stats.fragments);v3au_get_stats(s.au,&stats.aus);
 }catch(const std::exception&){stats.error=true;}
 v3vf_destroy(s.fragments);v3au_destroy(s.au);q.close();
}
int main(int argc,char** argv){
 AVCodecContext* ctx=nullptr;AVFrame* frame=nullptr;AVPacket* packet=nullptr;
 try{
  const bool capture_acks=argc>1&&std::string(argv[argc-1])=="capture-acks";
  if(capture_acks)--argc;
  const bool age_lower_only=argc>1&&std::string(argv[argc-1])=="enforce-age-lower";
  const bool enforce_age=age_lower_only||(argc>1&&std::string(argv[argc-1])=="enforce-age");
  if(enforce_age)--argc;
  uint32_t age_path_bound=0;
  if(argc>1 && std::string(argv[argc-1]).rfind("age-bound-ns=",0)==0){
   const std::string value=std::string(argv[--argc]).substr(13);size_t used=0;
   const auto ns=std::stoul(value,&used);
   if(!ns||ns>10000000||used!=value.size())throw std::runtime_error("age probe path bound");
   age_path_bound=static_cast<uint32_t>(ns);
  }
  const bool hdmi=argc>1&&std::string(argv[argc-1])=="hdmi";if(hdmi)--argc;
  const bool continuous=argc==5&&std::string(argv[4])=="source-dma-live";
  const bool dma=continuous||(argc==8&&std::string(argv[4])=="source-dma");
  if((argc!=4&&argc!=5&&!dma)||(std::string(argv[3])!="paced"&&std::string(argv[3])!="fast")||(argc==5&&!continuous&&std::string(argv[4])!="source-events"))throw std::runtime_error("usage: stream-decode INPUT REPORT paced|fast [source-events | source-dma SESSION BATCHES WAIT_MS | source-dma-live] [hdmi]");
  uint32_t sid=0,batches=0,wait_ms=0;bool automatic=false;
  if(dma){
   auto number=[](const char* s){size_t end=0;unsigned long long n=std::stoull(s,&end,0);if(s[0]=='-'||s[end]||n>UINT32_MAX)throw std::runtime_error("DMA argument");return static_cast<uint32_t>(n);};
   if(std::string(argv[1])!="/dev/v3-g0-source-ring"||std::string(argv[3])!="paced")throw std::runtime_error("SOURCE DMA device or pacing");
   if(continuous){
    if(!hdmi)throw std::runtime_error("live SOURCE DMA requires physical HDMI");
    automatic=true;
   }else{
    automatic=std::string(argv[5])=="auto";if(!automatic)sid=number(argv[5]);batches=number(argv[6]);wait_ms=number(argv[7]);
    if(!batches||batches>V3_SOURCE_RING_MAX_BATCHES||wait_ms<100||wait_ms>120000)throw std::runtime_error("finite SOURCE DMA bounds");
   }
   std::signal(SIGINT,stop_signal);std::signal(SIGTERM,stop_signal);
  }
  const bool observe_age=age_path_bound||age_lower_only;
  if(observe_age && !(dma && hdmi && automatic))throw std::runtime_error("age probe requires automatic physical SOURCE DMA and HDMI");
  if(enforce_age && !observe_age)throw std::runtime_error("source expiry requires a calibrated path bound");
  if(age_lower_only && age_path_bound)throw std::runtime_error("lower-only expiry does not take a path bound");
  if(capture_acks && !(dma && hdmi && automatic))throw std::runtime_error("ACK capture requires automatic physical SOURCE DMA and HDMI");
  ctx=avcodec_alloc_context3(avcodec_find_decoder(AV_CODEC_ID_H264));if(!ctx)throw std::runtime_error("codec alloc");
  ctx->thread_count=2;ctx->thread_type=FF_THREAD_SLICE;ctx->flags|=AV_CODEC_FLAG_OUTPUT_CORRUPT|AV_CODEC_FLAG_COPY_OPAQUE;
  // H264 codes600 visible lines as608 macroblock-aligned lines, then crops.
  ctx->max_pixels=1024*608;
  ctx->err_recognition=AV_EF_CAREFUL;if(avcodec_open2(ctx,ctx->codec,nullptr)<0)throw std::runtime_error("codec open");
  frame=av_frame_alloc();packet=av_packet_alloc();if(!frame||!packet)throw std::runtime_error("packet/frame alloc");
  SourceAge source_age(age_path_bound,age_lower_only);bool monotonic_ack=false;
  AckCapture ack_capture;
  std::unique_ptr<Presenter> presenter;std::string display_error;
  if(hdmi){
   auto backend=make_drm_backend("/dev/dri/card0");
   if(!backend->supports_physical_presentation())throw std::runtime_error(backend->unavailable_reason());
   monotonic_ack=backend->monotonic_ack_timestamps();
   const uint64_t refresh_millihz=backend->mode_refresh_millihz();
   if(monotonic_ack && enforce_age && refresh_millihz)
    // R22 measured 49.315 ms from READY check through completed scanout.
    // Reserve three refreshes for that path plus one for scheduling variation.
    ack_capture.flip_budget_ns=4*((UINT64_C(1000000000000)+refresh_millihz-1)/refresh_millihz);
   presenter=std::make_unique<Presenter>(std::move(backend));
   presenter->set_rgb_expand_mode(RgbExpandMode::kNeon);presenter->set_repeat_displayed_on_idle(true);
   presenter->set_signal_loss_notice(true);
   if(!presenter->set_proof_capture_path(std::string(argv[2])+".physical.json",&display_error))throw std::runtime_error(display_error);
   if(monotonic_ack && capture_acks)ack_capture.samples.reserve(AckCapture::limit);
   if(monotonic_ack && observe_age)ack_capture.age=&source_age;
   if(monotonic_ack && (observe_age || capture_acks))presenter->set_new_frame_ack(
    [](void* owner,const FrameIdentity& id,uint64_t timestamp){
     auto& capture=*static_cast<AckCapture*>(owner);
     if(capture.age)capture.age->ack(id.session_id,id.pts_ns,timestamp,id.capture_uncertainty_us);
     if(capture.checked_ns && capture.checked_id==((id.session_id<<32)|id.frame_id) &&
        timestamp>=capture.checked_ns){
      const uint64_t delay=timestamp-capture.checked_ns;
      capture.max_flip_delay_ns=std::max(capture.max_flip_delay_ns,delay);
      capture.flip_budget_overruns+=delay>capture.flip_budget_ns;
     }
     if(capture.samples.capacity()){
      if(capture.samples.size()<AckCapture::limit)
       capture.samples.push_back({(uint64_t(id.session_id)<<32)|id.frame_id,id.pts_ns,timestamp,id.capture_uncertainty_us});
      else ++capture.omitted;
     }
    },&ack_capture);
   if(monotonic_ack && enforce_age)presenter->set_ready_frame_expired(
    [](void* owner,const FrameIdentity& id){
     auto& capture=*static_cast<AckCapture*>(owner);
     const uint64_t now=monotonic_ns();
     // Include the measured scanout completion path before its physical ACK.
     const bool expired=now && capture.age->presentation_expired(id.session_id,id.pts_ns,now+capture.flip_budget_ns,id.capture_uncertainty_us);
     if(!expired){capture.checked_id=(id.session_id<<32)|id.frame_id;capture.checked_ns=now;}
     return expired;
    },&ack_capture);
   presenter->start();
  }
  Queue queue;ReaderStats input;
  std::thread producer=dma?std::thread(feed_dma,std::ref(queue),sid,automatic,batches,wait_ms,std::ref(input),observe_age?&source_age:nullptr):
   std::thread(argc==5?feed_sources:feed,std::ref(queue),argv[1],std::string(argv[3])=="paced",std::ref(input));Join join{queue,producer};
  std::vector<uint8_t> rgb(hdmi?0:kRgb565Bytes);std::vector<Output> outputs;
  unsigned valid_decoded=0,display_dropped=0;bool display_drain=true,display_stop=true;
  unsigned source_age_dropped=0,backlog_presentation_skips=0;
  unsigned errors=0,unknown=0,format=0,flagged=0,marked=0,expired=0,stale=0,session_flushes=0;uint64_t serial=0,decode_admission=0;
  auto start=Clock::now();double cpu_start=cpu_seconds();
  double first_output=0,last_output=0;
  auto expired_for_display=[&](const Info& info){
   return enforce_age && monotonic_ack && info.capture_us<=UINT64_MAX/1000 &&
    source_age.presentation_expired(info.id>>32,info.capture_us*1000,monotonic_ns(),info.capture_uncertainty_us);
  };
  auto receive=[&]{
   for(;;){
    int ret=avcodec_receive_frame(ctx,frame);if(ret==AVERROR(EAGAIN)||ret==AVERROR_EOF)break;
    if(ret<0){++errors;break;}
    if(!frame->opaque_ref||frame->opaque_ref->size!=sizeof(Info)){++unknown;av_frame_unref(frame);continue;}
    Info info{};std::memcpy(&info,frame->opaque_ref->data,sizeof(info));
    if(!queue.accepts(info)){++stale;av_frame_unref(frame);continue;}
    if(frame->width!=1024||frame->height!=600||frame->format!=AV_PIX_FMT_YUV420P){++format;av_frame_unref(frame);continue;}
    ++valid_decoded;flagged+=frame->decode_error_flags!=0;
    // The decoder has already advanced its references. Suppress only the
    // expired presentation, keeping useful pictures and future recovery.
    if(expired_for_display(info)){++source_age_dropped;av_frame_unref(frame);continue;}
    if(hdmi && dma && queue.presentation_backlogged()){
     ++backlog_presentation_skips;av_frame_unref(frame);continue;
    }
    FillOwner fill{presenter.get()};uint8_t* dst=rgb.data();
    if(hdmi){
     // At most one20fps frame interval; ingress runs independently and remains bounded.
     if(info.capture_us>UINT64_MAX/1000||presenter->begin_fill(50,&fill.slot,&display_error)!=SubmitResult::kQueued){++display_dropped;av_frame_unref(frame);continue;}
     fill.active=true;dst=fill.slot.rgb565;
    }
    yuv420p_rgb565_image image{frame->data[0],frame->data[1],frame->data[2],
     static_cast<size_t>(frame->linesize[0]),static_cast<size_t>(frame->linesize[1]),static_cast<size_t>(frame->linesize[2]),1024,600};
    if(yuv420p_to_rgb565_neon(&image,dst,2048)==YUV420P_RGB565_OK){
     const uint32_t crc=ieee_crc32(dst,kRgb565Bytes);
     if(outputs.size()<4096){outputs.push_back({info.id,crc});last_output=std::chrono::duration<double>(Clock::now()-start).count();if(outputs.size()==1)first_output=last_output;}
     if(hdmi){
      if(!queue.accepts(info)){++stale;av_frame_unref(frame);continue;}
      if(expired_for_display(info)){++source_age_dropped;av_frame_unref(frame);continue;}
      presenter->note_input_record();FrameIdentity identity{info.id>>32,static_cast<uint32_t>(info.id),info.capture_us*1000,crc,info.capture_uncertainty_us};
      if(presenter->finish_fill(fill.slot,identity,&display_error)==SubmitResult::kQueued)fill.active=false;
      else ++display_dropped;
     }
    }else ++format;
    av_frame_unref(frame);
   }
  };
  for(;;){
   const uint64_t current_admission=queue.admission();
   if(current_admission!=decode_admission){avcodec_flush_buffers(ctx);decode_admission=current_admission;++session_flushes;}
   Job job;int available=queue.pop(job);if(available<0)break;if(!available)continue;
   const uint64_t admission=queue.admission();
   if((admission>>32)&&(job.info.admission!=admission||static_cast<uint32_t>(job.info.id>>32)!=static_cast<uint32_t>(admission))){++stale;continue;}
   if(admission!=decode_admission){avcodec_flush_buffers(ctx);decode_admission=admission;++session_flushes;}
   if(Clock::now()-job.enqueued>std::chrono::seconds(3)){++expired;continue;}
   // The AVBuffer owns the queue payload. There is no packet-sized memcpy;
   // the budget is released only after the last codec reference disappears.
   auto* owner=new std::shared_ptr<Payload>(job.payload);
   packet->buf=av_buffer_create(job.payload->data.data(),job.payload->data.size(),
    [](void* p,uint8_t*){delete static_cast<std::shared_ptr<Payload>*>(p);},owner,0);
   if(!packet->buf){delete owner;++errors;continue;}
   packet->data=packet->buf->data;packet->size=static_cast<int>(job.bytes);packet->pts=static_cast<int64_t>(++serial);
   packet->opaque_ref=av_buffer_alloc(sizeof(Info));
   if(!packet->opaque_ref){av_packet_unref(packet);++errors;continue;}
   std::memcpy(packet->opaque_ref->data,&job.info,sizeof(Info));
   if(job.info.damaged){packet->flags|=AV_PKT_FLAG_CORRUPT;++marked;}
   int ret=avcodec_send_packet(ctx,packet);
   if(ret==AVERROR(EAGAIN)){receive();ret=avcodec_send_packet(ctx,packet);}
   av_packet_unref(packet);if(ret<0)++errors;receive();
  }
  producer.join();
  if(avcodec_send_packet(ctx,nullptr)<0)++errors;
  receive();
  Metrics display_metrics{};
  if(hdmi){
   display_drain=presenter->drain(3000,&display_error);
   if(!display_drain||input.error)presenter->invalidate_capture("finite ingress/drain failed");
   display_metrics=presenter->metrics();display_stop=presenter->stop(&display_error);
  }
  av_frame_free(&frame);av_packet_free(&packet);avcodec_free_context(&ctx);
  double span=std::chrono::duration<double>(Clock::now()-start).count();utsname machine{};uname(&machine);
  const auto ages=source_age.counts();
  const auto calibration=source_age.calibration();
  std::ofstream report(argv[2]);
  report<<"{\"machine\":\""<<machine.machine<<"\",\"physical\":"<<(hdmi?"true":"false")<<",\"paced\":"<<(std::string(argv[3])=="paced"?"true":"false")
   <<",\"cpu_seconds\":"<<cpu_seconds()-cpu_start<<",\"input_aus\":"<<input.inputs<<",\"known_damaged_aus\":"<<input.damaged
   <<",\"reader_error\":"<<(input.error?"true":"false")<<",\"accepted\":"<<queue.accepted<<",\"dropped\":"<<queue.dropped
   <<",\"pool_payload_cap_bytes\":"<<POOL_MAX<<",\"pool_peak_bytes\":"<<queue.peak_bytes<<",\"pool_peak_owners\":"<<queue.peak_owners
   <<",\"peak_ready\":"<<queue.peak_ready<<",\"owners_after_codec_close\":"<<queue.budget->owners.load()
   <<",\"bytes_after_codec_close\":"<<queue.budget->bytes.load()<<",\"queue_context_bytes\":"<<sizeof(Queue)
   <<",\"payload_context_bytes\":"<<sizeof(Payload)<<",\"info_bytes\":"<<sizeof(Info)
   <<",\"packet_corrupt_marked_aus\":"<<marked<<",\"decoded_frames\":"<<outputs.size()<<",\"decode_errors\":"<<errors
   <<",\"valid_decoded_frames\":"<<valid_decoded<<",\"display_dropped_frames\":"<<display_dropped
   <<",\"display_backlog_skipped_frames\":"<<backlog_presentation_skips
   <<",\"display_flip_age_budget_ns\":"<<ack_capture.flip_budget_ns
   <<",\"display_max_flip_delay_ns\":"<<ack_capture.max_flip_delay_ns
   <<",\"display_flip_budget_overruns\":"<<ack_capture.flip_budget_overruns
   <<",\"display_slot_wait_limit_ms\":50"
   <<",\"HDMI_queued_frames\":"<<display_metrics.queued<<",\"HDMI_physical_frames\":"<<display_metrics.presented_at_hdmi_boundary
   <<",\"HDMI_scanout_repeats\":"<<display_metrics.scanout_repeats<<",\"HDMI_backend_failures\":"<<display_metrics.backend_failures
   <<",\"HDMI_max_new_frame_gap_ns\":"<<display_metrics.max_new_frame_gap_ns<<",\"HDMI_new_frame_gaps_over_1s\":"<<display_metrics.new_frame_gaps_over_1s
   <<",\"HDMI_signal_loss_notices\":"<<display_metrics.signal_loss_notices
   <<",\"HDMI_first_ack_ns\":"<<display_metrics.first_physical_ack_ns<<",\"HDMI_last_ack_ns\":"<<display_metrics.last_physical_ack_ns
   <<",\"HDMI_drain_passed\":"<<(display_drain?"true":"false")<<",\"HDMI_stop_passed\":"<<(display_stop?"true":"false")
   <<",\"decoder_flagged_frames\":"<<flagged<<",\"unknown_identity\":"<<unknown<<",\"format_errors\":"<<format
   <<",\"expired_queue_jobs\":"<<expired<<",\"stale_session_jobs_or_frames\":"<<stale<<",\"session_decoder_flushes\":"<<session_flushes
   <<",\"first_output_seconds\":"<<first_output<<",\"last_output_seconds\":"<<last_output<<",\"coded_pixel_limit\":622592"
   <<",\"source_events\":"<<input.source_events<<",\"source_fragment_accepted\":"<<input.fragments.accepted_fragments
   <<",\"source_fragment_invalid\":"<<input.fragments.invalid_fragments<<",\"source_fragment_duplicates\":"<<input.fragments.duplicate_fragments
   <<",\"source_fragment_expired_units\":"<<input.fragments.expired_units<<",\"source_fragment_skipped_units\":"<<input.fragments.ordered_skipped_units
   <<",\"source_fragment_evicted_units\":"<<input.fragments.evicted_units<<",\"source_fragment_stale\":"<<input.fragments.stale_fragments
   <<",\"source_fragment_peak_units\":"<<input.fragments.peak_units
   <<",\"source_fragment_cache_peak_bytes\":"<<input.fragments.peak_bytes<<",\"source_fragment_current_bytes\":"<<input.fragments.current_bytes
   <<",\"source_AUs_submitted\":"<<input.aus.submitted<<",\"source_AUs_damaged\":"<<input.aus.damaged
   <<",\"source_AUs_output_dropped\":"<<input.aus.output_dropped<<",\"source_clock_mapping_valid\":false,\"wall_seconds\":"<<span
   <<",\"conditional_age_path_bound_ns\":"<<age_path_bound<<",\"conditional_age_DRM_monotonic\":"<<(monotonic_ack?"true":"false")
   <<",\"conditional_age_anchors\":"<<ages.anchors<<",\"conditional_age_rejected_anchors\":"<<ages.rejected
   <<",\"conditional_age_acks\":"<<ages.acks<<",\"conditional_age_unknown\":"<<ages.unknown
   <<",\"conditional_age_within_3s\":"<<ages.within<<",\"conditional_age_over_3s\":"<<ages.late
   <<",\"conditional_age_ambiguous\":"<<ages.ambiguous<<",\"conditional_age_min_ns\":"<<ages.min_age_ns
   <<",\"conditional_age_max_ns\":";
  if(age_lower_only)report<<"null";else report<<ages.max_age_ns;
  report<<",\"conditional_age_lower_only\":"<<(age_lower_only?"true":"false")
   <<",\"source_expiry_enabled\":"<<(enforce_age&&monotonic_ack?"true":"false")
   <<",\"source_expired_presentations\":"<<source_age_dropped+display_metrics.queued_expired
   <<",\"HDMI_queued_expired_frames\":"<<display_metrics.queued_expired
   <<",\"SOURCE_DMA_started\":"<<(input.dma.started?"true":"false")<<",\"SOURCE_DMA_batches\":"<<input.dma.acquired
   <<",\"SOURCE_DMA_continuous\":"<<(continuous?"true":"false")
   <<",\"SOURCE_DMA_timeout\":"<<(input.dma.timed_out?"true":"false")<<",\"SOURCE_DMA_lease_error\":"<<(input.dma.lease_error?"true":"false")
   <<",\"SOURCE_DMA_interrupted\":"<<(input.dma.interrupted?"true":"false")
   <<",\"SOURCE_DMA_tail_batches\":"<<input.dma.tail_batches
   <<",\"SOURCE_DMA_private_copied_bytes\":"<<input.dma.copied_bytes
   <<",\"SOURCE_DMA_bad_metadata\":"<<input.dma.parser.bad_metadata<<",\"SOURCE_DMA_bad_records\":"<<input.dma.parser.bad_records
   <<",\"SOURCE_DMA_missing_batches\":"<<input.dma.parser.missing_batches<<",\"SOURCE_DMA_duplicate_batches\":"<<input.dma.parser.duplicate_batches
   <<",\"SOURCE_records\":"<<input.source_records<<",\"SOURCE_session_rejected\":"<<input.source_session_rejected
   <<",\"SOURCE_auto_session\":"<<(input.source_session_auto?"true":"false")<<",\"SOURCE_sessions_adopted\":"<<input.source_sessions_adopted
   <<",\"source_session_state_bytes\":"<<sizeof(SourceSession)<<",\"source_ingress_context_bytes\":"<<sizeof(SourceIngress)<<",\"source_reader_stats_bytes\":"<<sizeof(ReaderStats)
   <<",\"SOURCE_DMA_stopped_running\":"<<input.dma.stopped.running<<",\"SOURCE_DMA_stopped_queued\":"<<input.dma.stopped.queued
   <<",\"SOURCE_DMA_stopped_user\":"<<input.dma.stopped.user<<",\"SOURCE_DMA_stopped_poisoned\":"<<input.dma.stopped.poisoned
   <<",\"SOURCE_DMA_stopped_ready\":"<<input.dma.stopped.ready
   <<",\"SOURCE_DMA_stopped_pinned\":"<<input.dma.stopped.pinned<<",\"pixels\":[";
  for(size_t i=0;i<outputs.size();++i){if(i)report<<',';report<<"{\"capture_id\":"<<outputs[i].id<<",\"rgb565_crc32\":"<<outputs[i].crc<<'}';}
  report<<"],\"path_calibration_anchors\":[";
  bool wrote_anchor=false;
  auto write_anchor=[&](const SourceAge::CalibrationAnchor& anchor){
   if(wrote_anchor)report<<',';
   wrote_anchor=true;
   report<<"{\"sid\":"<<anchor.sid<<",\"superframe\":"<<anchor.frame
         <<",\"board_rx_lower_ns\":"<<anchor.board_low_ns<<",\"board_rx_upper_ns\":"<<anchor.board_high_ns<<'}';
  };
  for(unsigned i=0;i<calibration.strata.size();++i)
   if((calibration.mask&(1U<<i))&&calibration.strata[i].frame!=calibration.last.frame)write_anchor(calibration.strata[i]);
  if(calibration.mask)write_anchor(calibration.last);
  report<<']';
  if(capture_acks){
   report<<",\"development_ACK_monotonic\":"<<(monotonic_ack?"true":"false")
    <<",\"development_ACK_omitted\":"<<ack_capture.omitted<<",\"development_ACKs\":[";
   for(size_t i=0;i<ack_capture.samples.size();++i){
    if(i)report<<',';
    const auto& row=ack_capture.samples[i];
    report<<'['<<row.id<<','<<row.pts_ns<<','<<row.board_ns;
    if(row.uncertainty_us)report<<','<<row.uncertainty_us;
    report<<']';
   }
   report<<']';
  }
  report<<",\"RF_DMA_HDMI_started\":"<<(input.dma.started||hdmi?"true":"false")<<",\"RF_TX_started\":false,\"HDMI_started\":"<<(hdmi?"true":"false")<<",\"full_G0_or_video_passed\":false}\n";
  if(!report)throw std::runtime_error("report write");
  return input.error||!display_drain||!display_stop?2:0;
 }catch(const std::exception& error){std::cerr<<error.what()<<'\n';av_frame_free(&frame);av_packet_free(&packet);avcodec_free_context(&ctx);return 1;}
}
