# Run: python test_deflate.py. What we write, someone else's code reads; what we read, theirs wrote.
import gzip
import os
import random
import struct
import subprocess
import sys
import zlib
from fractions import Fraction

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

# A three-byte repeat from more than 4096 bytes back costs more as a match than as three
# literals, so, as in zlib, it must not be taken; a nearer one still must be.
filler = bytes(b for b in random.Random(0).randbytes(6000) if b not in b"xyz")[:5000]
far = deflate.lz77(b"xyz" + filler + b"xyz")
assert not any(isinstance(t, tuple) and t[0] == 3 and t[1] > 4096 for t in far), "took a too-far 3-byte match"
near = deflate.lz77(b"xyz" + filler[:100] + b"xyz")
assert (3, 103) in near, "dropped a near 3-byte match"

# Every code must be complete, its lengths filling the tree exactly (a Kraft sum of 1):
# zlib rejects an incomplete one. Fibonacci counts build the deepest possible tree, so they
# always hit the depth limit, which is where the old trimming left codes incomplete.
fib = [1, 1]
while len(fib) < 25:
    fib.append(fib[-1] + fib[-2])
for limit, n in ((7, 19), (15, 25)):
    lengths = deflate.lengths_from(dict(enumerate(fib[:n])), limit)
    assert max(lengths.values()) <= limit, f"a code is longer than {limit} bits"
    assert sum(Fraction(1, 2 ** l) for l in lengths.values()) == 1, f"incomplete code at limit {limit}"

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

# The reading half, the other way round: our inflate on streams zlib wrote, at every level
# and with each strategy, so every block type and table shape zlib makes is read.
for label, data in CASES.items():
    for level in range(10):
        for strategy in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FIXED, zlib.Z_HUFFMAN_ONLY, zlib.Z_RLE):
            z = zlib.compressobj(level, zlib.DEFLATED, -15, 9, strategy)
            raw = z.compress(data) + z.flush()
            out, end = deflate.inflate(raw + b"trailer")
            assert out == data and end == len(raw), f"{label}: inflate misread level {level}, strategy {strategy}"
    assert deflate.gunzip(gzip.compress(data)) == data, f"{label}: gunzip misread gzip.compress"

# A header with every optional field: extra data, a name, a comment and a header CRC.
z = zlib.compressobj(6, zlib.DEFLATED, -15)
body = z.compress(b"fields") + z.flush()
header = b"\x1f\x8b\x08\x1e" + bytes(6) + b"\x03\x00abc" + b"name\0" + b"comment\0" + b"\0\0"
assert deflate.gunzip(header + body + struct.pack("<II", zlib.crc32(b"fields"), 6)) == b"fields"


# Broken input must be refused with a reason, not misread or crashed on.
def refused(stream, reason, read=deflate.inflate):
    try:
        read(stream)
    except ValueError as e:
        assert reason in str(e), f"wanted {reason!r}, got {e!r}"
        return
    raise AssertionError(f"accepted a stream that should fail with {reason!r}")


refused(b"\x07", "reserved")
refused(b"\x01\x05\x00\x00\x00hello", "complement")
refused(b"\x01\x05\x00\xfa\xffhel", "ends early")
# A fixed block whose first act is to copy 3 bytes from 1 back, before there are any.
w = deflate.BitWriter()
w.write(1, 1)
w.write(1, 2)
w.write_code(1, 7)
w.write_code(0, 5)
w.align()
refused(bytes(w.out), "reaches back")
good = gzip.compress(b"some data")
# The CRC and the length are checked apart: one bit off in either is enough.
refused(good[:-8] + bytes([good[-8] ^ 1]) + good[-7:], "trailer", deflate.gunzip)
refused(good[:-4] + bytes([good[-4] ^ 1]) + good[-3:], "trailer", deflate.gunzip)
# Three one-bit codes cannot exist: two use up every one-bit pattern there is.
refused({0: 1, 1: 1, 2: 1}, "more symbols", deflate.decoder)
refused(b"PK\x03\x04", "not a gzip", deflate.gunzip)
# Cut anywhere, header included, a gzip file is refused as ending early, never crashed on:
# the header fields were once read without checking they were all there.
full = header + body + struct.pack("<II", zlib.crc32(b"fields"), 6)
for cut in range(3, len(full)):
    refused(full[:cut], "ends early", deflate.gunzip)

# Members joined end to end are one file, as cat a.gz b.gz makes; Python's gzip agrees.
joined = gzip.compress(b"first, ") + gzip.compress(b"") + gzip.compress(CASES["english"])
assert deflate.gunzip(joined) == gzip.decompress(joined) == b"first, " + CASES["english"]
# Each member's trailer is checked, not only the last one's.
broken = bytearray(joined)
broken[len(gzip.compress(b"first, ")) - 8] ^= 1
refused(bytes(broken), "trailer", deflate.gunzip)
refused(joined + b"junk", "offset", deflate.gunzip)

