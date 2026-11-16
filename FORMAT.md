# The Corel PHOTO-PAINT CPT format: reverse-engineering notes

These notes describe what `cpt2png` can read. They complement the
[unofficial 2011 specification](http://web.archive.org/web/20220715204225/http://blog.argasinski.eu/wp-content/uploads/2011/08/cpt-specification-0.01.pdf),
which describes the header but not how pixels are stored. Everything about tiles and
their compression comes from analyzing real files.

Conventions: little-endian integers, positions in hexadecimal, `u32` = 4-byte unsigned
integer, `i32` = signed.

## 1. Two file families

- **CPT 6.0** (PHOTO-PAINT 6): a multi-page TIFF (see § 7).
- **CPT 7.0** (PHOTO-PAINT 7 and 8): a custom format with the `CPT7FILE` signature
  (§ 2 to 6).
- `CPT8FILE` and `CPT9FILE` (PHOTO-PAINT 8 in some cases, 9 and later): not studied.

## 2. CPT7 header

| Position | Type | Content |
| --- | --- | --- |
| 0x00 | 8 bytes | `CPT7FILE` |
| 0x08 | u8 | color model: 0x01 24-bit RGB, 0x05 8-bit grayscale, 0x06 1-bit black and white, 0x0A 256 colors (palette); also 0x03 CMYK, 0x0B Lab, 0x0C 48-bit RGB, 0x0E 16-bit grayscale |
| 0x0C | u32 | palette length in bytes (0 without a palette, 768 for 256 colors) |
| 0x18 | u32 | resolution in micro-dots per millimeter: dpi = value × 25.4 / 10⁶ |
| 0x28 | u32 | number of blocks |
| 0x34 | u32 | position of the block table; if 0, it directly follows the palette, at 0x13C + palette length |
| 0x13C | | palette: 3 bytes per color, in **B, G, R** order |

The block table holds one (`u32` position, `u32` flags) pair per block.

## 3. Blocks

Each block starts with 9 × `u32`:

| No. | Content |
| --- | --- |
| 0, 1 | width, height (pixels) |
| 2, 3 | tile width, tile height |
| 4 | bits per pixel (1, 8 or 24) |
| 5 | 0 |
| 6 | block type |
| 7 | flags |
| 8 | header length |

| Type | Role |
| --- | --- |
| 1 | background (always the first block) |
| 8 | object |
| 4 | transparency mask of the preceding object (8 bits) |
| 2 | saved selection mask (8 bits, 255 = selected) |
| 16 | preview thumbnail |

Observed tile sizes (each fits in 64 KiB): 768 × 682 in 1 bit, 144 × 151 in 24 bits,
256 × 256 in 8 bits.

The background's name lies between positions 0x60 and 0x90 of the block (exact position
not determined).

### Objects (type 8)

Reading the start of the block as a sequence of `i32`:

| No. | Content |
| --- | --- |
| 17, 18, 19, 20 | left, top, right, bottom, in image pixels; the vertical axis starts **from the bottom**: the top row, counted from the top, is height − 1 − [18] |
| 25 | opacity in % |

The object's name is a null-terminated string at position 0x6C of the block
(Windows-1252).

Compositing: objects are stacked in file order, with normal blending, and
alpha = transparency mask / 255 × opacity / 100.

## 4. Tile table

It lies after the block header (cpt2png searches the first 0x800 bytes). It is a
sequence of n (`u32` position, `u32` size) pairs, where:

- the first position points right after the table;
- each tile immediately follows the previous one;
- n = number of tile columns × number of tile rows, stored row by row.

## 5. Pixels

- Assemble the tile mosaic, crop it to width × height, then **flip it vertically**:
  rows are stored bottom-up.
- 24 bits: each pixel is stored in **B, G, R** order (like Windows DIB bitmaps).
- 1 bit: the most significant bit is the leftmost pixel; 1 = white, 0 = black.

## 6. Tile compression

A tile starts with a `u16` (compression) and a `u16` (predictor). Exception: a tile of
65,536 bytes or more without a recognized header is stored raw.

| Code | Method |
| --- | --- |
| 1 | raw: the bytes follow the header |
| 2 | GIF-style LZW |
| 4 | solid fill: a single color, whose bytes are at position 12 of the tile |
| 5 | variable-length deltas |

### LZW (type 2)

- Codes are read least significant bit first, 9 bits wide at the start.
- Code 256: table reset; code 257: end of data.
- After each code is processed, the width grows by one bit as soon as
  table size + 1 ≥ 2^width, up to 12 bits. This is an "early change" variant, one code
  earlier than GIF.

### Deltas (type 5)

The data form a continuous bit stream, read least significant bit first. Tokens are not
byte-aligned. Each token starts with a control byte `t`:

- mode `m = t >> 5`, count `n = (t & 31) + 1`;
- `m = 0`: n zero deltas;
- `m > 0`: n deltas follow, `m + 1` bits each, in two's complement.

The stream yields tile width × bytes per pixel × tile height deltas, then predictor 3
rebuilds the bytes, modulo 256:

- first row: each byte = delta + previous byte **of the same channel** (the first pixel
  equals its delta);
- following rows: each byte = delta + the byte right above it.

## 7. CPT 6.0 (PHOTO-PAINT 6)

A multi-page TIFF: page 0 is the background, the following pages are the objects, their
transparency masks, the selection masks and the thumbnail. Pixels are in the right
order, top-down: the `Orientation` = 4 tag written by the software must be ignored.

Each page carries a private tag **40999**:

| Bytes | Content |
| --- | --- |
| 4–7 | block type (same values as CPT7) |
| 8–23 | for an object: left, top, right, bottom (`i32`, vertical axis from the bottom as in CPT7) |
| 40–43 | for an object: opacity in % |
| 48… | object name, null-terminated string |

## 8. Checks

- 1-bit images: decoding is pixel-identical to the same image extracted from a
  CorelDRAW file where it was embedded.
- Images with objects: the computed composite matches the preview thumbnail stored in
  the file (mean difference of 0 to 4 out of 255 depending on the area).
- Every decoded tile has exactly the expected size, and seams between tiles are
  invisible at full resolution.

## 9. Open questions

- Thumbnails compressed with LZW and predictor 3: decoding produces vertical streaks.
  This has no consequence, since the thumbnail is only a preview; it is ignored.
- Exact position of the background's name.
- Versions 7.01, 8.0 and 9.0, and the CMYK, Lab, 48-bit and 16-bit grayscale models: no
  sample files studied.
