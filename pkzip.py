# Zip archives on top of deflate.py: writes them, lists them, tests and extracts them.
# Run: python pkzip.py OUT.zip FILE|DIR..., or -l / -t / -x ARCHIVE.zip [-d DIR]. DOCS.md has the format.
import os
import struct
import sys
import time
import zlib

import deflate

LOCAL, CENTRAL, END = b"PK\x03\x04", b"PK\x01\x02", b"PK\x05\x06"
STORED, DEFLATED = 0, 8
UTF8 = 0x800  # flag bit 11: the name is UTF-8, not the old DOS code page


def dos_time(stamp):
    # Zip keeps MS-DOS times: two-second steps, local time, nothing before 1980.
    t = time.localtime(stamp)
    if t.tm_year < 1980:
        return 0, 1 << 5 | 1  # 1980-01-01 00:00, the earliest there is
    return t.tm_hour << 11 | t.tm_min << 5 | t.tm_sec // 2, (t.tm_year - 1980) << 9 | t.tm_mon << 5 | t.tm_mday


def from_dos(clock, date):
    return f"{(date >> 9) + 1980:04d}-{date >> 5 & 15:02d}-{date & 31:02d} {clock >> 11:02d}:{clock >> 5 & 63:02d}"


def build(entries, level=6):
    # entries: (name, bytes or None for a directory, mtime). Each file is stored if DEFLATE would not shrink it.
    out, central = bytearray(), bytearray()
    for name, data, mtime in entries:
        raw_name = name.encode()
        flags = UTF8 if not raw_name.isascii() else 0
        clock, date = dos_time(mtime)
        if data is None:
            method, packed, crc, size, attrs = STORED, b"", 0, 0, 0o40755 << 16 | 0x10
        else:
            packed, method = deflate.deflate(data, level=level), DEFLATED
            if len(packed) >= len(data):
                packed, method = data, STORED
            crc, size, attrs = zlib.crc32(data), len(data), 0o100644 << 16
        if size > 0xFFFFFFFE or len(out) > 0xFFFFFFFE:
            raise ValueError(f"{name}: over 4 GiB needs ZIP64, which this does not write")
        fields = struct.pack("<HHHHIII", flags, method, clock, date, crc, len(packed), size)
        central += CENTRAL + struct.pack("<HH", 3 << 8 | 20, 20) + fields + struct.pack("<HHHHHII", len(raw_name), 0, 0, 0, 0, attrs, len(out)) + raw_name
        out += LOCAL + struct.pack("<H", 20) + fields + struct.pack("<HH", len(raw_name), 0) + raw_name + packed
    return bytes(out + central + END + struct.pack("<HHHHIIH", 0, 0, len(entries), len(entries), len(central), len(out), 0))


def gather(paths):
    # The entries for files and whole directories, named with forward slashes from where each path starts.
    entries = []
    for path in paths:
        top = os.path.dirname(os.path.abspath(path))
        name = lambda p: os.path.relpath(p, top).replace(os.sep, "/")
        if os.path.isfile(path):
            entries.append((name(path), open(path, "rb").read(), os.path.getmtime(path)))
            continue
        for here, dirs, files in os.walk(path):
            dirs.sort()
            entries.append((name(here) + "/", None, os.path.getmtime(here)))
            entries += [(name(os.path.join(here, f)), open(os.path.join(here, f), "rb").read(), os.path.getmtime(os.path.join(here, f)))
                        for f in sorted(files)]
    return entries


def members(archive):
    # The central directory, found from the end record at the back; it is the archive's real index.
    at = archive.rfind(END, max(0, len(archive) - 22 - 65535))
    if at < 0 or len(archive) < at + 22:
        raise ValueError("not a zip archive (no end of central directory record)")
    count, size, start = struct.unpack("<HII", archive[at + 10:at + 20])
    if start == 0xFFFFFFFF or count == 0xFFFF:
        raise ValueError("a ZIP64 archive, which this does not read")
    if start + size > at:
        raise ValueError("the central directory runs past where it should end")
    found, pos = [], start
    for _ in range(count):
        if archive[pos:pos + 4] != CENTRAL:
            raise ValueError(f"no central directory entry at offset {pos}")
        flags, method, clock, date, crc, packed, size = struct.unpack("<HHHHIII", archive[pos + 8:pos + 28])
        name_len, extra_len, comment_len = struct.unpack("<HHH", archive[pos + 28:pos + 34])
        offset = struct.unpack("<I", archive[pos + 42:pos + 46])[0]
        raw = archive[pos + 46:pos + 46 + name_len]
        name = raw.decode("utf-8" if flags & UTF8 else "cp437")
        found.append({"name": name, "flags": flags, "method": method, "crc": crc, "packed": packed, "size": size,
                      "offset": offset, "when": from_dos(clock, date)})
        pos += 46 + name_len + extra_len + comment_len
    return found


