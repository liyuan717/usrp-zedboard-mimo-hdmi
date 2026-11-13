"""Exact report representatives for the independently proven reset-assert cone.

This is NOT a CDC waiver. Callers must first prove the five-root/four-CLR
topology, local release timing and all 26 exact exception effects in Vivado.
Vivado can select either of two already-proven FCLK roots as the representative
for that same RF-domain assertion. Raw report rows are always retained.
"""
RF = "clk_out1_receiver_v2_rf_sampling_clock_0"
FCLK = "clk_fpga_0"
BASE = "receiver_v2_i/"
DESCRIPTION = "Combinational logic detected before a synchronizer"
DIN = BASE + "rf_capture_guard/inst/din_release_sync_reg[0]/CLR"
DOUT = BASE + "rf_capture_guard/inst/dout_release_sync_reg[0]/CLR"
ALLOWED = {
    DIN: (FCLK, RF, {
        BASE + "reset_rf/U0/PR_OUT_DFF[0].FDRE_PER/C",
        BASE + "rf_lclk_monitor/inst/ready_reg/C",
    }),
    DOUT: (RF, FCLK, {BASE + "reset_rf_lclk/U0/PR_OUT_DFF[0].FDRE_PER/C"}),
}


def validate_critical_rows(rows):
    critical = [row for row in rows if row.severity == "Critical"]
    if len(critical) != 2 or {row.destination for row in critical} != set(ALLOWED):
        raise RuntimeError("Critical reset representative endpoint set/count differs")
    for row in critical:
        source_clock, destination_clock, sources = ALLOWED[row.destination]
        if (row.cdc_id != "CDC-10" or row.source not in sources
                or getattr(row, "description", None) != DESCRIPTION
                or row.source_clock != source_clock or row.destination_clock != destination_clock
                or row.depth != 2 or row.exception != "False Path"):
            raise RuntimeError(f"Unreviewed Critical reset representative: {row}")
    return [(row.source, row.destination) for row in critical]
