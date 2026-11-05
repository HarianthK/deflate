# Run: python test_png.py. Every PngSuite image read as PIL reads it, every broken one refused
# with its reason, and every rewrite read back by PIL as the same pixels.
import glob
import io
import os
import struct
import subprocess
import sys
import warnings
import zlib

from PIL import Image

import png

warnings.simplefilter("ignore", DeprecationWarning)  # Pillow's getdata, which still works
SUITE = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pngsuite", "*.png")))
GOOD = [f for f in SUITE if not os.path.basename(f).startswith("x")]
assert len(GOOD) == 161 and len(SUITE) == 175, (len(GOOD), len(SUITE))


def as_pil_sees(image):
    # Our samples in PIL's terms: gray raw (PIL scales 1, 2 and 4 bits to 8), the rest as 8-bit RGBA.
    if image.color == 0:
        maxval = (1 << image.depth) - 1
        return [s if image.depth == 16 else s * 255 // maxval for row in image.rows for s in row]
    depth, maxval, rows = png.pixels(image)
    out = []
    for row in rows:
        for x in range(0, len(row), depth):
            p = [s >> 8 if maxval == 65535 else s for s in row[x:x + depth]]
            p = p[:1] * 3 + p[1:] if depth <= 2 else p
            out.append(tuple(p + [255] * (4 - len(p))))
    return out


def pil_pixels(data):
    im = Image.open(io.BytesIO(data))
    return list(im.getdata()) if im.mode in ("L", "I;16", "1") or im.mode.startswith("I") else list(im.convert("RGBA").getdata())


# Reading: each image as PIL decodes it, and each interlaced one the same as its plain twin.
read = {}
for f in GOOD:
    data = open(f, "rb").read()
    read[f] = png.read(data)
    assert as_pil_sees(read[f]) == pil_pixels(data), f"{os.path.basename(f)}: pixels differ from PIL's"
twins = [f for f in GOOD if os.path.basename(f).startswith("basi")]
for f in twins:
    twin = f.replace("basi", "basn")
    assert read[twin].rows == read[f].rows and read[f].interlaced and not read[twin].interlaced, f
assert len(twins) == 15
# Gray with tRNS has no alpha in PIL's terms; the key PIL reports is where ours is transparent.
for f in GOOD:
    image, key = read[f], Image.open(f).info.get("transparency")
    if image.color == 0 and key is not None:
        _, _, rows = png.pixels(image)
        assert all((a == 0) == (g == key) for row in rows for g, a in zip(row[::2], row[1::2])), f
        assert any(a == 0 for row in rows for a in row[1::2]), f
print(f"{len(GOOD)} PngSuite images read as PIL reads them, all 15 interlaced ones the same as their plain twins")


# Refusing: the suite's broken files, and breakages it lacks, each with a reason.
def refused(data, words):
    try:
        png.read(data)
    except ValueError as e:
        assert words in str(e), (words, str(e))
        return
    raise AssertionError(f"a broken PNG was read ({words})")


BROKEN = {"xs1n0g01": "signature", "xs2n0g01": "signature", "xs4n0g01": "signature", "xs7n0g01": "signature",
          "xcrn0g04": "signature", "xlfn0g04": "signature", "xhdn0g08": "IHDR chunk at offset 8 fails its CRC",
          "xc1n0g08": "color type 1", "xc9n2c08": "color type 9", "xd0n2c08": "bit depth 0",
          "xd3n2c08": "bit depth 3", "xd9n2c08": "bit depth 99", "xdtn0g01": "no IDAT", "xcsn0g01": "IDAT chunk at offset 49 fails its CRC"}
for f in SUITE:
    name = os.path.basename(f)[:-4]
    if name.startswith("x"):
        refused(open(f, "rb").read(), BROKEN.pop(name))
assert not BROKEN, BROKEN


def make(rows, filter_bytes, color=2, depth=8, extra=b"", width=4, height=None, tail=b""):
    raw = b"".join(bytes([k]) + bytes(r) for k, r in zip(filter_bytes, rows)) + tail
    ihdr = struct.pack(">IIBBBBB", width, height or len(rows), depth, color, 0, 0, 0)
    return png.SIGNATURE + png.chunk(b"IHDR", ihdr) + extra + png.chunk(b"IDAT", zlib.compress(raw)) + png.chunk(b"IEND", b"")


rows = [[10, 20, 30] * 4, [40, 50, 60] * 4]
good = make(rows, [0, 0])
assert png.read(good).rows == rows
# Up (2) adds the byte above, so a second row of up-filtered 1s is the first row plus one.
assert png.read(make([rows[0], [1] * 12], [0, 2])).rows[1] == [v + 1 for v in rows[0]]
refused(make(rows, [0, 5]), "row 1: filter type 5")
refused(make(rows, [0, 0], height=3), "ends at row 2 of 3")
refused(make(rows, [0, 0], tail=b"zz"), "2 bytes of image data after")
idat = png.chunk(b"IDAT", zlib.compress(b"\0" + bytes(12)))
refused(png.SIGNATURE + png.chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 1, 8, 2, 0, 0, 0)) + idat
        + png.chunk(b"tEXt", b"a\0b") + idat + png.chunk(b"IEND", b""), "not consecutive")