def contents(archive, m):
    # A member's bytes, with its CRC checked. The local header's own name and extra lengths say where data starts.
    if m["flags"] & 1:
        raise ValueError(f"{m['name']} is encrypted")
    at = m["offset"]
    if archive[at:at + 4] != LOCAL:
        raise ValueError(f"{m['name']}: no local header at offset {at}")
    name_len, extra_len = struct.unpack("<HH", archive[at + 26:at + 30])
    start = at + 30 + name_len + extra_len
    # Sizes come from the central directory: with flag bit 3 the local header's are zero, and follow the data.
    body = archive[start:start + m["packed"]]
    if len(body) < m["packed"]:
        raise ValueError(f"{m['name']}: the archive ends early")
    if m["method"] == STORED:
        data = body
    elif m["method"] == DEFLATED:
        data, _ = deflate.inflate(body)  # body is cut to its size, so a stream that runs on "ends early"
    else:
        raise ValueError(f"{m['name']} uses compression method {m['method']}, which this does not read")
    if len(data) != m["size"] or zlib.crc32(data) != m["crc"]:
        raise ValueError(f"{m['name']}: the CRC or size does not match the data")
    return data


def safe_path(root, name):
    # Zip slip: a member named ../x or /x or C:x would be written outside the folder it is extracted to.
    parts = name.replace("\\", "/").split("/")
    if name.startswith(("/", "\\")) or any(":" in p for p in parts) or ".." in parts:
        raise ValueError(f"{name}: refusing a name that leaves the folder it is extracted to")
    return os.path.join(root, *[p for p in parts if p])


def extract(archive, root):
    done = []
    for m in members(archive):
        path = safe_path(root, m["name"])
        if m["name"].endswith("/"):
            os.makedirs(path, exist_ok=True)
            continue
        data = contents(archive, m)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(path, "wb").write(data)
        done.append(path)
    return done


if __name__ == "__main__":
    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        sys.exit("usage: python pkzip.py OUT.zip FILE|DIR... [-1..-9], or -l | -t | -x ARCHIVE.zip [-d DIR]")
    if "-d" in argv:
        args.remove(argv[argv.index("-d") + 1])
    try:
        if "-l" in argv or "-t" in argv or "-x" in argv:
            archive = open(args[0], "rb").read()
            listed = members(archive)
            if "-l" in argv:
                # As unzip -l: size, date and name, then the totals.
                print(f"{'Length':>10}  {'Date':10} {'Time':5}   Name")
                for m in listed:
                    print(f"{m['size']:>10}  {m['when']}   {m['name']}")
                print(f"{sum(m['size'] for m in listed):>10}  {'':16}   {len(listed)} files")
            elif "-t" in argv:
                for m in listed:
                    if not m["name"].endswith("/"):
                        contents(archive, m)
                print(f"{args[0]}: OK, {len(listed)} entries")
            else:
                root = argv[argv.index("-d") + 1] if "-d" in argv else "."
                print(f"{args[0]}: {len(extract(archive, root))} files to {root}")
        else:
            level = next((int(a[1]) for a in argv if len(a) == 2 and a[0] == "-" and a[1] in "123456789"), 6)
            entries = gather(args[1:])
            packed = build(entries, level)
            open(args[0], "wb").write(packed)
            total = sum(len(d) for _, d, _ in entries if d)
            print(f"{args[0]}: {len(entries)} entries, {total} -> {len(packed)} bytes")
    except (OSError, ValueError) as e:
        sys.exit(f"pkzip: {e}")
