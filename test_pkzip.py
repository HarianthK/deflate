# Run: python test_pkzip.py. Archives pkzip writes must open in Python's zipfile and Windows' tar,
# archives they and git write must open in pkzip, and broken or hostile ones must be refused.
import io
import os
import random
import shutil
import subprocess
import tempfile
import time
import zipfile

import pkzip

rng = random.Random(3)
tmp = tempfile.mkdtemp()
TAR = r"C:\Windows\System32\tar.exe"  # bsdtar, which reads and writes zip; git bash's GNU tar does not


def refused(archive, words):
    try:
        for m in pkzip.members(archive):
            if not m["name"].endswith("/"):
                pkzip.contents(archive, m)
            pkzip.safe_path(tmp, m["name"])
        raise AssertionError(f"a bad archive was read ({words})")
    except ValueError as e:
        assert words in str(e), (words, e)


def read_all(archive):
    return {m["name"]: pkzip.contents(archive, m) for m in pkzip.members(archive) if not m["name"].endswith("/")}


try:
    tree = os.path.join(tmp, "tree")
    files = {"readme.txt": b"hello zip\n" * 300, "src/main.py": open("deflate.py", "rb").read(), "src/deep/noise.bin": rng.randbytes(4000),
             "empty": b"", "na\u00efve caf\u00e9.txt": "non-ASCII name\n".encode() * 20}
    for name, data in files.items():
        os.makedirs(os.path.dirname(os.path.join(tree, name)), exist_ok=True)
        open(os.path.join(tree, name), "wb").write(data)
    os.makedirs(os.path.join(tree, "nothing here"))
    ours = pkzip.build(pkzip.gather([tree]))

    # Python's zipfile reads every member back, with the CRCs, and sees which were stored.
    z = zipfile.ZipFile(io.BytesIO(ours))
    assert z.testzip() is None
    for name, data in files.items():
        info = z.getinfo("tree/" + name)
        assert z.read(info) == data, name
        assert info.compress_type == (zipfile.ZIP_STORED if name in ("src/deep/noise.bin", "empty") else zipfile.ZIP_DEFLATED), name
    assert z.getinfo("tree/nothing here/").is_dir() and z.getinfo("tree/na\u00efve caf\u00e9.txt").flag_bits & 0x800
    assert read_all(ours) == {"tree/" + n: d for n, d in files.items()}
    out = os.path.join(tmp, "extracted")
    assert len(pkzip.extract(ours, out)) == len(files)
    assert all(open(os.path.join(out, "tree", n), "rb").read() == d for n, d in files.items()) and os.path.isdir(os.path.join(out, "tree", "nothing here"))
    print(f"our archive of {len(files)} files and 4 folders: zipfile reads it, random bytes stored, the rest deflated; it extracts whole")
    if os.path.exists(TAR):
        out = os.path.join(tmp, "by tar")
        os.makedirs(out)
        open(os.path.join(tmp, "ours.zip"), "wb").write(ours)
        subprocess.run([TAR, "-xf", os.path.join(tmp, "ours.zip"), "-C", out], check=True)
        for name, data in files.items():
            assert open(os.path.join(out, "tree", name), "rb").read() == data, name
        print("and Windows' tar (libarchive) extracts it")

    # The other way: archives others wrote, each with its own habits.
    made = {}
    for label, kind, level in [("zipfile stored", zipfile.ZIP_STORED, None), ("zipfile level 1", zipfile.ZIP_DEFLATED, 1),
                               ("zipfile level 9", zipfile.ZIP_DEFLATED, 9)]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", kind, compresslevel=level) as w:
            w.comment = b"an archive comment, which sits after the end record"
            for name, data in files.items():
                w.writestr(name, data)
        made[label] = buf.getvalue()

    class Pipe(io.RawIOBase):
        # A stream zipfile cannot seek back in, so it writes each size after the data (flag bit 3).
        def __init__(self):
            self.data = bytearray()

        def writable(self):
            return True

        def write(self, b):
            self.data += b
            return len(b)
    pipe = Pipe()
    with zipfile.ZipFile(pipe, "w", zipfile.ZIP_DEFLATED) as w:
        for name, data in files.items():
            with w.open(name, "w") as f:
                f.write(data)
    made["zipfile to a pipe, sizes after the data"] = bytes(pipe.data)
    assert all(m["flags"] & 8 for m in pkzip.members(made["zipfile to a pipe, sizes after the data"]))
    for label, archive in made.items():
        assert read_all(archive) == files, label
    if os.path.exists(TAR):
        subprocess.run([TAR, "-a", "-cf", os.path.join(tmp, "tar.zip"), "-C", tree, "."], check=True)
        got = read_all(open(os.path.join(tmp, "tar.zip"), "rb").read())
        assert {n.removeprefix("./"): d for n, d in got.items()} == files, sorted(got)
        made["tar"] = True
    git_zip = subprocess.run(["git", "-c", "core.autocrlf=false", "archive", "--format=zip", "HEAD"], capture_output=True, check=True).stdout
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    got = read_all(git_zip)
    assert sorted(got) == sorted(tracked)
    for name in tracked:
        assert got[name] == subprocess.run(["git", "show", f"HEAD:{name}"], capture_output=True, check=True).stdout, name
    print(f"read archives from {', '.join(made)} and git archive of this repository")

    # Damage and hostility. Zip slip names would write outside the folder; each is refused.
    def hostile(name):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as w:
            w.writestr(zipfile.ZipInfo(name), b"pwned")
        return buf.getvalue()
    for name in ("../evil", "a/../../evil", "/etc/evil", "\\evil", "C:evil", "a/D:/evil"):
        refused(hostile(name), "leaves the folder")
    try:
        pkzip.extract(hostile("../evil"), os.path.join(tmp, "jail"))
        raise AssertionError("a zip-slip member was extracted")
    except ValueError:
        assert not os.path.exists(os.path.join(tmp, "evil"))
    damaged = bytearray(ours)
    first = next(m for m in pkzip.members(ours) if m["name"] == "tree/readme.txt")
    damaged[first["offset"] + 30 + len(first["name"].encode()) + first["packed"] // 2] ^= 1
    try:  # either the DEFLATE stream or the CRC says so
        pkzip.contents(bytes(damaged), first)
        raise AssertionError("a damaged member was read")
    except ValueError:
        pass
    stored = made["zipfile stored"]
    m = pkzip.members(stored)[0]
    flipped = bytearray(stored)
    flipped[m["offset"] + 30 + len(m["name"]) + 2] ^= 1
    refused(bytes(flipped), "CRC")
    refused(ours[:len(ours) // 2], "not a zip archive")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_BZIP2) as w:
        w.writestr("b", b"bzip2 inside")
    refused(buf.getvalue(), "method 12")
    locked = bytearray(made["zipfile stored"])
    at = locked.index(pkzip.CENTRAL)
    locked[at + 8] |= 1
    refused(bytes(locked), "encrypted")
    # The last member cut to 10 bytes, the central directory kept: its offsets now point past the data.
    cut = ours[:pkzip.members(ours)[-1]["offset"] + 10] + ours[ours.index(pkzip.CENTRAL):]
    refused(cut, "runs past")
    print("six zip-slip names, a damaged stream, a flipped stored byte, half an archive, bzip2, encryption and a cut member refused")

    assert pkzip.from_dos(*pkzip.dos_time(0)).startswith("1980-01-01")  # nothing before 1980
    assert pkzip.from_dos(*pkzip.dos_time(1700000000)) == time.strftime("%Y-%m-%d %H:%M", time.localtime(1700000000))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("ok")