# Cut short anywhere in its coded data, a stream must be refused, never decoded from the
# zeros a reader sees past the end. Each of the last 40 bytes is tried as the cut.
for level in (1, 9):
    whole = zlib.compressobj(level, zlib.DEFLATED, -15)
    whole = whole.compress(CASES["english"] + CASES["own source"]) + whole.flush()
    for cut in range(1, 41):
        refused(whole[:-cut], "ends early")

# Flipping random bits of a real stream may give garbage, but only ever a ValueError.
rng = random.Random(1)
raw = zlib.compress(CASES["own source"], 9)[2:-4]
for _ in range(300):
    bad = bytearray(raw)
    for _ in range(rng.randrange(1, 4)):
        bad[rng.randrange(len(bad))] ^= 1 << rng.randrange(8)
    try:
        deflate.inflate(bytes(bad))
    except ValueError:
        pass
print("inflate read every stream zlib wrote, and refused the broken ones")

# -t from the command line, as gzip -t: OK for a good file, the reason for a bad or missing
# one, exit status 1 if any failed, and nothing written anywhere.
good_data = CASES["english"]
open("t-good.gz", "wb").write(gzip.compress(good_data))
open("t-cut.gz", "wb").write(gzip.compress(good_data)[:-20])
try:
    script = deflate.__file__
    both = subprocess.run([sys.executable, script, "-t", "t-good.gz", "t-cut.gz", "t-missing.gz"], capture_output=True, text=True)
    assert both.returncode == 1, both
    assert f"t-good.gz: OK, {len(good_data)} bytes" in both.stdout, both.stdout
    # Reported, not crashed on: a traceback would also exit 1 and mention the name.
    assert "t-cut.gz: the stream ends early" in both.stderr and "Traceback" not in both.stderr, both.stderr
    assert any(line.startswith("t-missing.gz: ") for line in both.stderr.splitlines()), both.stderr
    alone = subprocess.run([sys.executable, script, "-t", "t-good.gz"], capture_output=True, text=True)
    assert alone.returncode == 0 and not alone.stderr, alone
    assert not os.path.exists("t-good") and not os.path.exists("t-cut"), "-t wrote a file"
finally:
    for name in ("t-good.gz", "t-cut.gz"):
        os.remove(name)
print("-t reports each file and fails if any is broken")

# -c, as gzip's: compressed bytes on standard output and nothing else, then read back the same way.
open("t-pipe.txt", "wb").write(good_data)
try:
    packed = subprocess.run([sys.executable, deflate.__file__, "t-pipe.txt", "-c"], capture_output=True)
    assert packed.returncode == 0 and gzip.decompress(packed.stdout) == good_data, packed.stderr
    open("t-pipe.txt.gz", "wb").write(packed.stdout)
    back = subprocess.run([sys.executable, deflate.__file__, "-d", "-c", "t-pipe.txt.gz"], capture_output=True)
    assert back.returncode == 0 and back.stdout == good_data, back.stderr
    assert not os.path.exists("t-pipe.txt.gz.gz") and not os.path.exists("t-pipe"), "-c wrote a file"
finally:
    for name in ("t-pipe.txt", "t-pipe.txt.gz"):
        if os.path.exists(name):
            os.remove(name)
print("-c writes to standard output, both ways")

# Levels, as gzip's: each is read back by zlib, 1 is bigger than 6 and 9 no bigger (a deeper search
# is not always smaller: here 8 can lose to 7), 6 is the default, and the header says which end.
text = CASES["english"] + CASES["own source"]
sizes = []
for level in range(1, 10):
    packed = deflate.gzip_bytes(text, level=level)
    assert gzip.decompress(packed) == text, f"level {level} read back wrong"
    sizes.append(len(packed))
assert sizes[0] > sizes[5] >= sizes[8], f"sizes by level: {sizes}"
assert deflate.deflate(text) == deflate.deflate(text, level=6)
assert [deflate.gzip_bytes(b"x", level=n)[8] for n in (1, 6, 9)] == [4, 0, 2]
try:
    deflate.deflate(b"x", level=10)
    raise AssertionError("level 10 was accepted")
except ValueError as e:
    assert "1 to 9" in str(e), e
open("t-level.txt", "wb").write(text)
try:
    run = subprocess.run([sys.executable, deflate.__file__, "t-level.txt", "-9", "-o", "t-level.gz"], capture_output=True, text=True)
    assert run.returncode == 0 and "level 9" in run.stdout, run
    written = open("t-level.gz", "rb").read()
    assert written[8] == 2 and gzip.decompress(written) == text and len(written) < sizes[0], "the -9 flag was not used"
finally:
    for name in ("t-level.txt", "t-level.gz"):
        if os.path.exists(name):
            os.remove(name)
print("levels 1 to 9 all read back; sizes by level:", sizes)

print("FAILURES" if failures else "ok")
sys.exit(1 if failures else 0)
