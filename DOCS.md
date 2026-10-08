# What I learned writing a gzip compressor

## Bits go in two directions at once

This is the part that breaks every first attempt. DEFLATE fills a byte from
its low end: the first bit of the stream is bit 0 of byte 0. But a Huffman
code is written most significant bit first, because that is what makes a
decoder able to read it one bit at a time without knowing the length in
advance. So the packer has two methods: `write` for plain numbers, which go
in low end first, and `write_code` for Huffman codes, which walks the code
from its top bit down and hands each bit to `write`. Extra bits after a
length or distance are plain numbers again.

## Nothing is a literal byte any more

After LZ77 the data is a stream of symbols, not bytes. Symbols 0..255 are
literal bytes, 256 means the block ends, and 257..285 are lengths: symbol 269
plus two extra bits covers lengths 19 to 22. Distances are a second alphabet,
0..29, on the same base-plus-extra-bits plan. The tables look arbitrary until
you notice they are the exponents of a rough logarithmic scale: near matches
and short lengths are cheap and precise, far ones are coarse.

## Canonical codes mean the tree is never sent

A Huffman tree would be expensive to transmit. It does not have to be: if
both sides agree that codes are assigned in order of length and then symbol,
then the code lengths alone determine every code. So the block header carries
only a list of lengths, one per symbol. That list is itself compressed, with
its own little Huffman code, and with run-length symbols for repeats and for
runs of zeros, which matter because most files use a small part of the 286
possible symbols. The header is therefore three codes deep: a code for the
lengths of the code that codes the lengths.

## The depth limit is a real constraint

Codes may be at most 15 bits (7 for the header's own code). A file with a
very skewed distribution can produce a deeper tree, and then the lengths have
to be flattened so the tree still closes up: the Kraft sum, the total of one
over two to the power of each length, must come to exactly one.

The first version capped the deep codes and then lengthened short ones until
the sum fell to one or below. Below was the bug. A sum under one is an
incomplete code, and zlib rejects every incomplete code with "invalid code
lengths set", so some inputs made files that no gzip could read. None of the
test inputs happened to push the header's 7-bit code past its limit, and the
bug sat there for ten days. It surfaced when testing an unrelated change
altered the test's own source, which the test also compresses: on that one
input the header's code came out at 0.883.

The fix stops trimming a finished tree. When the tree is too deep, the counts
are halved, rounding up so nothing reaches zero, and the tree is built again,
until it fits. A Huffman tree is complete by construction, so the code always
is. It is not optimal, which package-merge would be, but it can only cost a few
bits, never a broken file. A test now builds codes from Fibonacci counts, which
make the deepest tree possible and so always hit the limit, and checks the sum
is exactly one with exact fractions; the old trimming fails it.

## Compression can make a file bigger, so gzip cheats

Random data has no matches and a flat symbol distribution, so coding it costs
more bits than it saves, and the code tables are pure overhead. That is why
the format has a stored block: no compression, five bytes of header, copy the
bytes. A compressor is expected to try the block types and keep the smallest,
which is what `auto` does here, and it is why zipping a JPEG makes it about
0.02% bigger instead of noticeably bigger.

## Where the last few percent live

The first version took the first long match it found. gzip does not: when it
finds a match at position i, it also looks at i+1, and if the match there is
longer it emits byte i as a literal and takes the better match. That is lazy
matching, and it is now here too. On three real files compressed by both
versions it saved 2 to 3.3 points of gzip's size: ember's DOCS.md went from
104.6% to 101.3%, its vm.rs from 105.4% to 102.7%. It costs about half as much
time again, because most positions are now searched twice.

Measuring that took fixing the measurement first. The comparison had stored the
file name "t.bin" in our files and none in gzip's, so six bytes of every row were
a name, not compression, and tiny files looked 30% worse than they were. Compared
like for like, this already matched gzip on most inputs; the only real gap was
ordinary text.

### How deep to search

Each three-byte key remembers a number of the most recent places it occurred,
and only those are tried as match starts. gzip -9 follows up to 4,096. This used
16, and the notes guessed that the depth was what was left of the gap. Measured on
three real files, size as a share of gzip -9's and total time:

| depth | ember DOCS.md | ember vm.rs | deflate.py | time |
| --- | --- | --- | --- | --- |
| 16 | 101.4% | 102.6% | 100.8% | 0.75s |
| 64 | 100.3% | 101.2% | 100.4% | 1.29s |
| 256 | 100.2% | 101.1% | 100.4% | 1.62s |
| 1,024 | 100.2% | 100.9% | 100.3% | 2.59s |

Sixty-four keeps nearly all of the gain for 1.7 times the time; past that, each
step costs more time for a tenth of a percent, so it is 64 now. And even at
1,024 the gap does not close, which says the rest is not depth at all. The likely
cause is block splitting: gzip ends a block and starts a new one, with new code
tables, when the statistics of the text change, where this writes one block with
one set of tables for the whole file. That is the next thing to measure, not a
finding.

## The check is the whole point

Every test of the writer writes a file and then asks somebody else to read
it: Python's `gzip` module, `zlib` with a raw window, and the `gzip` program
itself. A compressor that agreed with my own decompressor would only prove the
two share my misunderstanding, which is the easiest way to be confidently
wrong about a file format.

So when the reading half arrived, the rule was turned around rather than
dropped: `inflate` is tested only on streams zlib wrote, never on this file's
own output. Every input is compressed at all ten levels with each of zlib's
four strategies, which between them produce stored, fixed and dynamic blocks,
run-length-only matching and Huffman-only coding, and all of it must come back
byte for byte.

## Reading is where the trust boundary is

A compressor only ever sees its own data. A decompressor reads whatever it is
handed, so the reader checks what the writer never had to: a block type of 3,
a stored length that disagrees with its complement, a table longer than the
format allows, a run of lengths spilling past the tables, a set of code lengths
that would need more codes than exist, a match reaching back before the first
byte, and a trailer whose CRC or length does not match. Each has a hand-built
broken stream in the test, and 300 randomly bit-flipped real streams must fail
with a `ValueError` and nothing else, never an index error from deep inside.

Two things about reading surprised me. A match may overlap the bytes it is
writing: distance 1, length 258 means "repeat the last byte 258 times", so the
copy has to go one byte at a time, and copying the slice in one step reads
bytes that do not exist yet. Breaking it that way made the "one long run" case
fail at once. And the stream does not say how long it is; the reader only
knows it is done after the end-of-block code of the block marked final, which
is why `inflate` returns where it stopped, so the gzip trailer can be found.

A gzip file may be several members concatenated: `cat a.gz b.gz > both.gz` is
a valid file, and so is what a log rotator appends to over time. `gunzip` reads
them all and joins the parts, checking each member's own CRC and length, so a
corrupt first member is caught even when the last one is fine. Anything after
the last member that is not another member is refused with its offset, rather
than silently dropped.
