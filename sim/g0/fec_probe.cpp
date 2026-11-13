// G0 systematic GF(256) erasure-code reference. Host experiment only: the
// final board FEC must be PL, not this program running on PS.
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <numeric>
#include <random>
#include <stdexcept>
#include <string>
#include <vector>

using Bytes = std::vector<uint8_t>;
using Matrix = std::vector<Bytes>;
using Symbols = std::vector<Bytes>;
struct Galois {
  std::array<uint8_t,65536> product{};
  std::array<uint8_t,256> inverse{};
  static uint8_t polynomial(uint8_t a, uint8_t b) {
    unsigned x=a, y=b, z=0;
    while(y) { if(y&1) z^=x; y>>=1; x<<=1; if(x&256) x^=0x11d; }
    return static_cast<uint8_t>(z);
  }
  Galois() {
    std::array<unsigned,512> ex{};
    std::array<unsigned,256> log{};
    unsigned x=1;
    for(unsigned i=0;i<255;++i) { ex[i]=x;log[x]=i; x<<=1;if(x&256)x^=0x11d; }
    for(unsigned i=255;i<512;++i)ex[i]=ex[i-255];
    for(unsigned a=0;a<256;++a)for(unsigned b=0;b<256;++b) {
      product[(a<<8)|b]=a&&b ? ex[log[a]+log[b]] : 0;
      if(product[(a<<8)|b]!=polynomial(a,b))throw std::runtime_error("GF multiplication oracle mismatch");
    }
    for(unsigned a=1;a<256;++a)inverse[a]=ex[255-log[a]];
  }
  uint8_t mul(uint8_t a,uint8_t b) const { return product[(unsigned(a)<<8)|b]; }
};
static const Galois gf;

static Matrix invert(Matrix matrix) {
  const size_t n=matrix.size(); Matrix inv(n,Bytes(n));
  for(size_t i=0;i<n;++i)inv[i][i]=1;
  for(size_t c=0;c<n;++c) {
    size_t pivot=c;
    while(pivot<n && matrix[pivot][c]==0)++pivot;
    if(pivot==n)throw std::runtime_error("singular coding matrix");
    std::swap(matrix[c],matrix[pivot]);std::swap(inv[c],inv[pivot]);
    auto scale=gf.inverse[matrix[c][c]];
    for(size_t j=0;j<n;++j) {matrix[c][j]=gf.mul(matrix[c][j],scale); inv[c][j]=gf.mul(inv[c][j],scale);}
    for(size_t r=0;r<n;++r)if(r!=c) {
      auto factor=matrix[r][c];
      if(!factor)continue;
      for(size_t j=0;j<n;++j) {matrix[r][j]^=gf.mul(factor,matrix[c][j]); inv[r][j]^=gf.mul(factor,inv[c][j]);}
    }
  }
  return inv;
}

static Matrix generator(unsigned k,unsigned n) {
  Matrix v(n,Bytes(k));
  for(unsigned r=0;r<n;++r) {
    v[r][0]=1;
    for(unsigned c=1;c<k;++c)v[r][c]=gf.mul(v[r][c-1],r+1);
  }
  Matrix top(v.begin(),v.begin()+k), inv=invert(top), g(n,Bytes(k));
  for(unsigned r=0;r<n;++r)for(unsigned c=0;c<k;++c)for(unsigned j=0;j<k;++j)
    g[r][c]^=gf.mul(v[r][j],inv[j][c]);
  for(unsigned r=0;r<k;++r)for(unsigned c=0;c<k;++c)
    if(g[r][c]!=(r==c))throw std::runtime_error("generator is not systematic");
  return g;
}

static Bytes combine(const Bytes& row,const Symbols& data,unsigned bytes) {
  Bytes result(bytes);
  for(size_t c=0;c<row.size();++c) {
    auto coefficient=row[c];
    if(!coefficient)continue;
    const auto* products=gf.product.data()+(unsigned(coefficient)<<8);
    for(unsigned i=0;i<bytes;++i)result[i]^=products[data[c][i]];
  }
  return result;
}

static Symbols encode(const Matrix& g,const Symbols& source,unsigned bytes) {
  Symbols result=source;
  for(size_t r=source.size();r<g.size();++r)result.push_back(combine(g[r],source,bytes));
  return result;
}

using Packet = std::pair<unsigned,Bytes>;
static bool recover(const Matrix& g,unsigned k,unsigned bytes,const std::vector<Packet>& received,Symbols& output) {
  std::vector<const Bytes*> unique(g.size(),nullptr);
  std::vector<unsigned> ids;
  output.clear();
  for(const auto& item:received) {
    if(item.first>=g.size() || item.second.size()!=bytes)return false;
    if(unique[item.first]) { if(*unique[item.first]!=item.second)return false; continue; }
    unique[item.first]=&item.second;ids.push_back(item.first);
  }
  if(ids.size()<k)return false;
  Matrix selected;Symbols selected_data;
  for(unsigned i=0;i<k;++i) {selected.push_back(g[ids[i]]);selected_data.push_back(*unique[ids[i]]);}
  auto inverse=invert(selected);
  for(unsigned r=0;r<k;++r)
    output.push_back(unique[r] ? *unique[r] : combine(inverse[r],selected_data,bytes));
  // Additional independent equations check block consistency. Bad link CRC
  // must already have been erased; FEC alone cannot detect arbitrary bad bytes.
  for(unsigned i=k;i<ids.size();++i)
    if(combine(g[ids[i]],output,bytes)!=*unique[ids[i]]) {output.clear();return false;}
  return true;
}

