# Writes a real gzip file: LZ77 matching, then Huffman coding, then the DEFLATE bit layout.
# Run: python deflate.py FILE [-o OUT] [--stored|--fixed|--dynamic]. DOCS.md explains the format.
import heapq
import struct
import sys
import zlib
from collections import Counter

WINDOW = 32768
MIN_MATCH, MAX_MATCH = 3, 258

# Lengths 3..258 are coded as symbols 257..285, each with a base and some extra bits.
LENGTH_CODES = [(257, 3, 0), (258, 4, 0), (259, 5, 0), (260, 6, 0), (261, 7, 0), (262, 8, 0), (263, 9, 0), (264, 10, 0),
                (265, 11, 1), (266, 13, 1), (267, 15, 1), (268, 17, 1), (269, 19, 2), (270, 23, 2), (271, 27, 2),
                (272, 31, 2), (273, 35, 3), (274, 43, 3), (275, 51, 3), (276, 59, 3), (277, 67, 4), (278, 83, 4),
                (279, 99, 4), (280, 115, 4), (281, 131, 5), (282, 163, 5), (283, 195, 5), (284, 227, 5), (285, 258, 0)]
# Distances 1..32768 are codes 0..29, same idea.
DISTANCE_CODES = [(0, 1, 0), (1, 2, 0), (2, 3, 0), (3, 4, 0), (4, 5, 1), (5, 7, 1), (6, 9, 2), (7, 13, 2), (8, 17, 3),
                  (9, 25, 3), (10, 33, 4), (11, 49, 4), (12, 65, 5), (13, 97, 5), (14, 129, 6), (15, 193, 6),
                  (16, 257, 7), (17, 385, 7), (18, 513, 8), (19, 769, 8), (20, 1025, 9), (21, 1537, 9), (22, 2049, 10),
                  (23, 3073, 10), (24, 4097, 11), (25, 6145, 11), (26, 8193, 12), (27, 12289, 12), (28, 16385, 13),
                  (29, 24577, 13)]
CODE_LENGTH_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]


class BitWriter:
    # DEFLATE fills each byte from its low end, but Huffman codes are written high bit first.
    def __init__(self):
        self.out = bytearray()
        self.bits = 0
        self.count = 0

    def write(self, value, n):
        self.bits |= (value & ((1 << n) - 1)) << self.count
        self.count += n
        while self.count >= 8:
            self.out.append(self.bits & 0xFF)
            self.bits >>= 8
            self.count -= 8

    def write_code(self, code, n):
        for i in range(n - 1, -1, -1):
            self.write((code >> i) & 1, 1)

    def align(self):
        if self.count:
            self.out.append(self.bits & 0xFF)
            self.bits = self.count = 0


def code_for(table, value):
    for symbol, base, extra in reversed(table):
        if value >= base:
            return symbol, value - base, extra
    raise ValueError(value)


def lz77(data):
    # Longest match in the last 32 KiB, found by hashing the next three bytes.
    heads, tokens, i = {}, [], 0

    def remember(pos):
        key = bytes(data[pos:pos + MIN_MATCH])
        if len(key) == MIN_MATCH:
            chain = heads.setdefault(key, [])
            chain.insert(0, pos)
            del chain[16:]  # the 16 most recent starts; older ones rarely win

    def longest(at):
        best_len, best_dist = 0, 0
        for candidate in heads.get(bytes(data[at:at + MIN_MATCH]), ()):
            dist = at - candidate
            if dist > WINDOW:
                break
            length = 0
            while length < MAX_MATCH and at + length < len(data) and data[candidate + length] == data[at + length]:
                length += 1
            if length > best_len:
                best_len, best_dist = length, dist
                if length == MAX_MATCH:
                    break
        return best_len, best_dist

    while i < len(data):
        best_len, best_dist = longest(i)
        remember(i)
        # Lazy matching, as gzip does: if a match starting one byte later is longer, emit
        # this byte as a literal and take that one instead.
        if MIN_MATCH <= best_len < MAX_MATCH and longest(i + 1)[0] > best_len:
            tokens.append(data[i])
            i += 1
            continue
        if best_len >= MIN_MATCH:
            tokens.append((best_len, best_dist))
            for j in range(i + 1, i + best_len):
                remember(j)
            i += best_len
        else:
            tokens.append(data[i])
            i += 1
    return tokens


def lengths_from(counts, limit):
    # A Huffman tree, then anything deeper than the format allows is flattened.
    if not counts:
        return {}
    if len(counts) == 1:
        return {next(iter(counts)): 1}
    heap = [[weight, i, {symbol: 0}] for i, (symbol, weight) in enumerate(sorted(counts.items()))]
    heapq.heapify(heap)
    nxt = len(heap)
    while len(heap) > 1:
        w1, _, a = heapq.heappop(heap)
        w2, _, b = heapq.heappop(heap)
        merged = {s: d + 1 for s, d in list(a.items()) + list(b.items())}
        heapq.heappush(heap, [w1 + w2, nxt, merged])
        nxt += 1
    lengths = heap[0][2]
    if max(lengths.values()) > limit:
        # Rare, and costs a few bits: cap the deep codes, then lengthen cheap ones until Kraft holds.
        for symbol in lengths:
            lengths[symbol] = min(lengths[symbol], limit)
        while sum(2.0 ** -length for length in lengths.values()) > 1.0000001:
            symbol = min((s for s in lengths if lengths[s] < limit), key=lambda s: (lengths[s], -counts[s]))
            lengths[symbol] += 1
    return lengths


