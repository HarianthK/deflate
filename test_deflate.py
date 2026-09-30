# Run: python test_deflate.py. Every file is decompressed by someone else's code, never ours.
import gzip
import os
import random
import subprocess
import sys
import zlib

import deflate

CASES = {
    "empty": b"",
    "one byte": b"a",
    "no repeats": bytes(range(256)),
    "one long run": b"x" * 70000,
    "repeating pair": b"ab" * 5000,
    "english": (b"the quick brown fox jumps over the lazy dog. " * 200),
    "own source": open(__file__, "rb").read() + open(deflate.__file__, "rb").read(),
    "far match": b"needle" + os.urandom(30000) + b"needle",
    "max length match": b"z" * 258 + b"q" + b"z" * 258,
    "random": bytes(random.randrange(256) for _ in range(20000)),
}


def check(label, data, mode):
    packed = deflate.gzip_bytes(data, name="t.bin", mode=mode)
    assert gzip.decompress(packed) == data, f"{label} ({mode}): gzip module read it back wrong"
    # zlib with a raw window decodes the DEFLATE stream alone, a second opinion on the bits.
    body = packed[len(b"\x1f\x8b\x08\x08\0\0\0\0\0\x03t.bin\0"):-8]
    assert zlib.decompressobj(-15).decompress(body) == data, f"{label} ({mode}): raw stream decoded wrong"
    return len(packed)


failures = 0
for label, data in CASES.items():
    sizes = {}
    for mode in ("stored", "fixed", "dynamic", "auto"):
        try:
            sizes[mode] = check(label, data, mode)
        except AssertionError as e:
            print("FAIL", e)
            failures += 1
    theirs = len(gzip.compress(data, 9))
    assert sizes["auto"] == min(sizes.values()), f"{label}: auto picked {sizes['auto']}, best was {min(sizes.values())}"
    # Compared without a stored file name, as gzip.compress writes none; with it, six bytes
    # of every row were the name "t.bin", not compression.
    ours = len(deflate.gzip_bytes(data))
    print(f"{label:18} {len(data):>7} bytes -> ours {ours:>7}  gzip -9 {theirs:>7}  "
          f"({100 * ours / theirs:.0f}% of theirs)")

# The real gzip program, if it is installed, is the strictest reader there is.
sample = CASES["english"] + CASES["own source"]
open("sample.bin", "wb").write(sample)
open("sample.bin.gz", "wb").write(deflate.gzip_bytes(sample, name="sample.bin"))
try:
    proc = subprocess.run(["gzip", "-dc", "sample.bin.gz"], capture_output=True)
    assert proc.returncode == 0 and proc.stdout == sample, f"gzip said: {proc.stderr[:200]}"
    print("gzip -dc read our file back byte for byte")
except FileNotFoundError:
    print("gzip not installed, skipped that check")
finally:
    os.remove("sample.bin")
    os.remove("sample.bin.gz")

print("FAILURES" if failures else "ok")
sys.exit(1 if failures else 0)
