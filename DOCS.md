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
to be flattened: cap the deep ones, then lengthen some short ones until the
Kraft sum is back to one, which is the arithmetic statement of "the tree
closes up". Doing this optimally is the package-merge algorithm; this does
the cheap fix, which costs a few bits on rare inputs. That is the one real
corner cut here.

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

What is left is search depth. This keeps 16 recent positions per three-byte
key; gzip -9 follows chains up to 4,096 long. Deeper chains would find a few
more matches on text, at the price of Python being slow enough already.

## The check is the whole point

Nothing in this repo decompresses anything. Every test writes a file and then
asks somebody else to read it: Python's `gzip` module, `zlib` with a raw
window, and the `gzip` program itself. A compressor that agreed with my own
decompressor would only prove the two share my misunderstanding, which is the
easiest way to be confidently wrong about a file format.
