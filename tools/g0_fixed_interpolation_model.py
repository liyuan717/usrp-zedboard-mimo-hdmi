#!/usr/bin/env python3
"""Opt-in integer final-PHY sampler for the G0 NCO/RRC software islands.

The capture-relative lfilter array index is the ADC window ENDPOINT. Hardware
RRC/interpolator tags instead identify its center (endpoint - 20). This model
does not implement bounded history, handshakes, or estimator service time.
Public acquisition still uses its original floating interpolation.
"""
import math
import numpy as np

Q32 = 1 << 32
Q16 = 1 << 16
MIN20 = -(1 << 19)
MAX20 = (1 << 19) - 1


def nearest_q(value, bits):
    """Exactly round the supplied binary float to Qbits; ties go to +infinity."""
    if not math.isfinite(float(value)):
        raise ValueError('non-finite timing input')
    numerator, denominator = float(value).as_integer_ratio()
    return (2 * numerator * (1 << bits) + denominator) // (2 * denominator)


def q32_to_q16(position_q32):
    """Round the complete coordinate, preserving fractional carry/negative floor."""
    position_q32 = np.asarray(position_q32)
    if not np.issubdtype(position_q32.dtype, np.integer):
        raise ValueError('Q32 positions must be integers')
    if np.any(position_q32 > np.iinfo(np.int64).max - 32768):
        raise ValueError('diagnostic int64 Q32 coordinate overflow')
    position_q16 = (position_q32.astype(np.int64) + 32768) // Q16
    return position_q16 // Q16, position_q16 % Q16


