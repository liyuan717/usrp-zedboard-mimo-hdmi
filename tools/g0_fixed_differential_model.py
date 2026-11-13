#!/usr/bin/env python3
"""Exact integer public differential candidates; not complete acquisition.

Calibration/fine CFO/timing stay outside. Direct127 convolution is independent
of the RTL comb decomposition. Energies and ratio comparisons use Python ints.
"""
import math
import operator
import numpy as np
from g0_packet_probe import public_prefix
from phase_angle_cost_probe import cordic_phase, PHASE_ERROR_BOUND_LSB

RATE = 4_000_000
DENOMINATOR = 1979168
MASK32 = (1 << 32) - 1
MASK48 = (1 << 48) - 1
NORMALIZATION_BOUND_LSB = math.asin(math.sqrt(2) / (1 << 25)) * (1 << 32) / (2 * math.pi)


def public_weights():
    p = public_prefix()[256:384, 0]
    d = p[:-1].conj() * p[1:]
    dr, di = np.rint(d.real).astype(np.int64), np.rint(d.imag).astype(np.int64)
    if np.max(np.abs(d - (dr + 1j * di))) > 1e-14:
        raise ValueError('public differential sequence is not signs-only')
    if (int(dr.sum()), int(di.sum())) != (16, 17):
        raise ValueError('public sequence changed')
    return 127 * dr - 16, 127 * di - 17


def quantize_adc(y):
    y = np.asarray(y)
    if y.ndim != 2 or y.shape[1] != 2 or not np.all(np.isfinite(y)):
        raise ValueError('expected finite two-RX complex samples')
    lanes = np.empty((len(y), 4), dtype=np.float64)
    lanes[:, 0::2], lanes[:, 1::2] = y.real, y.imag
    rounded = np.rint(lanes)  # Explicit nearest ties even; never clip/wrap.
    if np.any(rounded < -32768) or np.any(rounded > 32767):
        raise ValueError('calibrated input outside signed16; no silent saturation')
    return rounded.astype(np.int16), float(np.max(np.abs(rounded - lanes), initial=0))


def adc_differentials(adc):
    a = np.asarray(adc)
    if a.ndim != 2 or a.shape[1] != 4 or a.dtype.kind not in 'iu':
        raise ValueError('expected integer ADC[n,4]')
    if np.any(a < -32768) or np.any(a > 32767):
        raise ValueError('ADC outside signed16')
    a = a.astype(np.int64)
    z = np.empty((max(0, len(a) - 4), 4), dtype=np.int64)
    for rx in range(2):
        ar, ai = a[:-4, 2 * rx], a[:-4, 2 * rx + 1]
        br, bi = a[4:, 2 * rx], a[4:, 2 * rx + 1]
        z[:, 2 * rx], z[:, 2 * rx + 1] = ar * br + ai * bi, ar * bi - ai * br
    if np.any(z < -(1 << 32)) or np.any(z >= 1 << 32):
        raise AssertionError('signed33 product overflow')
    return z


def direct_scores(adc):
    """Return C[k,sense,rx,re/im], E[k], N[k,sense]; k is first ADC index.

    z[k] uses ADC k and k+4; candidate k consumes through ADC k+508.
    No three trailing zero-template samples are required, unlike scipy valid.
    """
    z = adc_differentials(adc)
    count = max(0, len(adc) - 508)
    c = np.zeros((count, 2, 2, 2), dtype=np.int64)
    energy = np.zeros(count, dtype=object)
    if not count:
        return c, energy, np.zeros((0, 2), dtype=object)
    wr, wi = public_weights()
    # Each square fits signed64. The physical two-RX sum can equal2^63,
    # requiring unsigned64; the127-sample sum then requires unsigned70.
    e = np.sum(z * z, axis=1, dtype=np.uint64)
    for phase in range(4):
        v = z[phase::4]
        if len(v) < 127:
            continue
        n = len(v) - 126
        ix = slice(phase, phase + 4 * n, 4)
        for rx in range(2):
            re, im = v[:, 2 * rx], v[:, 2 * rx + 1]
            rr, ii = np.correlate(re, wr, 'valid'), np.correlate(im, wi, 'valid')
            ri, ir = np.correlate(re, wi, 'valid'), np.correlate(im, wr, 'valid')
            c[ix, 0, rx, 0], c[ix, 0, rx, 1] = rr + ii, ir - ri
            c[ix, 1, rx, 0], c[ix, 1, rx, 1] = rr - ii, ir + ri
        prefix = np.r_[0, np.cumsum(e[phase::4].astype(object))]
        energy[ix] = prefix[127:] - prefix[:-127]
    if any(int(x) < 0 or int(x) >= 1 << 70 for x in energy):
        raise AssertionError('physical signed16 energy exceeds uint70')
    if np.any(c < -(1 << 48)) or np.any(c >= 1 << 48):
        raise AssertionError('correlation exceeds signed49')
    numerator = np.sum(c.astype(object) ** 2, axis=(2, 3))
    return c, energy, numerator