int main(int argc,char** argv) {
  try {
    if(argc!=6)throw std::runtime_error("usage: fec-probe K N SYMBOL_BYTES CASES OUTPUT_PREFIX");
    const unsigned k=std::stoul(argv[1]),n=std::stoul(argv[2]),bytes=std::stoul(argv[3]),cases=std::stoul(argv[4]);
    if(!k || k>n || n>255 || !bytes || bytes>4096 || !cases || cases>10000)throw std::runtime_error("bounded candidate parameters required");
    auto g=generator(k,n);
    std::mt19937 random(9361310);
    double encode_seconds=0, decode_seconds=0;
    unsigned recovered_cases=0,unrecoverable_cases=0;
    auto now=[](){return std::chrono::steady_clock::now();};
    for(unsigned iteration=0;iteration<cases;++iteration) {
      Symbols source(k,Bytes(bytes));
      for(auto& symbol:source)for(auto& b:symbol)b=static_cast<uint8_t>(random());
      auto begin=now();auto encoded=encode(g,source,bytes);
      encode_seconds+=std::chrono::duration<double>(now()-begin).count();
      std::vector<unsigned> ids(n);std::iota(ids.begin(),ids.end(),0);std::shuffle(ids.begin(),ids.end(),random);
      const unsigned erase=iteration%(n-k+1);
      ids.resize(n-erase);
      if(iteration%3==0)std::stable_sort(ids.begin(),ids.end(),[k](unsigned a,unsigned b){return (a>=k)>(b>=k);});
      std::vector<Packet> received;
      for(auto id:ids)received.emplace_back(id,encoded[id]);
      received.push_back(received.front()); // identical duplicates are idempotent
      Symbols restored;
      begin=now();bool ok=recover(g,k,bytes,received,restored);
      decode_seconds+=std::chrono::duration<double>(now()-begin).count();
      if(!ok || restored!=source)throw std::runtime_error("recoverable output mismatch");
      ++recovered_cases;
      received.clear();for(unsigned i=0;i<k-1;++i)received.emplace_back(i,encoded[i]);
      if(recover(g,k,bytes,received,restored))throw std::runtime_error("insufficient symbols declared recoverable");
      ++unrecoverable_cases;
    }
    const std::string prefix=argv[5];
    Symbols golden(k,Bytes(bytes));
    for(unsigned s=0;s<k;++s)for(unsigned b=0;b<bytes;++b)golden[s][b]=static_cast<uint8_t>(s*29+b*17+b/11);
    auto encoded=encode(g,golden,bytes);
    std::ofstream raw(prefix+".encoded.bin",std::ios::binary);
    for(auto& symbol:encoded)raw.write(reinterpret_cast<const char*>(symbol.data()),symbol.size());
    if(!raw)throw std::runtime_error("golden write failed");
    std::ofstream report(prefix+".json");
    report << "{\"scope\":\"G0 host reference only, not PL throughput or G1 acceptance\",\"k\":" << k
           << ",\"n\":" << n << ",\"symbol_bytes\":" << bytes << ",\"gf_polynomial\":285,"
           << "\"generator_definition\":\"V[r,c]=(r+1)^c in GF256; G=V*inverse(V[0:k,:]); bytes in symbol-major order\","
           << "\"gf_multiply_independent_cases\":65536,\"recovery_cases\":" << recovered_cases
           << ",\"insufficient_cases\":" << unrecoverable_cases
           << ",\"host_encode_source_mib_per_s\":" << double(k)*bytes*cases/(1<<20)/encode_seconds
           << ",\"host_recover_source_mib_per_s\":" << double(k)*bytes*cases/(1<<20)/decode_seconds
           << ",\"golden_source_pattern\":\"(symbol*29+byte*17+floor(byte/11)) mod 256\",\"matrix_rows\":[";
    for(size_t r=0;r<g.size();++r) {
      if(r)report << ',';
      report << '[';
      for(size_t c=0;c<g[r].size();++c) {if(c)report << ',';report << unsigned(g[r][c]);}
      report << ']';
    }
    report << "],\"g0_passed\":false}\n";
    if(!report)throw std::runtime_error("report write failed");
    std::cout << "FEC_PROBE_PASS k=" << k << " n=" << n << " cases=" << cases << '\n';
    return 0;
  } catch(const std::exception& e) {std::cerr << e.what() << '\n';return 1;}
}
