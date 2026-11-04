#pragma once
#include <algorithm>
#include <cmath>
#include <limits>

// Outward-rounded rolling sums. This is a rejection accelerator only:
// candidates not provably below the ORIGINAL repetition threshold still run
// its original sequential calculation and the unchanged public header gates.
namespace repetition_bound {
struct Sum {
  double low{0},high{0};
  void add(double x) {
    low=std::nextafter(low+x,-std::numeric_limits<double>::infinity());
    high=std::nextafter(high+x,std::numeric_limits<double>::infinity());
  }
};
struct Window {
  Sum re,im,energy_a,energy_b;
  void add(double r,double i,double a,double b) {
    re.add(r);im.add(i);energy_a.add(a);energy_b.add(b);
  }
  double upper(double sequential_rounding_bound) const {
    const double up=std::numeric_limits<double>::infinity();
    const double down=-up;
    const double r=std::nextafter(std::max(std::abs(re.low),std::abs(re.high))+sequential_rounding_bound,up);
    const double i=std::nextafter(std::max(std::abs(im.low),std::abs(im.high))+sequential_rounding_bound,up);
    const double numerator=std::nextafter(std::nextafter(r*r,up)+std::nextafter(i*i,up),up);
    const double a=std::max(0.0,std::nextafter(energy_a.low-sequential_rounding_bound,down));
    const double b=std::max(0.0,std::nextafter(energy_b.low-sequential_rounding_bound,down));
    const double denominator=std::max(1e-30,std::nextafter(a*b,down));
    return std::nextafter(numerator/denominator,up);
  }
};
} // namespace repetition_bound
