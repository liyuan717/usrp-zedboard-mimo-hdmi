#!/usr/bin/env python3
"""Name the three SPI output endpoints Vivado emits as /I in a cell SDF."""

import argparse
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("named", type=Path)
    args = parser.parse_args()

    # The OBUF input is outside the exported rf_spi simulation hierarchy.
    ports = {
        r"OTHER_RATIO_GENERATE\.Serial_Dout_reg/Q": "io0_o",
        r"SCK_O_NQ_4_NO_STARTUP_USED\.SCK_O_NE_4_FDRE_INST/Q": "sck_o",
        r"SS_O_reg\[0\]/Q": r"ss_o\[0\]",
    }
    unresolved = re.compile(rb"^(\s*\(INTERCONNECT (\S+) )/I(?= )", re.MULTILINE)
    seen = set()

    def name_endpoint(match):
        driver = match[2].decode("ascii")
        matches = [suffix for suffix in ports if driver.endswith(suffix)]
        if len(matches) != 1 or matches[0] in seen:
            raise ValueError(f"Unexpected /I endpoint driven by {driver}")
        suffix = matches[0]
        seen.add(suffix)
        return match[1] + ports[suffix].encode("ascii")

    named = unresolved.sub(name_endpoint, args.original.read_bytes())
    if seen != ports.keys():
        raise ValueError("Expected exactly one unnamed endpoint per SPI output")
    with args.named.open("xb") as out:
        out.write(named)


if __name__ == "__main__":
    main()