def canonical(lengths):
    # Same lengths always give the same codes: shorter first, then by symbol.
    codes, code, prev = {}, 0, 0
    for symbol in sorted(lengths, key=lambda s: (lengths[s], s)):
        code <<= lengths[symbol] - prev
        codes[symbol] = code
        code += 1
        prev = lengths[symbol]
    return codes


def run_lengths(table):
    # The code tables are themselves compressed: 16 repeats the last length, 17 and 18 are runs of zeros.
    out, i = [], 0
    while i < len(table):
        value = table[i]
        run = 1
        while i + run < len(table) and table[i + run] == value and run < 138:
            run += 1
        if value == 0 and run >= 3:
            out.append((17, run - 3, 3) if run <= 10 else (18, min(run, 138) - 11, 7))
            i += min(run, 138 if run > 10 else 10)
        elif run >= 4 and i > 0 and table[i - 1] == value:
            repeat = min(run, 6)
            out.append((16, repeat - 3, 2))
            i += repeat
        else:
            out.append((value, 0, 0))
            i += 1
    return out


def deflate(data, mode="auto"):
    # Incompressible input costs bits to Huffman-code, so the smallest of the three wins.
    if mode == "auto":
        return min((deflate(data, m) for m in ("stored", "fixed", "dynamic")), key=len)
    w = BitWriter()
    if mode == "stored":
        blocks = [data[s:s + 65535] for s in range(0, len(data), 65535)] or [b""]
        for n, block in enumerate(blocks):
            w.write(1 if n == len(blocks) - 1 else 0, 1)
            w.write(0, 2)
            w.align()
            w.out += struct.pack("<HH", len(block), len(block) ^ 0xFFFF) + block
        return bytes(w.out)

    tokens = lz77(data)
    lit_counts, dist_counts = Counter(), Counter()
    for token in tokens:
        if isinstance(token, int):
            lit_counts[token] += 1
        else:
            length, dist = token
            lit_counts[code_for(LENGTH_CODES, length)[0]] += 1
            dist_counts[code_for(DISTANCE_CODES, dist)[0]] += 1
    lit_counts[256] += 1  # end of block

    if mode == "fixed":
        lit_lengths = {s: (8 if s < 144 else 9 if s < 256 else 7 if s < 280 else 8) for s in range(288)}
        dist_lengths = {s: 5 for s in range(30)}
        w.write(1, 1)
        w.write(1, 2)
    else:
        lit_lengths = lengths_from(lit_counts, 15)
        dist_lengths = lengths_from(dist_counts, 15) or {0: 1}
        hlit = max(max(lit_lengths), 256) + 1
        hdist = max(dist_lengths) + 1
        table = [lit_lengths.get(s, 0) for s in range(hlit)] + [dist_lengths.get(s, 0) for s in range(hdist)]
        items = run_lengths(table)
        cl_lengths = lengths_from(Counter(symbol for symbol, _, _ in items), 7)
        hclen = max(4, max((i + 1 for i, s in enumerate(CODE_LENGTH_ORDER) if cl_lengths.get(s)), default=4))
        w.write(1, 1)
        w.write(2, 2)
        w.write(hlit - 257, 5)
        w.write(hdist - 1, 5)
        w.write(hclen - 4, 4)
        for s in CODE_LENGTH_ORDER[:hclen]:
            w.write(cl_lengths.get(s, 0), 3)
        cl_codes = canonical(cl_lengths)
        for symbol, extra, extra_bits in items:
            w.write_code(cl_codes[symbol], cl_lengths[symbol])
            if extra_bits:
                w.write(extra, extra_bits)

    lit_codes, dist_codes = canonical(lit_lengths), canonical(dist_lengths)
    for token in tokens:
        if isinstance(token, int):
            w.write_code(lit_codes[token], lit_lengths[token])
        else:
            length, dist = token
            symbol, extra, extra_bits = code_for(LENGTH_CODES, length)
            w.write_code(lit_codes[symbol], lit_lengths[symbol])
            if extra_bits:
                w.write(extra, extra_bits)
            symbol, extra, extra_bits = code_for(DISTANCE_CODES, dist)
            w.write_code(dist_codes[symbol], dist_lengths[symbol])
            if extra_bits:
                w.write(extra, extra_bits)
    w.write_code(lit_codes[256], lit_lengths[256])
    w.align()
    return bytes(w.out)


def gzip_bytes(data, name=None, mode="auto"):
    header = struct.pack("<BBBBIBB", 0x1F, 0x8B, 8, 8 if name else 0, 0, 0, 3)
    if name:
        header += name.encode() + b"\0"
    return header + deflate(data, mode) + struct.pack("<II", zlib.crc32(data), len(data) & 0xFFFFFFFF)


if __name__ == "__main__":
    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        sys.exit("usage: python deflate.py FILE [-o OUT] [--stored|--fixed|--dynamic]")
    mode = "stored" if "--stored" in argv else "fixed" if "--fixed" in argv else "dynamic" if "--dynamic" in argv else "auto"
    source = args[0]
    out = argv[argv.index("-o") + 1] if "-o" in argv else source + ".gz"
    data = open(source, "rb").read()
    packed = gzip_bytes(data, name=source.replace("\\", "/").split("/")[-1], mode=mode)
    open(out, "wb").write(packed)
    ratio = 100 * (1 - len(packed) / len(data)) if data else 0
    print(f"{source}: {len(data)} -> {len(packed)} bytes ({ratio:.1f}% smaller), {mode} blocks")