refused(make([[0, 1, 2, 3]], [0], color=3), "no PLTE")
refused(make([[0, 1, 2, 3]], [0], color=3, extra=png.chunk(b"PLTE", bytes(9))), "past the end of the palette")
refused(make(rows, [0, 0], extra=png.chunk(b"ZAPd", b"")), "critical chunk ZAPd")
refused(good[:-12], "before its IEND")
refused(good[:50], "ends inside the")
print("all 14 broken suite files and 9 other breakages refused, each with its reason")

# Rewriting: PIL reads every rewrite as the same pixels, with the ancillary chunks kept and no interlacing.
ours = theirs = 0
for f in GOOD:
    data = open(f, "rb").read()
    out = png.write(read[f])
    assert pil_pixels(out) == pil_pixels(data), f"{os.path.basename(f)}: the rewrite reads back differently"
    again = png.read(out)
    assert again.rows == read[f].rows and not again.interlaced, f
    kept = [k for k, _ in read[f].before + read[f].after]
    assert [k for _, k, _ in png.chunks(out)] == [b"IHDR"] + [k for k, _ in read[f].before] + [b"IDAT"] + [
        k for k, _ in read[f].after] + [b"IEND"], f
    assert b"gAMA" in kept or not any(k == b"gAMA" for _, k, _ in png.chunks(data)), f
    ours, theirs = ours + len(out), theirs + len(data)
print(f"all {len(GOOD)} rewritten and read back by PIL as the same pixels: {ours} bytes against the suite's {theirs}")

# The filters earn their place: a smooth gradient shrinks far more with them than with none.
gradient = png.Png(64, 64, 2, 8, False, [[(x * 4 + y) & 255 for x in range(64) for _ in range(3)] for y in range(64)], [], [])
unfiltered = png.SIGNATURE + png.chunk(b"IDAT", png.deflate.zlib_bytes(b"".join(b"\0" + bytes(r) for r in gradient.rows)))
assert len(png.write(gradient)) * 3 < len(unfiltered), (len(png.write(gradient)), len(unfiltered))
assert pil_pixels(png.write(gradient)) == [tuple(r[x:x + 3]) + (255,) for r in gradient.rows for x in range(0, 192, 3)]
print(f"a gradient takes {len(png.write(gradient))} bytes filtered, against {len(unfiltered)} without filters")
# A falling ramp: Sub leaves -3 a byte on the first row, Up -1 on the rest, small only when read as signed.
ramp = png.Png(40, 4, 0, 8, False, [[200 - 3 * x - y for x in range(40)] for y in range(4)], [], [])
body = b"".join(b for _, k, b in png.chunks(png.write(ramp)) if k == b"IDAT")
assert zlib.decompress(body)[::41] == b"\1\2\2\2", zlib.decompress(body)[::41]

# Netpbm: PGM and PPM as PIL reads them; PAM, which PIL cannot read, against PIL's RGBA.
for name in ("basn0g16", "basn2c08", "tbrn2c08", "basn3p04", "tp1n3p08", "basn4a08"):
    f = [g for g in GOOD if name in g][0]
    pam = png.netpbm(read[f])
    if pam.startswith(b"P7"):
        head, body = pam.split(b"ENDHDR\n")
        fields = dict(line.split(" ") for line in head.decode().split("\n")[1:-1])
        im = Image.open(f).convert("RGBA")
        want = bytes(s for p in im.getdata() for s in p)
        if fields["DEPTH"] == "2":
            want = bytes(s for p in im.getdata() for s in (p[0], p[3]))
        assert body == want and fields["WIDTH"] == str(im.width), name
    else:
        assert list(Image.open(io.BytesIO(pam)).getdata()) == list(Image.open(f).convert(
            "RGB" if pam.startswith(b"P6") else Image.open(f).mode).getdata()), name
print("decoded to PGM, PPM and PAM, matching PIL")

# From the command line: -i checks files and says which failed, a rewrite leaves a bigger result alone.
tmp = "t-png.png"
try:
    broken = [f for f in SUITE if "xcsn0g01" in f][0]
    run = subprocess.run([sys.executable, png.__file__, "-i", GOOD[0], broken], capture_output=True, text=True)
    assert run.returncode == 1 and "basi0g01.png: 32x32 gray, 1-bit, interlaced" in run.stdout and "xcsn" not in run.stdout, run
    assert "xcsn0g01.png: the IDAT chunk at offset 49 fails its CRC" in run.stderr, run
    run = subprocess.run([sys.executable, png.__file__, [g for g in GOOD if "z00n2c08" in g][0], "-o", tmp], capture_output=True, text=True)
    assert run.returncode == 0 and "smaller" in run.stdout and pil_pixels(open(tmp, "rb").read()) == pil_pixels(
        open([g for g in GOOD if "z00n2c08" in g][0], "rb").read()), run
    # Rewriting its own output again gains nothing, so the file is left as it was.
    run = subprocess.run([sys.executable, png.__file__, tmp], capture_output=True, text=True)
    assert run.returncode == 0 and "left alone" in run.stdout, run
finally:
    if os.path.exists(tmp):
        os.remove(tmp)
print("ok")
