#!/usr/bin/env python3
"""Configured G0 integer training CFO/H island; remaining receiver stays float.

Input is the final sampler's exact signed21/scale4 result. Only public QPSK
training is used. CORDIC/OLS/H arithmetic is integer; conversion to Hz and the
existing receiver's complex units occurs only at the output adapter. This is
not an estimator service-time or complete PHY acceptance claim.
"""
from decimal import Decimal, localcontext, ROUND_HALF_UP
import numpy as np
from g0_sounder import RATE, PREAMBLE_START, TRAIN_START, TRAIN_LENGTH
from g0_packet_probe import ChannelEstimateInvalid
from phase_angle_cost_probe import cordic_phase, PHASE_ERROR_BOUND_LSB

Q32 = 1 << 32
H_NORMALIZATION_Q40 = 1735428857
WEIGHTS = tuple(2 * b - 27 for b in range(28))
OLS_DIVISOR = 58464
HALF_TURN_GUARD = 2 * PHASE_ERROR_BOUND_LSB


class TrainingInvalid(ChannelEstimateInvalid):
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


def verify_normalization():
    with localcontext() as context:
        context.prec = 80
        exact = Decimal(1 << 40) / (Decimal(448) * Decimal(2).sqrt())
        rounded = int(exact.to_integral_value(rounding=ROUND_HALF_UP))
        if rounded != H_NORMALIZATION_Q40:
            raise AssertionError('H normalization constant mismatch')
        return {'constant': rounded, 'exact_decimal': str(exact), 'rounding': 'nearest ties toward positive infinity'}


def nearest_div(numerator, denominator):
    if denominator <= 0:
        raise ValueError('positive divisor required')
    return (2 * int(numerator) + int(denominator)) // (2 * int(denominator))


def exact_training_lanes(values):
    values = np.asarray(values)
    if values.shape != (448, 2):
        raise ValueError('training must contain448 two-complex vectors')
    lanes = np.empty((448, 4), dtype=float)
    lanes[:, 0::2] = 4 * values.real
    lanes[:, 1::2] = 4 * values.imag
    if not np.all(np.isfinite(lanes)) or np.any(lanes < -(1 << 20)) or np.any(lanes >= 1 << 20):
        raise ValueError('training input outside finite signed21')
    if not np.all(lanes == np.floor(lanes)):
        raise ValueError('training adapter requires exact integer/4; no implicit rounding')
    return lanes.astype(np.int64)


def training_signs(public, tx):
    index = np.arange(TRAIN_START[tx] + 32, TRAIN_START[tx] + TRAIN_LENGTH - 32)
    sequence = np.asarray(public)[index, tx]
    if not np.allclose(abs(sequence.real), 1 / np.sqrt(2), atol=1e-14, rtol=0) or not np.allclose(abs(sequence.imag), 1 / np.sqrt(2), atol=1e-14, rtol=0):
        raise ValueError('expected public unit QPSK training')
    return index, np.column_stack((np.where(sequence.real > 0, 1, -1), np.where(sequence.imag > 0, 1, -1)))


def unwrap_phase32(phases):
    """Match np.unwrap's signed +/-pi tie, then flag ambiguous half-turn steps."""
    signed = [int(p) if int(p) < 1 << 31 else int(p) - Q32 for p in phases]
    unwrapped = [signed[0]]
    deltas = []
    ambiguous = False
    for previous, current in zip(signed, signed[1:]):
        raw = current - previous
        delta = (raw + (1 << 31)) % Q32 - (1 << 31)
        if delta == -(1 << 31) and raw > 0:
            delta = 1 << 31
        ambiguous |= abs(abs(delta) - (1 << 31)) <= HALF_TURN_GUARD
        deltas.append(delta)
        unwrapped.append(unwrapped[-1] + delta)
    if any(not -(1 << 37) <= value < 1 << 37 for value in unwrapped):
        raise ValueError('unwrapped phase outside signed38')
    return unwrapped, deltas, bool(ambiguous)


