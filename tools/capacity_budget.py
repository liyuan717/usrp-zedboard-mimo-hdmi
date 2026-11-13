#!/usr/bin/env python3
"""Exact accounting for a hypothetical frame; deliberately cannot freeze G0."""
import argparse
import json
import math
from pathlib import Path
import random


def residual_probability(k, n, loss):
    return sum(math.comb(n, i) * loss**i * (1 - loss)**(n - i) for i in range(n - k + 1, n + 1))


def simulate(k, n, depth, model, blocks=12000):
    rng = random.Random(3109361)
    lost = count = failed = residual = 0
    bad = False
    # Models operate on sequential link packets, before the round-robin map.
    # These are erasure-count bounds for an ideal MDS code, not actual FEC tests.
    p_leave = 1 / model if model else 0
    p_enter = .15 * p_leave / .85 if model else 0
    for _ in range(blocks // depth):
        missing = [0] * depth
        for _symbol in range(n):
            for lane in range(depth):
                if model:
                    erase = bad
                    bad = rng.random() >= p_leave if bad else rng.random() < p_enter
                else:
                    erase = rng.random() < .15
                missing[lane] += erase
                lost += erase
                count += 1
        for m in missing:
            failed += m > n - k
            residual += max(0, m - (n - k))
    return {"blocks": blocks, "observed_packet_loss": lost / count,
            "blocks_beyond_ideal_mds_capacity": failed, "block_failure_fraction": failed / blocks,
            "model": "IID15pct" if not model else f"Gilbert15pct_mean_burst_{model}_packets",
            "actual_decoder_test": False}


def budget(profile, codec):
    p = profile
    feedback_enabled = p.get("feedback_enabled", True)
    if not feedback_enabled and any(p[x] for x in ("guard_rx_to_tx_us", "feedback_us", "guard_tx_to_rx_us")):
        raise ValueError("Stage1 no-feedback candidate cannot hide feedback/TDD windows in active capacity")
    if sum(p[x] for x in ("forward_us", "guard_rx_to_tx_us", "feedback_us", "guard_tx_to_rx_us")) != p["superframe_us"]:
        raise ValueError("TDD windows do not sum to superframe")
    if p["sample_rate_hz"] % p["symbol_rate_hz"]:
        raise ValueError("integer oversampling is required by this budget")
    packet_bytes = p["symbol_bytes"] + p["link_header_bytes"] + p["link_crc_bytes"]
    data = math.ceil(p["packets_per_stream"] * packet_bytes * 8 / p["qpsk_bits_per_symbol"])
    pilots = math.ceil(data / p["pilot_spacing_data_symbols"]) * p["orthogonal_pilot_symbols_per_group"]
    head = p["phy_header_bytes"] * 8 * p["phy_header_bpsk_repetitions"]
    prefix = p.get("prefix_silence_symbols", 0)
    total = prefix + data + pilots + head + p["preamble_symbols"] + p["training_symbols_per_tx"] * p["spatial_streams"] + p["tail_symbols"]
    available = p["forward_us"] * p["symbol_rate_hz"] / 1e6
    def feedback_size(f):
        return ((f["payload_bytes"] + f["link_header_bytes"] + f["link_crc_bytes"]) * 8 /
                        p["qpsk_bits_per_symbol"] * f["packet_repetitions"] + f["preamble_symbols"] +
                        f["training_symbols"] + f["phy_header_bytes"] * 8 * f["phy_header_bpsk_repetitions"] + f["tail_symbols"])
    feedback_symbols = feedback_size(p["feedback_candidate"]) if feedback_enabled else 0
    packet_rate = p["spatial_streams"] * p["packets_per_stream"] * 1e6 / p["superframe_us"]
    coded_link_bps = packet_rate * packet_bytes * 8
    result = {"status": "calculated_only_not_measured", "g0_passed": False,
              "forward_symbols": {"data": data, "pilots": pilots, "phy_header": head, "prefix_silence": prefix,
                                  "total": total, "available": available, "fits": total <= available},
              "feedback_enabled": feedback_enabled, "stage": p.get("stage", 2),
              "feedback_symbols": feedback_symbols,
              "feedback_us": feedback_symbols / p["symbol_rate_hz"] * 1e6,
              "feedback_fits": feedback_symbols / p["symbol_rate_hz"] * 1e6 <= p["feedback_us"] if feedback_enabled else None,
              "coded_link_capacity_bps": coded_link_bps, "fec_options": []}
    if "stage2_reserve_estimate" in p:
        reserve=p["stage2_reserve_estimate"]
        result["stage2_reserve_estimate"]={**reserve,
            "feedback_symbols":feedback_size(reserve["feedback_candidate"]),
            "coded_capacity_bps_if_estimated_windows_hold":coded_link_bps*p["superframe_us"]/reserve["total_us"],
            "deducted_from_stage1_capacity":False,"stage2_measurement_passed":False}
    for k, n in p["fec_candidates"]:
        option = {"k": k, "n": n, "iid15pct_ideal_mds_block_failure_probability": residual_probability(k, n, .15),
                  "codec_loads": [], "loss_models": []}
        for c in codec["candidates"]:
            # Actual fragments already include every video descriptor and tail pad.
            # FEC protection is counted once, here, after the measured source count.
            link_load = c["source_symbols"] * packet_bytes * 8 / 10 * n / k
            fill = c["source_symbol_fill_ratio"]
            usable = packet_rate * k / n * (p["symbol_bytes"] - p["video_header_bytes"]) * 8 * fill
            option["codec_loads"].append({"name": c["name"], "coded_link_required_bps": link_load,
                "calculated_load_fraction": link_load / coded_link_bps,
                "calculated_margin_at_least_30pct": link_load <= .7 * coded_link_bps,
                "video_capacity_at_measured_fragment_fill_bps": usable,
                "measured_reliable_margin_passed": False})
        for depth in p["interleave_blocks_candidates"]:
            for model in (0, 4, 8, 16):
                option["loss_models"].append({"interleave_depth": depth, **simulate(k, n, depth, model)})
        result["fec_options"].append(option)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--codec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = budget(json.loads(args.profile.read_text()), json.loads(args.codec.read_text()))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "fec_options"}))