def normalize_phase(re, im):
    """Common shift to25 amplitude bits, ties +infinity, then signed27 CORDIC."""
    re, im = int(re), int(im)
    if not -(1 << 48) <= re < 1 << 48 or not -(1 << 48) <= im < 1 << 48:
        raise ValueError('phase source outside signed49')
    shift = max(0, max(abs(re), abs(im)).bit_length() - 25)
    vals = [x if not shift else (x + (1 << (shift - 1))) >> shift for x in (re, im)]
    if not all(-(1 << 26) <= x < 1 << 26 for x in vals):
        raise AssertionError('normalized phase input overflows signed27')
    phase, error = cordic_phase(*vals)
    signed_phase = phase if phase < 1 << 31 else phase - (1 << 32)
    radius = math.hypot(re, im)
    coordinate_error = 0.0 if not shift else math.sqrt(2) * (1 << (shift - 1))
    bound = (math.asin(min(1.0, coordinate_error / radius)) * (1 << 32) / (2 * math.pi)
             if radius else None)
    exact_phase = math.atan2(im, re) / (2 * math.pi) * (1 << 32)
    phase_error = abs((phase - exact_phase + (1 << 31)) % (1 << 32) - (1 << 31)) if radius else None
    if not error and phase_error > bound + PHASE_ERROR_BOUND_LSB + 1e-4:
        raise AssertionError('angle exceeds normalization plus CORDIC bound')
    return {'shift': shift, 'coordinates_s27': vals, 'phase_u32': phase,
            'phase_s32': signed_phase, 'phase_error': int(error),
            'rounding': 'shift=max(0,bit_length(maxabs)-25); nearest ties toward positive infinity',
            'normalization_angle_bound_phase_lsb': bound,
            'universal_normalization_bound_phase_lsb': NORMALIZATION_BOUND_LSB,
            'CORDIC_angle_bound_phase_lsb': PHASE_ERROR_BOUND_LSB,
            'observed_angle_error_phase_lsb': phase_error,
            'raw_CFO_hz': signed_phase * RATE / (4 * (1 << 32))}