def estimate_training(lanes, signs, tx):
    """One TX, exactly448 vectors. Returns integer model outputs/diagnostics."""
    lanes, signs = np.asarray(lanes), np.asarray(signs)
    if lanes.shape != (448, 4) or signs.shape != (448, 2):
        raise ValueError('training vector/sign dimensions')
    if not np.issubdtype(lanes.dtype, np.integer) or np.any(lanes < -(1 << 20)) or np.any(lanes >= 1 << 20):
        raise ValueError('signed21 training ports required')
    if tx not in (0, 1) or np.any((signs != 1) & (signs != -1)):
        raise ValueError('TX/sign input')
    lanes, signs = lanes.astype(np.int64), signs.astype(np.int64)
    energy = [sum(int(row[2 * rx]) ** 2 + int(row[2 * rx + 1]) ** 2 for row in lanes) for rx in range(2)]
    if any(value >= 1 << 50 for value in energy):
        raise ValueError('RX energy exceeded unsigned50')
    selected = 0 if energy[0] >= energy[1] else 1
    despread = np.empty_like(lanes)
    for rx in range(2):
        i, q = lanes[:, 2 * rx], lanes[:, 2 * rx + 1]
        despread[:, 2 * rx] = i * signs[:, 0] + q * signs[:, 1]
        despread[:, 2 * rx + 1] = q * signs[:, 0] - i * signs[:, 1]
    if np.any(despread < -(1 << 22)) or np.any(despread >= 1 << 22):
        raise ValueError('despread signed23 exceeded')
    block = despread.reshape(28, 16, 4).sum(axis=1)
    if np.any(block < -(1 << 26)) or np.any(block >= 1 << 26):
        raise ValueError('block sum signed27 exceeded')
    phases, unwrapped, weighted, bad, zero_counts, max_delta = [], [], [], [], [], []
    for rx in range(2):
        output = [cordic_phase(int(row[2 * rx]), int(row[2 * rx + 1])) for row in block]
        p = [value for value, error in output]
        zero = sum(error for value, error in output)
        u, d, ambiguous = unwrap_phase32(p)
        slope_sum = sum(w * value for w, value in zip(WEIGHTS, u))
        if not -(1 << 47) <= slope_sum < 1 << 47:
            raise ValueError('OLS weighted sum exceeded signed48')
        phases.append(p); unwrapped.append(u); weighted.append(slope_sum)
        bad.append(bool(zero or ambiguous)); zero_counts.append(zero); max_delta.append(max(abs(x) for x in d))
    info = {'tx': int(tx), 'energy_u50': energy, 'selected_rx': selected,
        'despread_max_absolute': int(abs(despread).max()), 'block_sum_max_absolute': int(abs(block).max()),
        'block_sums': block.tolist(), 'phases_u32': phases, 'unwrapped_phase_s38': unwrapped,
        'weighted_sums_s48': weighted, 'branch_invalid': bad, 'zero_blocks': zero_counts,
        'maximum_adjacent_phase_delta_LSB': max_delta, 'half_turn_ambiguity_guard_LSB': HALF_TURN_GUARD}
    if bad[selected]:
        raise TrainingInvalid('selected training branch has zero or ambiguous half-turn block phase', info)
    step = nearest_div(weighted[selected], OLS_DIVISOR)
    if not -(1 << 31) <= step < 1 << 31:
        raise ValueError('residual symbol phase step exceeded signed32')
    first_symbol = TRAIN_START[tx] + 32
    phase = (-step * (first_symbol - PREAMBLE_START)) & 0xffffffff
    coefficients = []
    rotated = np.empty_like(despread)
    for index, row in enumerate(despread):
        bin_index = ((phase + 0x80000) & 0xffffffff) >> 20
        angle = bin_index * (2 * np.pi / 4096)
        c, s = int(np.rint(65536 * np.cos(angle))), int(np.rint(65536 * np.sin(angle)))
        coefficients.append((c, s))
        for rx in range(2):
            i, q = int(row[2 * rx]), int(row[2 * rx + 1])
            rotated[index, 2 * rx] = nearest_div(i * c - q * s, 4096)
            rotated[index, 2 * rx + 1] = nearest_div(i * s + q * c, 4096)
        phase = (phase - step) & 0xffffffff
    if np.any(rotated < -(1 << 26)) or np.any(rotated >= 1 << 26):
        raise ValueError('rotated training exceeded signed27')
    totals = [sum(int(x) for x in rotated[:, lane]) for lane in range(4)]
    if any(not -(1 << 35) <= value < 1 << 35 for value in totals):
        raise ValueError('H accumulation exceeded signed36')
    # Python integers preserve the signed36 x unsigned31 product beyond int64.
    hraw = [nearest_div(total * H_NORMALIZATION_Q40, 1 << 40) for total in totals]
    if any(not -(1 << 25) <= value < 1 << 25 for value in hraw):
        raise ValueError('H output exceeded signed26')
    info.update({'symbol_step_s32': step, 'rotation_first_phase_u32': (-step * (first_symbol - PREAMBLE_START)) & 0xffffffff,
        'rotation_step_u32': (-step) & 0xffffffff, 'rotated_max_absolute': int(abs(rotated).max()),
        'H_accumulators_s36': totals, 'H_raw_s26': hraw, 'H_scale': 64,
        'normalization_constant_Q40': H_NORMALIZATION_Q40,
        'first_symbol': first_symbol, 'input_max_absolute': int(abs(lanes).max()), 'g0_passed': False})
    return info


