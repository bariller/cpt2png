"""Low-level decoder for Corel PHOTO-PAINT 7/8 images ("CPT7FILE" signature).

The format is undocumented; this decoder comes from reverse engineering.
See FORMAT.md at the root of the repository for the full description.

Summary:
  * the file holds a list of blocks (background, objects, masks, thumbnail);
  * each block is a grid of tiles, stored row by row, pixel rows bottom-up;
  * 24-bit pixels are stored B, G, R (Windows DIB order), palettes too;
  * each tile starts with u16 compression + u16 predictor:
      1 = raw, 2 = GIF-style LZW, 4 = solid fill, 5 = variable-length deltas.
    A tile of 64 KiB or more without a known header is raw.
"""
import struct

import numpy as np

# Block types
BLOCK_BACKGROUND = 1
BLOCK_SELECTION = 2
BLOCK_TRANSPARENCY = 4
BLOCK_OBJECT = 8
BLOCK_THUMBNAIL = 16

# Tile compression types
COMP_RAW, COMP_LZW, COMP_SOLID, COMP_DELTA = 1, 2, 4, 5
PREDICTOR_DELTA = 3


class Block:
    """Header of one image block."""

    __slots__ = ('off', 'w', 'h', 'tw', 'th', 'bpp', 'type', 'flag', 'hlen')

    def __repr__(self):
        return 'Block(type=%d, %dx%d, %d bpp, tiles %dx%d, @0x%x)' % (
            self.type, self.w, self.h, self.bpp, self.tw, self.th, self.off)


def lzw_decode(data, min_bits=8):
    """GIF-style LZW, codes read LSB-first, with an "early change" of code width:
    the width grows as soon as len(table) + 1 >= 2**width (max 12 bits)."""
    clear = 1 << min_bits
    eoi = clear + 1
    width = min_bits + 1
    table = [bytes([i]) for i in range(clear)] + [b'', b'']
    out = bytearray()
    prev = None
    bitbuf = nbits = pos = 0
    n = len(data)
    while True:
        while nbits < width:
            if pos >= n:
                return bytes(out)
            bitbuf |= data[pos] << nbits
            pos += 1
            nbits += 8
        code = bitbuf & ((1 << width) - 1)
        bitbuf >>= width
        nbits -= width
        if code == clear:
            del table[clear + 2:]
            width = min_bits + 1
            prev = None
            continue
        if code == eoi:
            return bytes(out)
        if prev is None:
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            if len(table) < 4096:
                table.append(prev + entry[:1])
        elif code == len(table):
            entry = prev + prev[:1]
            table.append(entry)
        else:
            raise ValueError('invalid LZW code %d' % code)
        out += entry
        prev = entry
        if len(table) + 1 >= (1 << width) and width < 12:
            width += 1


def delta_tokens(d, total):
    """Decode the type 5 bit stream into `total` signed deltas.

    Continuous LSB-first bit stream (tokens are not byte-aligned). Each token is
    an 8-bit control t: mode m = t >> 5, count n = (t & 31) + 1.
    m == 0: n zero deltas; m > 0: n deltas of m + 1 bits each, two's complement.
    """
    buf = bytes(d) + b'\0' * 8
    nbits = len(d) * 8
    bp = i = 0
    t_start, t_n, t_b, t_i = [], [], [], []
    while bp + 8 <= nbits and i < total:
        k, sh = bp >> 3, bp & 7
        t = ((buf[k] | (buf[k + 1] << 8)) >> sh) & 255
        bp += 8
        m, n = t >> 5, (t & 31) + 1
        if m:
            b = m + 1
            t_start.append(bp)
            t_n.append(n)
            t_b.append(b)
            t_i.append(i)
            bp += n * b
        i += n
    out = np.zeros(max(total, i) + 1, dtype=np.int64)
    if t_n:
        t_n = np.array(t_n)
        t_b = np.array(t_b)
        tok = np.repeat(np.arange(len(t_n)), t_n)          # token index of each sample
        k = np.arange(t_n.sum()) - (np.cumsum(t_n) - t_n)[tok]  # rank inside its token
        b = t_b[tok]
        start = np.array(t_start)[tok] + k * b
        arr = np.frombuffer(buf, np.uint8).astype(np.int64)
        byte = start >> 3
        win = arr[byte] | (arr[byte + 1] << 8)
        x = (win >> (start & 7)) & ((1 << b) - 1)
        out[np.array(t_i)[tok] + k] = x - ((x & (1 << (b - 1))) << 1)
    return out[:total]


def unpredict(deltas, chans):
    """Undo predictor 3: the first row adds the previous byte of the same channel,
    the other rows add the byte above. Everything modulo 256."""
    d = deltas.astype(np.int64)
    d[0] = np.cumsum(d[0].reshape(-1, chans), axis=0).reshape(-1)
    return (np.cumsum(d, axis=0) & 255).astype(np.uint8)


