# Run: python png.py IN.png [-o OUT.png] [-1..-9] | -i FILE.png ... | -d IN.png [-o OUT.pam]
# PNG on top of deflate.py: read any PNG, rewrite it smaller, or decode it to Netpbm.
import math
import struct
import sys
import zlib
from collections import namedtuple

import deflate

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
COLORS = {0: "gray", 2: "RGB", 3: "palette", 4: "gray+alpha", 6: "RGBA"}
# Adam7: each pass's first column and row, then its step across and down.
PASSES = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
# Ancillary chunks a rewrite keeps although they are unsafe to copy, because it leaves the pixels alone.
KNOWN = {b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT", b"bKGD", b"hIST", b"pHYs", b"sPLT", b"tIME"}

Png = namedtuple("Png", "width height color depth interlaced rows before after")


def chunks(data):
    if data[:8] != SIGNATURE:
        raise ValueError("not a PNG file, or one damaged in transfer (the signature is wrong)")
    pos = 8
    while True:
        if pos + 12 > len(data):
            raise ValueError("the file ends before its IEND chunk")
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if pos + 12 + length > len(data):
            raise ValueError(f"the file ends inside the {kind!r} chunk at offset {pos}")
        body = data[pos + 8:pos + 8 + length]
        if zlib.crc32(kind + body) != struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]:
            raise ValueError(f"the {kind.decode('latin-1')} chunk at offset {pos} fails its CRC")
        yield pos, kind, body
        pos += 12 + length
        if kind == b"IEND":
            return


def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def unfilter(kind, line, prev, bpp):
    out = bytearray(line)
    n = len(out)
    if kind == 1:
        for i in range(bpp, n):
            out[i] = (out[i] + out[i - bpp]) & 255
    elif kind == 2:
        for i in range(n):
            out[i] = (out[i] + prev[i]) & 255
    elif kind == 3:
        for i in range(n):
            out[i] = (out[i] + ((out[i - bpp] if i >= bpp else 0) + prev[i] >> 1)) & 255
    elif kind == 4:
        for i in range(n):
            a, c = (out[i - bpp], prev[i - bpp]) if i >= bpp else (0, 0)
            out[i] = (out[i] + paeth(a, prev[i], c)) & 255
    elif kind:
        raise ValueError(f"filter type {kind}, but there are only five (0 to 4)")
    return out


def filtered(kind, line, prev, bpp):
    if kind == 0:
        return bytes(line)
    out = bytearray(len(line))
    for i, x in enumerate(line):
        a, c = (line[i - bpp], prev[i - bpp]) if i >= bpp else (0, 0)
        guess = a if kind == 1 else prev[i] if kind == 2 else (a + prev[i]) >> 1 if kind == 3 else paeth(a, prev[i], c)
        out[i] = (x - guess) & 255
    return out