def coarse_nco_configuration(raw_phase_u32, sense, known_index, phase_origin_index=0):
    """Explicit phase/step descriptor; one rounding after full phase*offset.

    The sample-index subtraction is unsigned modulo48, with epoch separate.
    signed(raw_phase) is phase per4 samples. Do not round the step before
    multiplying the potentially large offset to establish initial phase.
    """
    raw = operator.index(raw_phase_u32); known = operator.index(known_index)
    origin = operator.index(phase_origin_index); direction = operator.index(sense)
    if not 0 <= raw <= MASK32 or not 0 <= known <= MASK48 or not 0 <= origin <= MASK48 or direction not in (0, 1):
        raise ValueError('coarse configuration requires phase32/sense1/unsigned48 indices')
    signed = raw if raw < 1 << 31 else raw - (1 << 32)
    directed = signed if direction else -signed
    offset = (known - origin) & MASK48
    step = (directed + 2) // 4
    phase = ((directed * offset + 2) // 4) & MASK32
    return {'step_u32': step & MASK32, 'phase_u32': phase,
            'phase_origin_index48': origin, 'known_index48': known,
            'offset_u48': offset, 'raw_phase_u32': raw, 'sense': direction,
            'directed_phase_s33': directed,
            'phase_equation': 'round_ties_up(directed_phase_s33*((known-origin) mod2^48)/4) mod2^32'}


def candidates(c, energy, numerator, *, origin=0, arm_start=0, hit_denominator=5):
    """Complete512-position windows; exact ratios and sense-major argmax ties.

    Default first strict.20 hit starts the window. Explicit denominator10
    is a diagnostic candidate gate only; training/CRC admission is separate.
    Fine-training rejection/acceptance is intentionally not implemented here.
    """
    if hit_denominator not in (5, 10):
        raise ValueError('supported coarse diagnostic gates are1/5 and1/10')
    search = max(0, arm_start - origin)
    result = []
    while search < len(c):
        first = next((k for k in range(search, len(c)) if int(energy[k]) > 0 and
                      any(hit_denominator * int(numerator[k, s]) > DENOMINATOR * int(energy[k]) for s in range(2))), None)
        if first is None or first + 512 > len(c):
            break
        best = None
        # np.argmax traverses all of sense0 before sense1. A later sense0
        # candidate therefore wins an exact tie with an earlier sense1 entry.
        for sense in range(2):
            for k in range(first, first + 512):
                e, p = int(energy[k]), int(numerator[k, sense])
                if not e:
                    continue
                if best is None or p * best[3] > best[2] * e:
                    best = (sense, k, p, e)
        sense, k, p, e = best
        pair = [sum(int(v) ** 2 for v in c[k, sense, rx]) for rx in range(2)]
        rx = 1 if pair[1] > pair[0] else 0
        phase = normalize_phase(*c[k, sense, rx])
        corrected_phase = -phase['phase_s32'] if sense else phase['phase_s32']
        phase['sense_corrected_CFO_hz'] = corrected_phase * RATE / (4 * (1 << 32))
        phase['coarse_NCO_step_s32'] = (-corrected_phase + 2) // 4
        phase['coarse_NCO_step_u32'] = phase['coarse_NCO_step_s32'] & MASK32
        # Feed the already ties-up-quantized hardware step through the legacy
        # Hz interface. Passing raw phase/4 would re-round half steps ties-even.
        phase['applied_CFO_hz'] = -phase['coarse_NCO_step_s32'] * RATE / (1 << 32)
        phase['applied_raw_CFO_hz'] = -phase['applied_CFO_hz'] if sense else phase['applied_CFO_hz']
        result.append({'first_hit_index': origin + first, 'window_end_exclusive': origin + first + 512,
                       'selected_start_index': origin + k, 'sense': sense, 'selected_rx': rx,
                       'C_s49': c[k].reshape(-1).tolist(), 'E_u70': e,
                       'numerator': p, 'denominator': DENOMINATOR * e,
                       'score_float_diagnostic': p / (DENOMINATOR * e), 'phase': phase,
                       'last_required_ADC_index': origin + first + 511 + 508,
                       'software_coarse_known_index': origin + first + 512 + 512 + 128,
                       'fine_training_validated': False})
        search = first + 512
    return result


class FixedDifferentialSelector:
    """Opt-in causal receiver selector, computing bounded direct-score chunks.

    This is a software numerical adapter, not an implementation of acquisition
    scheduling/queues. Only completed512-position windows can be returned.
    Chunks overlap511 starts, so a first hit near a chunk end is not skipped.
    Default1/5 preserves historical models. R9 uses explicit hit_denominator=10;
    its gate arithmetic/full55RTL/recovery proofs are joined separately.
    """
    def __init__(self, calibrated, public_preamble, phase_origin_index=0, *, hit_denominator=5):
        if not np.array_equal(np.asarray(public_preamble), public_prefix()[256:384, 0]):
            raise ValueError('integer selector requires its declared public preamble')
        self.y = np.asarray(calibrated)
        self.score_count = max(0, len(self.y) - 511)
        self.calls = []
        self.scan_chunks = []
        self.cache = None
        self.phase_origin_index = operator.index(phase_origin_index)
        if hit_denominator not in (5, 10):
            raise ValueError('supported coarse diagnostic gates are1/5 and1/10')
        self.hit_denominator = hit_denominator
        if not 0 <= self.phase_origin_index <= MASK48:
            raise ValueError('phase origin outside unsigned48')

    def nco_configuration(self, selected, known_index):
        return coarse_nco_configuration(selected['phase']['phase_u32'], selected['sense'],
                                        known_index, self.phase_origin_index)

    def next_candidate(self, search):
        search = int(search)
        while search < self.score_count:
            if self.cache is None or not self.cache[0] <= search < self.cache[1] - 511:
                origin = search
                end = min(self.score_count, origin + 65536)
                adc, error = quantize_adc(self.y[origin:end + 508])
                c, e, n = direct_scores(adc)
                assert len(c) == end - origin
                self.cache = (origin, end, c, e, n)
                self.scan_chunks.append({'first_start_index': origin, 'last_start_exclusive': end,
                                         'ADC_rounding_error_max': error})
            origin, end, c, e, n = self.cache
            found = candidates(c, e, n, origin=origin, arm_start=search,
                               hit_denominator=self.hit_denominator)
            if found:
                chosen = found[0]
                self.calls.append(chosen)
                return chosen
            if end == self.score_count:
                return None
            search = end - 511
            self.cache = None
        return None


def self_test():
    rails = np.full((1024, 4), -32768, dtype=np.int16)
    z = adc_differentials(rails)
    assert np.all(z[:, 0::2] == 1 << 31) and np.all(z[:, 1::2] == 0)
    c, e, n = direct_scores(rails)
    assert not np.any(c) and all(int(v) == 127 * (1 << 63) for v in e)
    assert not candidates(c, e, n)
    cc = np.zeros((1024, 2, 2, 2), dtype=np.int64); cc[:, :, 0, 0] = 1
    ee = np.full(1024, 5, dtype=object); nn = np.full((1024, 2), DENOMINATOR, dtype=object)
    assert not candidates(cc, ee, nn)  # Equality exactly.20 must not trigger.
    nn[3, 1] += 1; nn[20, 0] += 1
    got = candidates(cc, ee, nn)[0]
    assert got['first_hit_index'] == 3 and got['selected_start_index'] == 20 and got['sense'] == 0
    ee[:] = 10; nn[:] = DENOMINATOR
    assert not candidates(cc, ee, nn, hit_denominator=10)
    nn[3, 1] += 1
    assert not candidates(cc, ee, nn)  # Default remains strict1/5.
    assert candidates(cc, ee, nn, hit_denominator=10)[0]['first_hit_index'] == 3
    ee[:] = 0; nn[:] = 0
    ee[3] = 1; nn[3, 1] = 2_000_000
    ee[20] = 2; nn[20, 0] = 4_000_000
    ee[25] = 3; nn[25, 0] = 5_000_000
    got = candidates(cc, ee, nn)[0]
    assert got['selected_start_index'] == 20 and got['selected_rx'] == 0
    rng = np.random.default_rng(0xD1FF)
    a = rng.integers(-32768, 32768, (520, 4), dtype=np.int16)
    c, e, _ = direct_scores(a); z = adc_differentials(a); wr, wi = public_weights()
    for k in range(len(c)):
        assert int(e[k]) == sum(int(v) ** 2 for row in z[k:k+505:4] for v in row)
        for sense in range(2):
            for rx in range(2):
                sr = si = 0
                for j in range(127):
                    zr, zi = map(int, z[k+4*j, 2*rx:2*rx+2])
                    br, bi = int(wr[j]), int(wi[j]) * (1 if sense else -1)
                    sr += zr*br-zi*bi; si += zr*bi+zi*br
                assert tuple(map(int, c[k, sense, rx])) == (sr, si)
    for re, im in [(0, 0), (1, 0), (-1, 0), (0, -1), (1 << 26, 1),
                   ((1 << 48) - 1, -(1 << 48)), ((1 << 27) - 1, 1 << 27)]:
        normalize_phase(re, im)
    for re, im in rng.integers(-(1 << 48), 1 << 48, (1000, 2), dtype=np.int64):
        normalize_phase(re, im)
    return {'signed16_rails_exact_z33_E70': True, 'scalar_direct_convolution_fullrange': True,
            'zero_energy_and_zero_C_reject': True, 'strict_threshold_equality_reject': True,
            'exact_ratio_and_sense_then_time_ties': True, 'strong_RX0_tie': True,
            'phase_fullrange_samples': 1007, 'normalization_bound_phase_lsb': NORMALIZATION_BOUND_LSB,
            'normalization_plus_CORDIC_bound_phase_lsb': NORMALIZATION_BOUND_LSB + PHASE_ERROR_BOUND_LSB}
