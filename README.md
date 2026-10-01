# deflate

A gzip compressor written from the bits up, in one Python file with no
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
- All three block types: stored, fixed-table and dynamic-table. By default it
  builds all three and keeps the smallest, which is why incompressible input
  never grows by more than the 5 bytes a stored block costs.
- The gzip envelope: header, CRC32 and length.

## How it compares

`python test_deflate.py` compresses ten awkward inputs and has three other
programs read them back. Sizes against `gzip -9`:

| input | ours | gzip -9 |
| --- | --- | --- |
| 9 KB of repeated English | 102 | 108 |
| 70 KB of one repeated byte | 102 | 103 |
| 20 KB of random bytes | 20023 | 20028 |
| empty, and one byte | 20, 21 | 20, 21 |
| ember's DOCS.md, 22 KB of prose | 9334 | 9302 |
| ember's vm.rs, 36 KB of Rust | 8288 | 8189 |

Level with gzip or smaller on repetitive input, and within about one percent
on real text, where gzip's longer hash chains still find a few more matches.
Lazy matching, below, closed most of the gap that was there.

## Running it

    python deflate.py FILE [-o OUT] [--stored|--fixed|--dynamic]
    python test_deflate.py

## Notes

[DOCS.md](DOCS.md) is what the format taught me, including the two bit-order
rules that make every first attempt at this fail.