def unpack(line, depth, count):
    if depth == 8:
        return list(line[:count])
    if depth == 16:
        return list(struct.unpack(f">{count}H", line[:2 * count]))
    per, mask = 8 // depth, (1 << depth) - 1
    return [line[i // per] >> (8 - depth * (i % per + 1)) & mask for i in range(count)]


def pack(samples, depth):
    if depth == 8:
        return bytes(samples)
    if depth == 16:
        return struct.pack(f">{len(samples)}H", *samples)
    per, out = 8 // depth, bytearray()
    for i in range(0, len(samples), per):
        out.append(sum(s << (8 - depth * (j + 1)) for j, s in enumerate(samples[i:i + per])))
    return bytes(out)


def read(data):
    header, idat, before, after, palette, last = None, [], [], [], None, None
    for pos, kind, body in chunks(data):
        if header is None and kind != b"IHDR":
            raise ValueError("the first chunk is not IHDR")
        if kind == b"IHDR":
            if header or len(body) != 13:
                raise ValueError("a second IHDR, or one that is not 13 bytes")
            width, height, depth, color, method, filtering, interlace = struct.unpack(">IIBBBBB", body)
            if not 0 < width < 2**31 or not 0 < height < 2**31:
                raise ValueError(f"a {width}x{height} image")
            if depth not in DEPTHS.get(color, ()):
                raise ValueError(f"color type {color} with bit depth {depth}, which PNG does not allow")
            if method or filtering or interlace > 1:
                raise ValueError("an unknown compression, filter or interlace method")
            header = width, height, depth, color, interlace
        elif kind == b"IDAT":
            if idat and last != b"IDAT":
                raise ValueError("IDAT chunks that are not consecutive")
            idat.append(body)
        elif kind == b"IEND":
            break
        elif kind == b"PLTE":
            if idat or palette is not None or not body or len(body) % 3 or len(body) > 768:
                raise ValueError("a PLTE chunk that is misplaced, repeated or not whole colours")
            palette = body
            before.append((kind, body))
        elif kind[0] & 0x20 == 0:
            raise ValueError(f"the critical chunk {kind.decode('latin-1')}, which this reader does not know")
        elif kind[3] & 0x20 or kind in KNOWN:
            (after if idat else before).append((kind, body))
        last = kind
    if not idat:
        raise ValueError("no IDAT chunk, so no image")
    width, height, depth, color, interlace = header
    if color == 3 and palette is None:
        raise ValueError("a palette image with no PLTE chunk")
    channels = CHANNELS[color]
    raw = deflate.unzlib(b"".join(idat))
    rows = [[0] * (width * channels) for _ in range(height)]
    pos = 0
    for x0, y0, dx, dy in PASSES if interlace else [(0, 0, 1, 1)]:
        across, down = math.ceil((width - x0) / dx), math.ceil((height - y0) / dy)
        if across <= 0 or down <= 0:
            continue
        stride, bpp = (across * channels * depth + 7) // 8, max(1, channels * depth // 8)
        prev = bytes(stride)
        for j in range(down):
            if pos + 1 + stride > len(raw):
                raise ValueError(f"the image data ends at row {j} of {down}")
            try:
                line = unfilter(raw[pos], raw[pos + 1:pos + 1 + stride], prev, bpp)
            except ValueError as e:
                raise ValueError(f"row {j}: {e}") from None
            pos += 1 + stride
            samples, row = unpack(line, depth, across * channels), rows[y0 + j * dy]
            for i in range(across):
                x = (x0 + i * dx) * channels
                row[x:x + channels] = samples[i * channels:(i + 1) * channels]
            prev = line
    if pos != len(raw):
        raise ValueError(f"{len(raw) - pos} bytes of image data after the last row")
    if color == 3 and any(i >= len(palette) // 3 for row in rows for i in row):
        raise ValueError("a pixel names a colour past the end of the palette")
    return Png(width, height, color, depth, bool(interlace), rows, before, after)


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def write(image, level=6):
    # Always without interlacing: it only ever costs bytes, and buys a blurry preview while loading.
    channels = CHANNELS[image.color]
    bpp = max(1, channels * image.depth // 8)
    raw, prev = bytearray(), bytes((image.width * channels * image.depth + 7) // 8)
    for row in image.rows:
        line = pack(row, image.depth)
        if image.color == 3 or image.depth < 8:
            # As the spec advises and libpng does: filters predict nothing useful from palette indexes.
            kind, out = 0, line
        else:
            # libpng's heuristic: the filter whose bytes, read as signed, sum smallest.
            options = [filtered(k, line, prev, bpp) for k in range(5)]
            kind = min(range(5), key=lambda k: sum(b if b < 128 else 256 - b for b in options[k]))
            out = options[kind]
        raw.append(kind)
        raw += out
        prev = line
    ihdr = struct.pack(">IIBBBBB", image.width, image.height, image.depth, image.color, 0, 0, 0)
    return (SIGNATURE + chunk(b"IHDR", ihdr) + b"".join(chunk(k, b) for k, b in image.before)
            + chunk(b"IDAT", deflate.zlib_bytes(bytes(raw), level=level))
            + b"".join(chunk(k, b) for k, b in image.after) + chunk(b"IEND", b""))


def pixels(image):
    # Plain samples for Netpbm: palettes looked up, and tRNS turned into an alpha channel.
    extra = dict(image.before)
    trns, maxval = extra.get(b"tRNS"), (1 << image.depth) - 1
    if image.color == 3:
        plte = extra[b"PLTE"]
        alpha = (trns or b"") + b"\xff" * 256
        colours = [tuple(plte[3 * i:3 * i + 3]) + ((alpha[i],) if trns else ()) for i in range(len(plte) // 3)]
        return 4 if trns else 3, 255, [[s for i in row for s in colours[i]] for row in image.rows]
    if trns and image.color in (0, 2):
        n = CHANNELS[image.color]
        key = list(struct.unpack(f">{n}H", trns[:2 * n]))
        return n + 1, maxval, [[s for x in range(0, len(row), n)
                                for s in row[x:x + n] + [0 if row[x:x + n] == key else maxval]] for row in image.rows]
    return CHANNELS[image.color], maxval, image.rows


def netpbm(image):
    depth, maxval, rows = pixels(image)
    samples = [s for row in rows for s in row]
    body = bytes(samples) if maxval < 256 else struct.pack(f">{len(samples)}H", *samples)
    if depth in (1, 3):
        return f"P{5 if depth == 1 else 6}\n{image.width} {image.height}\n{maxval}\n".encode() + body
    kind = "GRAYSCALE_ALPHA" if depth == 2 else "RGB_ALPHA"
    return (f"P7\nWIDTH {image.width}\nHEIGHT {image.height}\nDEPTH {depth}\nMAXVAL {maxval}\n"
            f"TUPLTYPE {kind}\nENDHDR\n").encode() + body


def describe(data):
    image = read(data)
    names = [kind.decode("latin-1") for _, kind, _ in chunks(data)]
    shown = [f"{n} x{names.count(n)}" if names.count(n) > 1 else n for n in dict.fromkeys(names)]
    return (f"{image.width}x{image.height} {COLORS[image.color]}, {image.depth}-bit"
            f"{', interlaced' if image.interlaced else ''}; chunks {' '.join(shown)}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    args = [a for i, a in enumerate(argv) if not a.startswith("-") and (i == 0 or argv[i - 1] != "-o")]
    if not args:
        sys.exit("usage: python png.py IN.png [-o OUT.png] [-1..-9] | -i FILE.png ... | -d IN.png [-o OUT]")
    if "-i" in argv:
        # As pngcheck: every chunk's CRC and the whole image decoded, one line a file.
        bad = 0
        for name in args:
            try:
                print(f"{name}: {describe(open(name, 'rb').read())}")
            except (OSError, ValueError) as e:
                print(f"{name}: {e}", file=sys.stderr)
                bad += 1
        sys.exit(1 if bad else 0)
    source = args[0]
    try:
        data = open(source, "rb").read()
        image = read(data)
    except (OSError, ValueError) as e:
        sys.exit(f"png: {source}: {e}")
    if "-d" in argv:
        out = argv[argv.index("-o") + 1] if "-o" in argv else source.removesuffix(".png") + ".pam"
        open(out, "wb").write(netpbm(image))
        sys.exit(print(f"{source}: {image.width}x{image.height} to {out}"))
    level = next((int(a[1]) for a in argv if len(a) == 2 and a[0] == "-" and a[1] in "123456789"), 6)
    packed = write(image, level)
    out = argv[argv.index("-o") + 1] if "-o" in argv else source
    if len(packed) >= len(data) and out == source:
        sys.exit(print(f"{source}: {len(data)} bytes, already as small as this makes it; left alone"))
    open(out, "wb").write(packed)
    print(f"{source}: {len(data)} -> {len(packed)} bytes ({100 * (1 - len(packed) / len(data)):.1f}% smaller) to {out}")
