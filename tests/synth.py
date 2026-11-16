"""Minimal CPT7 writer, only used to build synthetic test files."""
import struct

import numpy as np


class BitWriter:
    """Bit stream, least significant bit first."""

    def __init__(self):
        self.acc = 0
        self.n = 0

    def write(self, value, nbits):
        self.acc |= (int(value) & ((1 << nbits) - 1)) << self.n
        self.n += nbits

    def getvalue(self):
        return self.acc.to_bytes((self.n + 7) // 8, 'little')


def lzw_literal(data):
    """GIF-style LZW that only emits literals; follows the decoder's code width
    rule ("early change") and resets the table before it reaches 4096 entries."""
    bw = BitWriter()
    width, tlen, first = 9, 258, True
    bw.write(256, width)
    for byte in data:
        bw.write(byte, width)
        if not first and tlen < 4096:
            tlen += 1
        first = False
        if tlen + 1 >= (1 << width) and width < 12:
            width += 1
        if tlen >= 4000:
            bw.write(256, width)
            width, tlen, first = 9, 258, True
    bw.write(257, width)
    return bw.getvalue()


def _bits_needed(v):
    v = int(v)
    return (v.bit_length() if v >= 0 else (-v - 1).bit_length()) + 1


def delta_encode(tile, chans):
    """Type 5 compression with predictor 3 of one tile (rows, bytes per row)."""
    x = tile.astype(np.int64)
    d = np.empty_like(x)
    d[1:] = x[1:] - x[:-1]
    row0 = x[0].reshape(-1, chans)
    r0 = row0.copy()
    r0[1:] = row0[1:] - row0[:-1]
    d[0] = r0.reshape(-1)
    flat = (((d + 128) % 256) - 128).reshape(-1)
    bw = BitWriter()
    i = 0
    while i < len(flat):
        n = 1 + (i * 7) % 32  # varied token lengths
        chunk = flat[i:i + n]
        if not chunk.any():
            bw.write(len(chunk) - 1, 8)
        else:
            b = max(2, max(_bits_needed(v) for v in chunk))
            bw.write(((b - 1) << 5) | (len(chunk) - 1), 8)
            for v in chunk:
                bw.write(v, b)
        i += len(chunk)
    return bw.getvalue()


def tile_bytes(kind, raw, chans=1):
    """kind: 'raw', 'lzw', 'delta' or 'solid'; raw: array (rows, bytes per row)."""
    if kind == 'raw':
        return struct.pack('<HH', 1, 0) + raw.tobytes()
    if kind == 'lzw':
        return struct.pack('<HH', 2, 0) + lzw_literal(raw.tobytes())
    if kind == 'delta':
        return struct.pack('<HH', 5, 3) + delta_encode(raw, chans)
    if kind == 'solid':
        return struct.pack('<HH', 4, 0) + b'\0' * 8 + raw.reshape(-1)[:chans].tobytes()
    raise ValueError(kind)


def make_block(img, bpp, tw, th, kinds, block_type=1, ints=None, name=b''):
    """img: (h, w) or (h, w, 3) RGB, top-down; for 1 bit, values 0/1 (1 = white)."""
    h, w = img.shape[:2]
    cols, rows = -(-w // tw), -(-h // th)
    flipped = img[::-1]
    if bpp == 1:
        canvas = np.zeros((rows * th, cols * tw), np.uint8)
        canvas[:h, :w] = flipped
        canvas = np.packbits(canvas, axis=1)
        chans, stride = 1, tw // 8
    else:
        chans = bpp // 8
        canvas = np.zeros((rows * th, cols * tw, chans), np.uint8)
        canvas[:h, :w] = flipped.reshape(h, w, chans)
        if chans == 3:
            canvas = canvas[:, :, ::-1]  # RGB -> BGR
        canvas = canvas.reshape(rows * th, cols * tw * chans)
        stride = tw * chans
    tiles = []
    for k in range(cols * rows):
        r, c = divmod(k, cols)
        raw = np.ascontiguousarray(canvas[r * th:(r + 1) * th, c * stride:(c + 1) * stride])
        tiles.append(tile_bytes(kinds[k % len(kinds)], raw, chans))
    return dict(w=w, h=h, tw=tw, th=th, bpp=bpp, type=block_type,
                ints=ints or {}, name=name, tiles=tiles)


def build_cpt7(blocks, dpi=300, palette=None, table_in_header=True):
    pal = b'' if palette is None else np.asarray(palette, np.uint8)[:, ::-1].tobytes()
    out = bytearray(0x13c + len(pal))
    out[0:8] = b'CPT7FILE'
    out[8] = 10 if palette is not None else 1
    struct.pack_into('<I', out, 0x0c, len(pal))
    struct.pack_into('<I', out, 0x18, round(dpi * 1e6 / 25.4))
    struct.pack_into('<I', out, 0x28, len(blocks))
    out[0x13c:] = pal
    table = len(out)
    if table_in_header:
        struct.pack_into('<I', out, 0x34, table)
    out += bytes(8 * len(blocks))
    for i, b in enumerate(blocks):
        off = len(out)
        hdr = bytearray(0x100)
        struct.pack_into('<9I', hdr, 0, b['w'], b['h'], b['tw'], b['th'], b['bpp'], 0,
                         b['type'], 0, len(hdr))
        for k, v in b['ints'].items():
            struct.pack_into('<i', hdr, 4 * k, v)
        hdr[0x6c:0x6c + len(b['name'])] = b['name']
        cur = off + len(hdr) + 8 * len(b['tiles'])
        entries = bytearray()
        for t in b['tiles']:
            entries += struct.pack('<II', cur, len(t))
            cur += len(t)
        out += hdr + entries + b''.join(b['tiles'])
        struct.pack_into('<II', out, table + 8 * i, off, 0)
    return bytes(out)