def delta_decode(d, stride, rows, chans):
    return unpredict(delta_tokens(d, stride * rows).reshape(rows, stride), chans)


def find_tile_table(f, start, end):
    """The tile table is a run of (u32 offset, u32 size) pairs located somewhere
    after the block header; the first offset points right after the table and
    each tile follows the previous one. Return (position, [(offset, size), ...])."""
    end = min(end, len(f) - 8)
    for p in range(start, end):
        o = struct.unpack_from('<I', f, p)[0]
        if o > p and (o - p) % 8 == 0:
            n = (o - p) // 8
            if n > 100000:
                continue
            cur = o
            tiles = []
            for k in range(n):
                to, ts = struct.unpack_from('<II', f, p + 8 * k)
                if to != cur:
                    break
                tiles.append((to, ts))
                cur = to + ts
            else:
                return p, tiles
    raise ValueError('tile table not found')


def read_blocks(f):
    """Return the list of blocks of a CPT7 file (bytes)."""
    if f[:8] != b'CPT7FILE':
        raise ValueError('not a CPT7 file')
    count = struct.unpack_from('<I', f, 0x28)[0]
    pal_len = struct.unpack_from('<I', f, 0x0c)[0]
    base = struct.unpack_from('<I', f, 0x34)[0] or (0x13c + pal_len)
    blocks = []
    for i in range(count):
        b = Block()
        b.off = struct.unpack_from('<I', f, base + 8 * i)[0]
        (b.w, b.h, b.tw, b.th, b.bpp, _, b.type, b.flag, b.hlen) = struct.unpack_from('<9I', f, b.off)
        blocks.append(b)
    return blocks


def decode_block(f, b):
    """Pixels of a block, top-down: (h, w) uint8 for 1-bit (0/255) and 8-bit,
    (h, w, 3) RGB uint8 for 24-bit."""
    _, tiles = find_tile_table(f, b.off, b.off + 0x800)
    w, h, tw, th, bpp = b.w, b.h, b.tw, b.th, b.bpp
    cols, rows = -(-w // tw), -(-h // th)
    if len(tiles) != cols * rows:
        raise ValueError('expected %d tiles, found %d' % (cols * rows, len(tiles)))
    if bpp == 1:
        stride, chans = tw // 8, 1
    else:
        chans = bpp // 8
        stride = tw * chans
    need = stride * th
    canvas = np.zeros((rows * th, cols * stride), np.uint8)
    for k, (to, ts) in enumerate(tiles):
        d = f[to:to + ts]
        ctype, pred = struct.unpack_from('<HH', d, 0) if ts >= 4 else (-1, 0)
        if ts >= 65536 or (ts >= need and ctype not in (COMP_RAW, COMP_LZW, COMP_SOLID, COMP_DELTA)):
            raw = np.frombuffer(d[:need], np.uint8)
        elif ctype == COMP_RAW:
            raw = np.frombuffer(d[4:4 + need], np.uint8)
        elif ctype == COMP_LZW:
            r = lzw_decode(d[4:])
            raw = np.frombuffer(r[:need] + b'\0' * max(0, need - len(r)), np.uint8)
            if pred == PREDICTOR_DELTA:
                raw = unpredict(raw.reshape(th, stride), chans)
        elif ctype == COMP_SOLID:
            raw = np.tile(np.frombuffer(d[12:12 + chans], np.uint8), need // chans)
        elif ctype == COMP_DELTA:
            raw = delta_decode(d[4:], stride, th, chans)
        else:
            raise ValueError('unknown tile compression %d' % ctype)
        r_, c_ = divmod(k, cols)
        canvas[r_ * th:(r_ + 1) * th, c_ * stride:(c_ + 1) * stride] = raw.reshape(th, stride)
    if bpp == 1:
        bits = np.unpackbits(canvas, axis=1)
        return (bits[:h, :w][::-1] * 255).astype(np.uint8)
    img = canvas.reshape(rows * th, cols * tw, chans)[:h, :w][::-1]
    if chans == 1:
        return img[:, :, 0]
    if chans == 3:
        return img[:, :, ::-1].copy()  # BGR -> RGB
    return img


def dpi(f):
    """Resolution in dots per inch (stored in micro-dots per millimetre)."""
    return round(struct.unpack_from('<I', f, 0x18)[0] * 25.4 / 1e6)


def palette(f):
    """RGB palette as an (n, 3) uint8 array, or None."""
    pal_len = struct.unpack_from('<I', f, 0x0c)[0]
    if not pal_len:
        return None
    return np.frombuffer(f[0x13c:0x13c + pal_len], np.uint8).reshape(-1, 3)[:, ::-1].copy()
