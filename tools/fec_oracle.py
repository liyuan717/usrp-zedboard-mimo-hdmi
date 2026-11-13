#!/usr/bin/env python3
"""Independent Lagrange interpolation check of G0 Vandermonde FEC goldens."""
import argparse
import json
from pathlib import Path


def multiply(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        a <<= 1
        if a & 256:
            a ^= 0x11d
        b >>= 1
    return result


def inverse(a):
    if not a:
        raise ValueError("inverse of zero")
    result, power = 1, 254
    while power:
        if power & 1:
            result = multiply(result, a)
        a = multiply(a, a)
        power >>= 1
    return result


def check(prefix):
    candidate = json.loads(prefix.with_suffix(".json").read_text())
    k, n, size = (candidate[x] for x in ("k", "n", "symbol_bytes"))
    if candidate["gf_polynomial"] != 0x11d:
        raise ValueError("unexpected field polynomial")
    denominators = []
    for column in range(k):
        product = 1
        for j in range(k):
            if j != column:
                product = multiply(product, (column + 1) ^ (j + 1))
        denominators.append(inverse(product))
    matrix = []
    for row in range(n):
        values = []
        for column in range(k):
            numerator = 1
            for j in range(k):
                if j != column:
                    numerator = multiply(numerator, (row + 1) ^ (j + 1))
            values.append(multiply(numerator, denominators[column]))
        matrix.append(values)
    table = [bytes(multiply(a,b) for b in range(256)) for a in range(256)]
    source = [bytes((s*29+b*17+b//11)&255 for b in range(size)) for s in range(k)]
    encoded = bytearray()
    for row in matrix:
        output = bytearray(size)
        for coefficient, symbol in zip(row, source):
            products = table[coefficient]
            for i,b in enumerate(symbol):
                output[i] ^= products[b]
        encoded.extend(output)
    result = {"scope": "G0 independent host Lagrange/bit-polynomial oracle, not PL acceptance",
              "matrix_equal": matrix == candidate["matrix_rows"],
              "encoded_bytes_equal": encoded == prefix.with_suffix(".encoded.bin").read_bytes(),
              "k":k,"n":n,"symbol_bytes":size,"bytes_compared":len(encoded),"g0_passed":False}
    prefix.with_suffix(".oracle.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result))
    if not result["matrix_equal"] or not result["encoded_bytes_equal"]:
        raise SystemExit(1)


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("prefix",type=Path)
    check(p.parse_args().prefix)
