// Continuous TX-only UHD 3.15 transport. --simulate never constructs a device.
#include <uhd/usrp/multi_usrp.hpp>
#include <uhd/types/metadata.hpp>
#include <uhd/types/stream_cmd.hpp>
#include <uhd/version.hpp>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <deque>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <sstream>
#include <thread>
#include <unistd.h>
#include <vector>

using cf = std::complex<float>;
static volatile std::sig_atomic_t interrupted = 0;
static void stop_handler(int) { interrupted = 1; }
static std::atomic<bool> abort_run{false};
static uint64_t little(const unsigned char* p, size_t n) {
    uint64_t v=0; for(size_t i=0;i<n;++i) v |= uint64_t(p[i])<<(8*i); return v;
}
static void exact(void* dest, size_t n) {
    auto* p=static_cast<unsigned char*>(dest); size_t at=0;
    auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(10);
    while(at<n) {
        if(interrupted || abort_run) throw std::runtime_error("cancelled while reading IQ pipe");
        pollfd fd{STDIN_FILENO,POLLIN,0}; int ready=poll(&fd,1,100);
        if(ready<0) {if(errno==EINTR) continue; throw std::runtime_error("poll failed");}
        if(!ready) {
            if(std::chrono::steady_clock::now()>deadline) throw std::runtime_error("IQ input timeout");
            continue;
        }
        ssize_t got=read(STDIN_FILENO,p+at,n-at);
        if(got<0) {if(errno==EINTR) continue; throw std::runtime_error("IQ read failed");}
        if(!got) throw std::runtime_error("truncated IQ pipe (explicit EOS required)");
        at+=size_t(got); deadline=std::chrono::steady_clock::now()+std::chrono::seconds(10);
    }
}
static std::string json_quote(const std::string& value) {
    std::ostringstream out;out<<'"';
    for(unsigned char c:value) {
        if(c=='"'||c=='\\') out<<'\\'<<c;
        else if(c<32) out<<"\\u"<<std::hex<<std::setw(4)<<std::setfill('0')<<unsigned(c)<<std::dec;
        else out<<c;
    }
    out<<'"';return out.str();
}
struct Block {uint64_t sequence; std::vector<cf> lanes[2];};
int main(int argc,char** argv) {
    std::map<std::string,std::string> opt;
    bool simulated=false, discover=false, preflight=false, pace=false;
    for(int i=1;i<argc;++i) {
        std::string k=argv[i];
        if(k=="--simulate") {simulated=true; continue;}
        if(k=="--discover") {discover=true; continue;}
        if(k=="--preflight") {preflight=true; continue;}
        if(k=="--simulate-realtime") {pace=true; continue;}
        if(k=="--help") {std::cout<<"TX-only IQB1 pipe: --simulate | --enable-tx YES; --stats FILE --actual FILE; --discover has no TX streamer\n";return 0;}
        const std::vector<std::string> allowed={"--stats","--actual","--addr","--serial","--freq","--bandwidth","--gain","--scale","--queue","--prefill","--max-send-samples","--clock","--time","--enable-tx","--rx-out","--rx-bandwidth","--rx-gain","--max-rx-seconds","--tx-zero-preroll-samples"};
        if(std::find(allowed.begin(),allowed.end(),k)==allowed.end() || i+1>=argc) {std::cerr<<"unknown/missing argument "<<k<<"\n";return 2;}
        opt[k]=argv[++i];
    }
    auto get=[&](const std::string& k,const std::string& d){return opt.count(k)?opt.at(k):d;};
    std::signal(SIGINT,stop_handler); std::signal(SIGTERM,stop_handler);
    unsigned long long input_samples=0,accepted=0,blocks=0,shorts=0,short_samples=0;
    uint64_t fnv=14695981039346656037ull;
    std::atomic<unsigned> underflows{0},late{0},other_async{0};
    bool ack[2]={false,false}, eos=false, complete=false;
    size_t highwater=0; std::string error;
    std::mutex mutex,ack_mutex; std::condition_variable changed; std::deque<Block> queue;
    bool producer_done=false; std::string producer_error;
    std::thread reader,async_reader,rx_reader;
    uhd::rx_streamer::sptr rx;
    uhd::tx_streamer::sptr tx;
    uhd::usrp::multi_usrp::sptr u;
    std::atomic<bool> async_stop{false},rx_stop{false};
    std::atomic<double> rx_end{1e99};
    unsigned long long rx_samples=0,preroll_samples=0;
    std::string rx_error;
    auto began=std::chrono::steady_clock::now(); double timed_start=0;
    try {
        uint16_t endian=1;
        if(*reinterpret_cast<unsigned char*>(&endian)!=1||sizeof(float)!=4||sizeof(cf)!=8)
            throw std::runtime_error("IQB1 transport requires little-endian float32 complex pairs");
        const double freq=std::stod(get("--freq","5500000000"));
        const double bandwidth=std::stod(get("--bandwidth","40000000"));
        const bool capture=opt.count("--rx-out"),check_rx=capture||opt.count("--rx-bandwidth");
        const double rx_bw=std::stod(get("--rx-bandwidth","40000000")),rx_gain=std::stod(get("--rx-gain","20"));
        const double max_rx=std::stod(get("--max-rx-seconds","60"));
        const size_t zero_preroll=std::stoul(get("--tx-zero-preroll-samples","0"));
        if(zero_preroll>4000000 || (simulated&&zero_preroll))
            throw std::runtime_error("invalid diagnostic TX zero preroll");
        if((simulated&&(capture||preflight))||!std::isfinite(rx_bw)||rx_bw<=0||rx_bw>40e6||!std::isfinite(rx_gain)||rx_gain<0||rx_gain>30||!std::isfinite(max_rx)||max_rx<1||max_rx>120)
            throw std::runtime_error("invalid receiver/preflight configuration");
        const double gain=std::stod(get("--gain","0")),scale=std::stod(get("--scale","0.5"));
        const size_t capacity=std::stoul(get("--queue","8")),prefill=std::stoul(get("--prefill","4"));
        const size_t maxsend=std::stoul(get("--max-send-samples","65536"));
        if(!std::isfinite(freq)||freq<1.2e9||freq>6e9||!std::isfinite(bandwidth)||bandwidth<=0||bandwidth>40e6||!std::isfinite(gain)||gain<0||gain>12||!std::isfinite(scale)||scale<=0||scale>1||capacity<1||capacity>64||prefill<1||prefill>capacity||maxsend<1||maxsend>65536)
            throw std::runtime_error("outside bounded transport configuration");
        if(!simulated && !discover && !preflight && get("--enable-tx","NO")!="YES")
            throw std::runtime_error("TX disabled; explicit --enable-tx YES required");
        if(!simulated) {
            u=uhd::usrp::multi_usrp::make("type=x300,addr="+get("--addr","192.168.10.2"));
            if(discover) {
                std::cout<<u->get_pp_string()<<"\n";
                for(size_t ch=0;ch<u->get_tx_num_channels();++ch)
                    std::cout<<"TX "<<ch<<" bandwidth readback "<<u->get_tx_bandwidth(ch)<<" Hz\n";
                return 0;
            }
            u->set_clock_source(get("--clock","internal"));u->set_time_source(get("--time","internal"));
            u->set_tx_subdev_spec(uhd::usrp::subdev_spec_t("A:0 B:0"));
            if(u->get_tx_num_channels()<2) throw std::runtime_error("two TX channels required");
            auto serial=u->get_usrp_tx_info(0).get("mboard_serial","");
            if(serial!=get("--serial","3095BA5")) throw std::runtime_error("unexpected USRP serial");
            for(size_t ch=0;ch<2;++ch) {
                u->set_tx_rate(4000000,ch);u->set_tx_freq(freq,ch);u->set_tx_gain(gain,ch);
                u->set_tx_bandwidth(bandwidth,ch);u->set_tx_antenna("TX/RX",ch);
            }
            if(check_rx) {
                u->set_rx_subdev_spec(uhd::usrp::subdev_spec_t("A:0 B:0"));
                if(u->get_rx_num_channels()!=2) throw std::runtime_error("two RX channels required");
                for(size_t ch=0;ch<2;++ch) {
                    u->set_rx_rate(4e6,ch);u->set_rx_freq(freq,ch);u->set_rx_gain(rx_gain,ch);
                    u->set_rx_bandwidth(rx_bw,ch);u->set_rx_antenna("RX2",ch);
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(300));
            std::ofstream a(get("--actual","actual.json"));
            if(!a) throw std::runtime_error("cannot write actual configuration");
            a<<std::setprecision(17)<<"{\"serial\":"<<json_quote(serial)<<",\"uhd_version\":"<<json_quote(uhd::get_version_string())<<",\"device\":"<<json_quote(u->get_pp_string())<<",\"clock_source\":"<<json_quote(u->get_clock_source(0))<<",\"time_source\":"<<json_quote(u->get_time_source(0))<<",\"channels\":[";
            bool match=u->get_clock_source(0)==get("--clock","internal")&&u->get_time_source(0)==get("--time","internal");
            for(size_t ch=0;ch<2;++ch) {
                double r=u->get_tx_rate(ch),f=u->get_tx_freq(ch),b=u->get_tx_bandwidth(ch),g=u->get_tx_gain(ch);
                bool locked=u->get_tx_sensor("lo_locked",ch).to_bool();
                if(ch) a<<",";
                a<<"{\"channel\":"<<ch<<",\"sample_rate_hz\":"<<r<<",\"frequency_hz\":"<<f<<",\"bandwidth_hz\":"<<b<<",\"gain_db\":"<<g<<",\"antenna\":"<<json_quote(u->get_tx_antenna(ch))<<",\"lo_locked\":"<<(locked?"true":"false")<<"}";
                match=match&&std::abs(r-4e6)<=1&&std::abs(f-freq)<=1&&std::abs(b-bandwidth)<=1&&std::abs(g-gain)<=.01&&locked;
            }
            a<<"],\"rx_channels\":[";
            if(check_rx) for(size_t ch=0;ch<2;++ch) {
                double r=u->get_rx_rate(ch),f=u->get_rx_freq(ch),b=u->get_rx_bandwidth(ch),g=u->get_rx_gain(ch);
                bool locked=u->get_rx_sensor("lo_locked",ch).to_bool();
                if(ch) a<<",";
                a<<"{\"channel\":"<<ch<<",\"sample_rate_hz\":"<<r<<",\"frequency_hz\":"<<f<<",\"bandwidth_hz\":"<<b<<",\"gain_db\":"<<g<<",\"antenna\":"<<json_quote(u->get_rx_antenna(ch))<<",\"lo_locked\":"<<(locked?"true":"false")<<"}";
                match=match&&std::abs(r-4e6)<=1&&std::abs(f-freq)<=1&&std::abs(b-rx_bw)<=1&&std::abs(g-rx_gain)<=.01&&locked;
            }
            a<<"],\"configuration_matches_requested\":"<<(match?"true":"false")<<"}\n";a.close();
            if(!match) throw std::runtime_error("actual RF configuration differs from requested; no TX streamer started (check bandwidth coercion)");
            if(preflight) {std::cout<<"TX/RX configuration verified; no streamer created, no RF transmitted\n";return 0;}
            uhd::stream_args_t args("fc32","sc16");args.channels={0,1};tx=u->get_tx_stream(args);
            if(capture) rx=u->get_rx_stream(args);
        }
        reader=std::thread([&]{try {
            uint64_t sequence=0;
            while(!interrupted&&!abort_run) {
                unsigned char head[16];exact(head,sizeof(head));
                if(std::memcmp(head,"IQB1",4)||little(head+4,8)!=sequence++) throw std::runtime_error("IQ block identity/order error");
                const size_t n=little(head+12,4);
                if(!n) {std::lock_guard<std::mutex> l(mutex);eos=true;break;}
                if(n>65536) throw std::runtime_error("unbounded IQ block");
                std::vector<float> raw(n*4);exact(raw.data(),raw.size()*sizeof(float));
                Block block;block.sequence=sequence-1;
                for(auto& lane:block.lanes) lane.resize(n);
                for(size_t i=0;i<n;++i) for(size_t ch=0;ch<2;++ch) {
                    float re=raw[4*i+2*ch],im=raw[4*i+2*ch+1];
                    if(!std::isfinite(re)||!std::isfinite(im)||std::abs(re)>.125001f||std::abs(im)>.125001f) throw std::runtime_error("invalid canonical IQ magnitude");
                    block.lanes[ch][i]=cf(re,im)*float(scale);
                }
                std::unique_lock<std::mutex> l(mutex);
                while(queue.size()>=capacity&&!abort_run&&!interrupted) changed.wait_for(l,std::chrono::milliseconds(100));
                if(abort_run||interrupted) break;
                input_samples+=n;queue.push_back(std::move(block));highwater=std::max(highwater,queue.size());changed.notify_all();
            }
        }catch(const std::exception& e){std::lock_guard<std::mutex> l(mutex);producer_error=e.what();abort_run=true;}
        {std::lock_guard<std::mutex> l(mutex);producer_done=true;}changed.notify_all();});
        {
            std::unique_lock<std::mutex> l(mutex);
            if(!changed.wait_for(l,std::chrono::seconds(15),[&]{return queue.size()>=prefill||producer_done||abort_run||interrupted;})) throw std::runtime_error("prefill timeout");
        }
        if(abort_run||interrupted) throw std::runtime_error("cancelled before timed start");
        timed_start=simulated?0:u->get_time_now().get_real_secs()+.8;
        if(rx) {
            const double rx_start=timed_start-.1;
            uhd::stream_cmd_t command(uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS);
            command.stream_now=false;command.time_spec=uhd::time_spec_t(rx_start);rx->issue_stream_cmd(command);
            rx_reader=std::thread([&,rx_start,max_rx]{try {
                const std::string prefix=get("--rx-out","");
                std::ofstream files[2],ledger(prefix+".blocks.jsonl");
                for(size_t ch=0;ch<2;++ch) {
                    files[ch].open(prefix+".rx"+std::to_string(ch)+".cf32",std::ios::binary);
                    if(!files[ch]) throw std::runtime_error("cannot create RX capture");
                }
                if(!ledger) throw std::runtime_error("cannot create RX timestamp ledger");
                std::vector<cf> buffers[2];for(auto& b:buffers) b.resize(16384);
                while(!rx_stop&&!interrupted&&!abort_run) {
                    std::vector<void*> ptr={buffers[0].data(),buffers[1].data()};uhd::rx_metadata_t md;
                    size_t n=rx->recv(ptr,16384,md,.25);
                    if(md.error_code==uhd::rx_metadata_t::ERROR_CODE_TIMEOUT&&u->get_time_now().get_real_secs()<rx_start+.5) continue;
                    if(md.error_code!=uhd::rx_metadata_t::ERROR_CODE_NONE) throw std::runtime_error(md.strerror());
                    if(!n||!md.has_time_spec||std::abs(md.time_spec.get_real_secs()-(rx_start+rx_samples/4e6))>1.5/4e6)
                        throw std::runtime_error("RX timestamp discontinuity");
                    ledger<<std::setprecision(17)<<"{\"sample_offset\":"<<rx_samples<<",\"samples\":"<<n<<",\"device_seconds\":"<<md.time_spec.get_real_secs()<<"}\n";
                    for(size_t ch=0;ch<2;++ch) {
                        files[ch].write(reinterpret_cast<const char*>(buffers[ch].data()),n*sizeof(cf));
                        if(!files[ch]) throw std::runtime_error("RX capture write failed");
                    }
                    rx_samples+=n;
                    if(rx_start+rx_samples/4e6>=rx_end) break;
                    if(rx_samples>=max_rx*4e6) throw std::runtime_error("bounded RX capture duration exceeded");
                }
                for(auto& f:files) {f.flush();if(!f) throw std::runtime_error("RX capture flush failed");}
                ledger.flush();if(!ledger) throw std::runtime_error("RX ledger write failed");
            }catch(const std::exception& e) {rx_error=e.what();abort_run=true;changed.notify_all();}});
        }
        if(tx) async_reader=std::thread([&]{try {while(!async_stop) {
            uhd::async_metadata_t md;
            if(!tx->recv_async_msg(md,.1)) continue;
            if(md.event_code==uhd::async_metadata_t::EVENT_CODE_BURST_ACK) {std::lock_guard<std::mutex> l(ack_mutex);if(md.channel<2) ack[md.channel]=true;}
            else {
                if(md.event_code==uhd::async_metadata_t::EVENT_CODE_UNDERFLOW||md.event_code==uhd::async_metadata_t::EVENT_CODE_UNDERFLOW_IN_PACKET) ++underflows;
                else if(md.event_code==uhd::async_metadata_t::EVENT_CODE_TIME_ERROR) ++late;
                else ++other_async;
                abort_run=true;changed.notify_all();
            }
        }}catch(const std::exception&) {++other_async;abort_run=true;changed.notify_all();}});
        bool first=true;
        if(tx && (rx || zero_preroll)) {
            // Startup zeros only; the subsequent canonical RF frames are contiguous.
            std::vector<cf> zero(16384);preroll_samples=zero_preroll?zero_preroll:400000;
            size_t at=0;
            while(at<preroll_samples&&!abort_run&&!interrupted) {
                uhd::tx_metadata_t md;md.start_of_burst=first;md.has_time_spec=first;md.time_spec=uhd::time_spec_t(timed_start);
                std::vector<const void*> ptr={zero.data(),zero.data()};
                size_t n=tx->send(ptr,std::min<size_t>(zero.size(),preroll_samples-at),md,2.0);
                if(!n) throw std::runtime_error("TX startup zero write failed");
                first=false;at+=n;
            }
        }
        while(!interrupted&&!abort_run) {
            Block block;
            {
                std::unique_lock<std::mutex> l(mutex);
                if(!changed.wait_for(l,std::chrono::seconds(5),[&]{return !queue.empty()||producer_done||abort_run||interrupted;})) throw std::runtime_error("bounded TX queue starved");
                if(queue.empty()) break;
                block=std::move(queue.front());queue.pop_front();changed.notify_all();
            }
            size_t at=0,n=block.lanes[0].size();
            while(at<n&&!interrupted&&!abort_run) {
                size_t request=n-at,wrote=0;
                if(simulated) {
                    wrote=std::min(request,maxsend);
                    for(size_t i=at;i<at+wrote;++i) for(size_t ch=0;ch<2;++ch) {
                        const auto* bytes=reinterpret_cast<const unsigned char*>(&block.lanes[ch][i]);
                        for(size_t j=0;j<sizeof(cf);++j) {fnv^=bytes[j];fnv*=1099511628211ull;}
                    }
                    if(pace) std::this_thread::sleep_for(std::chrono::duration<double>(double(wrote)/4e6));
                } else {
                    uhd::tx_metadata_t md;md.start_of_burst=first;md.end_of_burst=false;md.has_time_spec=first;md.time_spec=uhd::time_spec_t(timed_start);
                    std::vector<const void*> ptr={block.lanes[0].data()+at,block.lanes[1].data()+at};
                    wrote=tx->send(ptr,request,md,2.0);
                }
                if(!wrote||wrote>request) throw std::runtime_error("TX zero/invalid short write");
                if(wrote<request) {++shorts;short_samples+=request-wrote;}
                first=false;at+=wrote;accepted+=wrote;
            }
            if(at!=n) throw std::runtime_error("interrupted/async failure inside IQ block");
            ++blocks;
        }
        if(tx) {
            rx_end=timed_start+(preroll_samples+accepted)/4e6+.02;
            cf zero;std::vector<const void*> ptr={&zero,&zero};uhd::tx_metadata_t md;md.end_of_burst=true;tx->send(ptr,0,md,.5);
            auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
            while(std::chrono::steady_clock::now()<deadline) {
                {std::lock_guard<std::mutex> l(ack_mutex);if(ack[0]&&ack[1]) break;}
                if(interrupted) break;
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
        }
        if(reader.joinable()) reader.join();
        if(rx_reader.joinable()) rx_reader.join();
        if(rx) {uhd::stream_cmd_t stop(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS);rx->issue_stream_cmd(stop);}
        async_stop=true;if(async_reader.joinable()) async_reader.join();
        if(!producer_error.empty()) throw std::runtime_error(producer_error);
        if(!rx_error.empty()) throw std::runtime_error("RX: "+rx_error);
        if(interrupted||abort_run||underflows||late||other_async||!eos||accepted!=input_samples||accepted==0) throw std::runtime_error("incomplete continuous TX session");
        if(tx) {std::lock_guard<std::mutex> l(ack_mutex);if(!ack[0]||!ack[1]) throw std::runtime_error("missing per-channel end-of-burst ACK");}
        complete=true;
    }catch(const std::exception& e){error=e.what();abort_run=true;changed.notify_all();
        if(tx) try {cf zero;std::vector<const void*> ptr={&zero,&zero};uhd::tx_metadata_t md;md.end_of_burst=true;tx->send(ptr,0,md,.2);}catch(...){}
    }
    abort_run=true;changed.notify_all();rx_stop=true;
    if(rx_reader.joinable()) rx_reader.join();
    if(rx) try {uhd::stream_cmd_t stop(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS);rx->issue_stream_cmd(stop);}catch(...){}
    if(reader.joinable()) reader.join();
    async_stop=true;if(async_reader.joinable()) async_reader.join();
    double elapsed=std::chrono::duration<double>(std::chrono::steady_clock::now()-began).count();
    std::ofstream stats(get("--stats","transport.json"));
    stats<<std::setprecision(17)<<"{\"schema\":\"mrf1-continuous-tx-v1\",\"simulated\":"<<(simulated?"true":"false")<<",\"transport_pass\":"<<(complete?"true":"false")<<",\"interrupted\":"<<(interrupted?"true":"false")<<",\"input_samples_per_channel\":"<<input_samples<<",\"accepted_samples_per_channel\":"<<accepted<<",\"blocks_accepted\":"<<blocks<<",\"partial_send_events\":"<<shorts<<",\"short_sample_tails_retried\":"<<short_samples<<",\"underflows\":"<<underflows.load()<<",\"late_errors\":"<<late.load()<<",\"other_async_errors\":"<<other_async.load()<<",\"queue_high_water_blocks\":"<<highwater<<",\"burst_ack_channels\":["<<(ack[0]?"true":"false")<<","<<(ack[1]?"true":"false")<<"],\"timed_start_device_seconds\":"<<timed_start<<",\"elapsed_seconds\":"<<elapsed<<",\"simulated_accepted_fnv1a64\":\""<<std::hex<<fnv<<std::dec<<"\",\"error\":"<<json_quote(error)<<",\"rx_samples_per_channel\":"<<rx_samples<<",\"tx_startup_zero_samples\":"<<preroll_samples<<",\"rx_error\":"<<json_quote(rx_error)<<"}\n";
    if(!stats.good()) {std::cerr<<"cannot save transport statistics\n";return 2;}
    if(!error.empty()) std::cerr<<error<<"\n";
    return complete?0:1;
}
