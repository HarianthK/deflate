# deflate

A gzip compressor and decompressor written from the bits up, in one Python file with no
dependencies. It produces real `.gz` files that the `gzip` program reads.

    $ python deflate.py deflate.py
    deflate.py: 9377 -> 3584 bytes (61.8% smaller), auto blocks
    $ gzip -dc deflate.py.gz | diff - deflate.py && echo identical
    identical

DEFLATE is what gzip, zip, png and most HTTP responses are made of, and it is
two ideas stacked: LZ77 replaces anything it has seen before with "go back N
bytes and copy M", and Huffman coding gives the common symbols shorter bit
patterns than the rare ones.

## What it does

- LZ77 matching over a 32 KiB window, with a hash of the next three bytes.
- Huffman code lengths from a real tree, capped at the depth the format
  allows, then turned into canonical codes.
- All three block types: stored, fixed-table and dynamic-table. By default
  the input is cut into blocks where its statistics change, and each block is
  the cheapest of the three, priced exactly without writing it. That is why
  incompressible input never grows by more than the 5 bytes a stored block
  costs.
- Levels `-1` to `-9`, as gzip has: a shallower search for speed or a deeper
  one for size, 6 by default.
- The gzip envelope: header, CRC32 and length.
- The zlib envelope too (RFC 1950, what PNG and HTTP's `deflate` use), with
  `-z` as pigz has it, and preset dictionaries with `--dict FILE`: matches may
  reach back into bytes both ends already have, so short messages that look
  alike shrink. Eighteen 80-byte JSON records take 1,383 bytes alone and 396
  with two earlier records as the dictionary, the same as zlib at `-9`.
  Python's zlib reads what it writes and the other way round.
- The reading half too: `inflate` and `gunzip` decode any DEFLATE stream,
  including every level and strategy zlib writes, and refuse broken ones with
  a reason rather than misreading them.
- Zip archives, in `pkzip.py`: writing them (each file deflated, or stored if
  that is smaller), and listing, testing and extracting them. Python's
  `zipfile` and Windows' `tar` read what it writes, it reads what they and
  `git archive` write, and it refuses names that would extract outside the
  folder.
- PNG, in `png.py`: reading any PNG (all five colour types, every bit
  depth, Adam7 interlacing), rewriting it smaller, checking it as `pngcheck`
  does, and decoding it to Netpbm. All 161 images of the PngSuite read as
  Pillow reads them and all 14 broken ones are refused with a reason; the
  rewrites are 13% smaller than the suite's own files and Pillow reads each
  as the same pixels. The ray tracer's 640x360 render, written with no
  filters, goes from 195,604 bytes to 148,470, against 138,286 from Pillow's
  `optimize=True`.

## How it compares

`python test_deflate.py` compresses ten awkward inputs and has three other
programs read them back. Sizes against `gzip -9`:

| input | ours | gzip -9 |
| --- | --- | --- |
| 9 KB of repeated English | 102 | 108 |
| 70 KB of one repeated byte | 102 | 103 |
| 20 KB of random bytes | 20023 | 20028 |
| empty, and one byte | 20, 21 | 20, 21 |
| text, then noise, then other text, 54 KB | 32014 | 33195 |
| ember's DOCS.md, 28 KB of prose | 11069 | 11134 |
| ember's vm.rs, 59 KB of Rust | 12910 | 12875 |

That is our default level against gzip's best. At `-9` against `-9`, vm.rs is
12862 bytes to gzip's 12875. Smaller or level on every input but one,
because a file is cut into blocks, each with its own code tables, where its
contents change. DOCS.md has the measurements, and how the old one percent
gap turned out to be a level mismatch, not block splitting.

## Running it

    python deflate.py FILE [-o OUT] [-1..-9] [--stored|--fixed|--dynamic]
    python deflate.py -d FILE.gz [-o OUT]
    python deflate.py FILE -z [--dict DICT]   # a zlib stream, FILE.zz; -d and -t read it back, with --dict if it needs one
    python deflate.py -t FILE.gz ...      # check files, as gzip -t, writing nothing
    python deflate.py -l FILE.gz ...      # sizes and ratio, as gzip -l, without inflating (-N: stored name)
    python deflate.py FILE -c > FILE.gz   # -c writes to standard output, with -d too
    python deflate.py -d -N FILE.gz       # -N names the output after the name stored inside
    python test_deflate.py
    python pkzip.py OUT.zip FILE|DIR... [-1..-9]
    python pkzip.py -l | -t | -x ARCHIVE.zip [-d DIR]
    python test_pkzip.py
    python png.py IN.png [-o OUT.png] [-1..-9]   # rewrite smaller; left alone if it would not be
    python png.py -i FILE.png ...                # check every CRC and decode, one line a file
    python png.py -d IN.png [-o OUT.pam]         # to PGM, PPM or PAM, with tRNS as alpha
    python test_png.py                           # needs Pillow, as the second opinion

## Notes

[DOCS.md](DOCS.md) is what the format taught me, including the two bit-order
rules that make every first attempt at this fail.