def quantized_positions(peak, step, period, preamble_start):
    """Quantize peak/step separately, then accumulate including negative offsets.

P32(k) = round(peak*2^32) + (k-preamble_start)*round(step*2^32).
Do not quantize a previously floating-computed positions[0]: the early period
padding is an intentional negative offset from the preamble anchor. Every
period reanchors from that period's causal timing estimate. This software
array adapter uses checked int64 capture-relative coordinates, NOT arbitrary
48-bit absolute ADC tags (the eventual RTL accumulator needs 48+32 bits).
"""
    if not isinstance(period, (int, np.integer)) or period <= 0:
        raise ValueError('invalid period')
    if not isinstance(preamble_start, (int, np.integer)) or not 0 <= preamble_start < period:
        raise ValueError('invalid preamble anchor')
    if not 4 * (1 - .0002) <= step <= 4 * (1 + .0002):
        raise ValueError('step outside candidate 4 samples/symbol +/-200ppm')
    anchor = nearest_q(peak, 32)
    increment = nearest_q(step, 32)
    first = anchor - int(preamble_start) * increment
    last = first + (int(period) - 1) * increment
    if first < 0 or last > np.iinfo(np.int64).max - 32768:
        raise ValueError('capture-relative Q32 coordinate outside diagnostic range')
    positions_q32 = first + np.arange(period, dtype=np.int64) * increment
    index, weight = q32_to_q16(positions_q32)
    positions = index.astype(float) + weight.astype(float) / Q16
    residual = (index * Q16 + weight) * Q16 - positions_q32
    delta_max = max(preamble_start, period - 1 - preamble_start)
    info = {
        'coordinate': 'capture-relative ADC endpoint; RTL center tag = endpoint - 20',
        'position_equation': 'peak_q32 + (symbol_index-preamble_start)*step_q32',
        'rounding': 'nearest; exact half toward positive infinity',
        'peak_q32': int(anchor), 'step_q32': int(increment),
        'timing_origin_sample': anchor / Q32,
        'first_position_q32': int(first), 'last_position_q32': int(last),
        'first_endpoint': int(index[0]), 'last_endpoint': int(index[-1]),
        'Q32_anchor_and_step_error_bound_samples': (1 + delta_max) / (2 * Q32),
        'Q16_weight_max_error_from_Q32_samples': int(abs(residual).max()) / Q32,
        'Q16_fraction_carry_count': int(np.count_nonzero(index != positions_q32 // Q32)),
        'symbol_count': int(period),
        'timing_origin': 'quantized peak_q32/2^32; no original float peak mixed into symbol times',
        'full_48bit_absolute_tag_accumulator_implemented': False,
    }
    return index, weight, positions, positions_q32, info


def exact_signed20_lanes(values):
    """Undo the frontend's exact /2 adapter without silently re-rounding mf."""
    values = np.asarray(values)
    if values.ndim != 2 or values.shape[1] != 2:
        raise ValueError('expected two complex RRC lanes')
    scaled = np.empty((len(values), 4), dtype=float)
    scaled[:, 0::2] = 2 * values.real
    scaled[:, 1::2] = 2 * values.imag
    if not np.all(np.isfinite(scaled)) or np.any(scaled < MIN20) or np.any(scaled > MAX20):
        raise ValueError('RRC adapter is not finite signed20')
    if not np.all(scaled == np.floor(scaled)):
        raise ValueError('RRC adapter is not exactly integer/2; refusing implicit rounding')
    lanes = scaled.astype(np.int64)
    if not np.array_equal(lanes.astype(float), scaled):
        raise ValueError('RRC adapter integer conversion is not exact')
    return lanes


def interpolate_lanes(x0, x1, weight, output_fraction_bits=1):
    """Signed20 input/Q16 weight; signed(20+F) output, ties toward +infinity.

F=1 retains the smallest extra precision that removed the measured r3 packet
271 regression from F=0. Explicit F=0 remains available to reproduce it; F=2
is an alternate numerical control. The input/history width does not change.
"""
    if not isinstance(output_fraction_bits, (int, np.integer)) or not 0 <= output_fraction_bits <= 2:
        raise ValueError('candidate output fraction bits must be0,1,or2')
    x0, x1, weight = np.asarray(x0), np.asarray(x1), np.asarray(weight)
    if x0.shape != x1.shape or x0.ndim != 2 or x0.shape[1] != 4 or weight.shape != (len(x0),):
        raise ValueError('interpolation shape')
    if any(not np.issubdtype(v.dtype, np.integer) for v in (x0, x1, weight)):
        raise ValueError('interpolation ports require integers')
    if np.any(x0 < MIN20) or np.any(x0 > MAX20) or np.any(x1 < MIN20) or np.any(x1 > MAX20):
        raise ValueError('interpolation input outside signed20')
    if np.any(weight < 0) or np.any(weight >= Q16):
        raise ValueError('interpolation weight outside unsigned16')
    x0, x1, weight = x0.astype(np.int64), x1.astype(np.int64), weight.astype(np.int64)
    scale = 1 << output_fraction_bits
    shift = 16 - output_fraction_bits
    result = x0 * scale + ((x1 - x0) * weight[:, None] + (1 << (shift - 1))) // (1 << shift)
    if np.any(result < MIN20 * scale) or np.any(result > MAX20 * scale):
        raise ValueError('interpolation convex-range invariant failed')
    return result


def fixed_sampler(mf, peak, step, period, preamble_start, output_fraction_bits=1):
    index, weight, positions, _, info = quantized_positions(peak, step, period, preamble_start)
    if np.any(index < 0) or np.any(index + 1 >= len(mf)):
        raise ValueError('interpolation needs both endpoints; no np.interp edge hold')
    x0 = exact_signed20_lanes(mf[index])
    x1 = exact_signed20_lanes(mf[index + 1])
    result = interpolate_lanes(x0, x1, weight, output_fraction_bits)
    scale = 2 << output_fraction_bits
    samples = (result[:, 0::2] + 1j * result[:, 1::2]) / scale
    info.update({
        'scope': 'integer Q32 position/Q16 interpolation software island; remaining estimator/PHY float',
        'input_scale2_integer_conversion_exact': True,
        'output_fraction_bits': int(output_fraction_bits),
        'output_bits': 20 + int(output_fraction_bits),
        'output_scale_adapter': f'signed{20 + output_fraction_bits} integer / {scale} to original floating receiver units',
        'input_max_absolute': int(max(abs(x0).max(), abs(x1).max())),
        'output_max_absolute': int(abs(result).max()),
        'bounded_history_or_RTL_estimator_service_verified': False,
        'g0_passed': False,
    })
    return samples, positions, info