class FixedTrainingEstimator:
    def __init__(self, retain_vectors=False):
        self.calls = []
        self.vectors = []
        self.retain_vectors = retain_vectors

    def __call__(self, r, times, public):
        h = np.zeros((2, 2), dtype=complex)
        frequencies, evm, reports, vectors = [], [], [], []
        for tx in range(2):
            index, signs = training_signs(public, tx)
            lanes = exact_training_lanes(r[index])
            try:
                report = estimate_training(lanes, signs, tx)
            except TrainingInvalid as error:
                self.calls.append({'scope': 'integer training CFO/H software island; configured receiver timing remains external',
                    'status': 'invalid', 'error': 'training_invalid', 'invalid_tx': tx,
                    'tx': reports + [error.diagnostics], 'g0_passed': False})
                if self.retain_vectors:
                    vectors.append({'tx': tx, 'lanes': lanes, 'signs': signs,
                                    'times': np.asarray(times[index]), 'report': error.diagnostics})
                    self.vectors.append(vectors)
                raise
            # Only this output adapter uses real time/Hz; the integer estimator
            # assumes uniform symbol locations, an explicit OLS model change.
            sample_period = float((times[index[-1]] - times[index[0]]) * RATE / (len(index) - 1))
            if not np.isfinite(sample_period) or not 3.999 <= sample_period <= 4.001:
                raise ValueError('training time adapter outside current sampling candidate')
            frequency = report['symbol_step_s32'] * RATE / (Q32 * sample_period)
            raw = np.array(report['H_raw_s26'])
            h[:, tx] = (raw[0::2] + 1j * raw[1::2]) / 64
            model = public[index, tx, None] * h[None, :, tx] * np.exp(2j * np.pi * frequency * times[index])[:, None]
            evm.append(float(np.sqrt(np.mean(abs(r[index] - model) ** 2) / np.mean(abs(model) ** 2))))
            frequencies.append(frequency)
            report.update({'sample_period_adapter': sample_period, 'residual_CFO_hz_adapter': frequency,
                           'uniform_symbol_OLS_model_change': True})
            reports.append(report)
            if self.retain_vectors:
                vectors.append({'tx': tx, 'lanes': lanes, 'signs': signs, 'times': np.asarray(times[index]), 'report': report})
        self.calls.append({'scope': 'integer training CFO/H software island; configured receiver timing remains external',
                           'status': 'valid', 'tx': reports, 'g0_passed': False})
        if self.retain_vectors:
            self.vectors.append(vectors)
        return h, np.array(frequencies), evm


def uniform_time_channel(r, times, public):
    """Float-only ablation isolates uniform-symbol OLS from all integer changes."""
    h = np.zeros((2, 2), dtype=complex);frequencies=[];evm=[]
    for tx in range(2):
        index, _ = training_signs(public, tx)
        g = r[index] * np.conj(public[index, tx])[:, None]
        strong = int(np.argmax(np.sum(abs(g) ** 2, axis=0)))
        block = g[:, strong].reshape(28, 16).mean(axis=1)
        phase = np.unwrap(np.angle(block)) / (2 * np.pi)
        cycles_per_symbol = float(np.dot(WEIGHTS, phase) / OLS_DIVISOR)
        sample_period = float((times[index[-1]] - times[index[0]]) * RATE / 447)
        frequency = cycles_per_symbol * RATE / sample_period
        h[:, tx] = np.mean(g * np.exp(-2j * np.pi * frequency * times[index])[:, None], axis=0)
        model = public[index, tx, None] * h[None, :, tx] * np.exp(2j * np.pi * frequency * times[index])[:, None]
        evm.append(float(np.sqrt(np.mean(abs(r[index] - model) ** 2) / np.mean(abs(model) ** 2))))
        frequencies.append(frequency)
    return h, np.array(frequencies), evm
