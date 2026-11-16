"""Convert Corel PHOTO-PAINT images (.cpt) to PNG.

Supported formats:
  * CPT7 ("CPT7FILE" signature, PHOTO-PAINT 7 and 8);
  * CPT6 (PHOTO-PAINT 6), a multi-page TIFF with a private tag.

For each file, the program writes:
  <name>.png                    the image as displayed by PHOTO-PAINT
  <name>_layers/NN_<object>.png one transparent PNG per object (if the image has any)
  masks/<name>_mask.png         the saved selection mask (if there is one)
"""
import argparse
import io
import os
import re
import struct
import sys

import numpy as np
from PIL import Image

from . import __version__, cpt7

CPT6_TAG = 40999  # private TIFF tag of PHOTO-PAINT 6: object description
TIFF_MAGIC = (b'II*\0', b'MM\0*')
MODES = {'1': '1-bit black and white', 'L': '8-bit grayscale',
         'P': '256 colors', 'RGB': '24-bit color'}


class UnsupportedFile(Exception):
    """The file is not in a supported format."""


class Document:
    """Decoded image: background, objects (layers) and selection masks."""

    def __init__(self, fmt, background, mode, dpi, palette=None, background_name='Background'):
        self.fmt = fmt                          # 'CPT7' or 'CPT6'
        self.mode = mode                        # '1', 'L', 'P' or 'RGB'
        self.background = background            # numpy array, top-down
        self.palette = None
        if palette is not None:                 # padded to 256 colors
            self.palette = np.zeros((256, 3), np.uint8)
            self.palette[:len(palette)] = palette[:256]
        self.dpi = dpi
        self.background_name = background_name
        self.layers = []                        # (name, rgb HxWx3, alpha 0..1 HxW, x0, y0)
        self.masks = []                         # uint8 HxW arrays, 255 = selected

    @property
    def size(self):
        return self.background.shape[1], self.background.shape[0]

    def description(self):
        w, h = self.size
        return '%d×%d, %d dpi, %s' % (w, h, self.dpi, MODES[self.mode])

    def background_rgb(self):
        return to_rgb(self.background, self.mode, self.palette)

    def _placement(self, layer):
        H, W = self.background.shape[:2]
        _, rgb, _, x0, y0 = layer
        ys0, xs0 = max(0, y0), max(0, x0)
        ys1, xs1 = min(H, y0 + rgb.shape[0]), min(W, x0 + rgb.shape[1])
        if ys1 <= ys0 or xs1 <= xs0:
            return None
        return ((slice(ys0, ys1), slice(xs0, xs1)),
                (slice(ys0 - y0, ys1 - y0), slice(xs0 - x0, xs1 - x0)))

    def flatten(self):
        """Composited image (RGB uint8): objects stacked in file order,
        normal blending, alpha = transparency mask × opacity."""
        comp = self.background_rgb().astype(np.float64)
        for layer in self.layers:
            place = self._placement(layer)
            if place is None:
                continue
            dst, src = place
            a = layer[2][src][..., None]
            comp[dst] = layer[1][src] * a + comp[dst] * (1 - a)
        return np.clip(comp + 0.5, 0, 255).astype(np.uint8)

    def layer_rgba(self, k):
        """Object number k on a transparent canvas the size of the image."""
        H, W = self.background.shape[:2]
        layer = self.layers[k]
        rgba = np.zeros((H, W, 4), np.uint8)
        place = self._placement(layer)
        if place is not None:
            dst, src = place
            rgba[dst + (slice(0, 3),)] = layer[1][src]
            rgba[dst + (3,)] = np.clip(layer[2][src] * 255 + 0.5, 0, 255).astype(np.uint8)
        return rgba

    def useful_masks(self):
        """Selection masks, except empty ones and those that select everything."""
        return [m for m in self.masks if 0.0 < (m == 255).mean() < 0.99]


def to_rgb(arr, mode, palette):
    if mode == 'P':
        return palette[arr]
    if mode == '1':
        arr = np.where(arr, 255, 0).astype(np.uint8)
    if arr.ndim == 2:
        return np.dstack([arr] * 3)
    return arr


def to_image(arr, mode, palette=None):
    if mode == '1':
        return Image.fromarray(np.where(arr, 255, 0).astype(np.uint8)).convert('1', dither=Image.Dither.NONE)
    if mode == 'P':
        h, w = arr.shape
        im = Image.frombytes('P', (w, h), np.ascontiguousarray(arr, np.uint8).tobytes())
        im.putpalette(palette.astype(np.uint8).tobytes())
        return im
    return Image.fromarray(np.ascontiguousarray(arr))


def _cstr(b):
    return b.split(b'\0', 1)[0].decode('cp1252', 'replace')


# --------------------------------------------------------------------- reading

def read_cpt7(data):
    blocks = cpt7.read_blocks(data)
    if not blocks:
        raise UnsupportedFile('no image in the file')
    bg_block = blocks[0]
    if bg_block.bpp not in (1, 8, 24):
        raise UnsupportedFile('%d bits per pixel color model not supported' % bg_block.bpp)
    palette = cpt7.palette(data)
    bg = cpt7.decode_block(data, bg_block)
    if bg_block.bpp == 1:
        mode, bg = '1', bg > 127
    elif bg_block.bpp == 8:
        mode = 'P' if palette is not None else 'L'
    else:
        mode = 'RGB'
    m = re.search(rb'\0([A-Za-z\xc0-\xff][^\0]{1,40})\0', data[bg_block.off + 0x60:bg_block.off + 0x90])
    doc = Document('CPT7', bg, mode, cpt7.dpi(data), palette if mode == 'P' else None,
                   m.group(1).decode('cp1252') if m else 'Background')
    H = bg.shape[0]
    i = 1
    while i < len(blocks):
        b = blocks[i]
        if b.type == cpt7.BLOCK_OBJECT:
            if b.bpp not in (1, 8, 24):
                raise UnsupportedFile('%d bits per pixel object not supported' % b.bpp)
            ints = struct.unpack_from('<26i', data, b.off)
            left, top, opacity = ints[17], ints[18], ints[25]
            name = _cstr(data[b.off + 0x6c:b.off + 0x6c + 64]) or 'Object %d' % i
            rgb = to_rgb(cpt7.decode_block(data, b), doc.mode, doc.palette)
            if i + 1 < len(blocks) and blocks[i + 1].type == cpt7.BLOCK_TRANSPARENCY:
                alpha = cpt7.decode_block(data, blocks[i + 1]).astype(np.float64) / 255
                i += 1
            else:
                alpha = np.ones(rgb.shape[:2])
            doc.layers.append((name, rgb, alpha * opacity / 100, left, H - 1 - top))
        elif b.type == cpt7.BLOCK_SELECTION:
            doc.masks.append(cpt7.decode_block(data, b))
        i += 1  # thumbnails (type 16) are ignored
    return doc


def read_cpt6(data):
    with Image.open(io.BytesIO(data)) as im:
        dpi = im.info.get('dpi')
        frames = []
        for k in range(getattr(im, 'n_frames', 1)):
            im.seek(k)
            tag = im.tag_v2.get(CPT6_TAG)
            frames.append((im.copy(), bytes(tag) if tag is not None else b''))
    dpi = round(float(dpi[0])) if dpi else 0

    def block_type(tag):
        return struct.unpack_from('<I', tag, 4)[0] if len(tag) >= 8 else 0

    bg_im = frames[0][0]
    if bg_im.mode not in ('1', 'L', 'P', 'RGB'):
        bg_im = bg_im.convert('RGB')
    palette = None
    if bg_im.mode == 'P':
        palette = np.array(bg_im.getpalette()[:768], np.uint8).reshape(-1, 3)
    doc = Document('CPT6', np.array(bg_im), bg_im.mode, dpi, palette)
    H = doc.background.shape[0]
    k = 1
    while k < len(frames):
        fr, tag = frames[k]
        typ = block_type(tag)
        if typ == cpt7.BLOCK_OBJECT:
            ints = struct.unpack_from('<11i', tag, 0)
            left, top, opacity = ints[2], ints[3], ints[10]
            name = _cstr(tag[48:]) or 'Object %d' % k
            rgb = np.array(fr.convert('RGB'))
            alpha = np.ones(rgb.shape[:2])
            if k + 1 < len(frames) and block_type(frames[k + 1][1]) == cpt7.BLOCK_TRANSPARENCY:
                alpha = np.array(frames[k + 1][0].convert('L')).astype(np.float64) / 255
                k += 1
            doc.layers.append((name, rgb, alpha * opacity / 100, left, H - 1 - top))
        elif typ == cpt7.BLOCK_SELECTION:
            doc.masks.append(np.array(fr.convert('L')))
        k += 1
    return doc


def read_cpt(path):
    """Read a .cpt file and return a Document."""
    with open(path, 'rb') as fh:
        data = fh.read()
    if not data:
        raise UnsupportedFile('empty file (incomplete copy or download?)')
    magic = data[:8]
    if magic == b'CPT7FILE':
        return read_cpt7(data)
    if magic[:4] in TIFF_MAGIC:
        return read_cpt6(data)
    if magic in (b'CPT8FILE', b'CPT9FILE'):
        raise UnsupportedFile('%s format (PHOTO-PAINT 8 or later) not supported'
                              % magic[:4].decode())
    raise UnsupportedFile('not a recognized Corel PHOTO-PAINT file')


# --------------------------------------------------------------------- writing

def _safe(name):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip(' .') or 'layer'


def _save(im, path, dpi):
    if dpi:
        im.save(path, dpi=(dpi, dpi), optimize=True)
    else:
        im.save(path, optimize=True)


def write(doc, out_dir, base, layers=True, masks=True):
    """Write the document's PNG files into out_dir. Return the list of files written."""
    os.makedirs(out_dir, exist_ok=True)
    written = []
    main_png = os.path.join(out_dir, base + '.png')
    if doc.layers:
        _save(Image.fromarray(doc.flatten()), main_png, doc.dpi)
    else:
        _save(to_image(doc.background, doc.mode, doc.palette), main_png, doc.dpi)
    written.append(main_png)
    if layers and doc.layers:
        d = os.path.join(out_dir, base + '_layers')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, '00_%s.png' % _safe(doc.background_name))
        _save(Image.fromarray(doc.background_rgb()), p, doc.dpi)
        written.append(p)
        for k, layer in enumerate(doc.layers):
            p = os.path.join(d, '%02d_%s.png' % (k + 1, _safe(layer[0])))
            _save(Image.fromarray(doc.layer_rgba(k)), p, doc.dpi)
            written.append(p)
    if masks:
        for k, mk in enumerate(doc.useful_masks()):
            d = os.path.join(out_dir, 'masks')
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, '%s_mask%s.png' % (base, '' if k == 0 else k + 1))
            _save(Image.fromarray(mk), p, doc.dpi)
            written.append(p)
    return written


# --------------------------------------------------------------------- command line

def _collect(entries, recursive):
    """Return [(path of the .cpt file, relative subfolder)] and the number of missing entries."""
    def is_cpt(name):
        return name.lower().endswith('.cpt') and not name.startswith('._')

    found, missing = [], 0
    for entry in entries:
        if os.path.isdir(entry):
            if recursive:
                for root, dirs, files in os.walk(entry):
                    dirs.sort()
                    found += [(os.path.join(root, n), os.path.relpath(root, entry))
                              for n in sorted(files) if is_cpt(n)]
            else:
                found += [(os.path.join(entry, n), '.') for n in sorted(os.listdir(entry))
                          if is_cpt(n) and os.path.isfile(os.path.join(entry, n))]
        elif os.path.isfile(entry):
            found.append((entry, '.'))
        else:
            print('%s: not found' % entry, file=sys.stderr)
            missing += 1
    return found, missing


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='cpt2png',
        description='Convert Corel PHOTO-PAINT images (.cpt) to PNG, losslessly.')
    parser.add_argument('inputs', nargs='+', metavar='INPUT',
                        help='.cpt file or folder containing .cpt files')
    parser.add_argument('-o', '--output', metavar='FOLDER',
                        help='output folder (default: next to each source file)')
    parser.add_argument('-r', '--recursive', action='store_true',
                        help='also process subfolders')
    parser.add_argument('--overwrite', action='store_true',
                        help='replace existing PNG files (by default they are kept)')
    parser.add_argument('--no-layers', action='store_true',
                        help='do not export objects as separate layers')
    parser.add_argument('--no-masks', action='store_true',
                        help='do not export selection masks')
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    args = parser.parse_args(argv)

    files, failures = _collect(args.inputs, args.recursive)
    if not files and not failures:
        print('No .cpt file found.', file=sys.stderr)
        return 1
    converted = skipped = 0
    for path, rel in files:
        name = os.path.basename(path)
        base = os.path.splitext(name)[0]
        if args.output:
            out_dir = os.path.normpath(os.path.join(args.output, rel))
        else:
            out_dir = os.path.dirname(path) or '.'
        target = os.path.join(out_dir, base + '.png')
        if os.path.exists(target) and not args.overwrite:
            print('%s: already converted (%s exists, use --overwrite to replace it)' % (name, target))
            skipped += 1
            continue
        try:
            doc = read_cpt(path)
            written = write(doc, out_dir, base, layers=not args.no_layers,
                            masks=not args.no_masks)
        except UnsupportedFile as e:
            print('%s: skipped, %s' % (name, e), file=sys.stderr)
            failures += 1
            continue
        except Exception as e:  # damaged file or unknown variant of the format
            print('%s: ERROR, %s' % (name, e), file=sys.stderr)
            failures += 1
            continue
        extra = []
        if doc.layers:
            extra.append('%d layers' % (len(doc.layers) + 1))
        n_masks = len(doc.useful_masks())
        if n_masks:
            extra.append('%d mask%s' % (n_masks, 's' if n_masks > 1 else ''))
        print('%s → %s  (%s, %s%s)' % (name, written[0], doc.fmt, doc.description(),
                                       ''.join('; ' + e for e in extra)))
        converted += 1
    print('%d converted, %d already present, %d failed.' % (converted, skipped, failures))
    return 1 if failures else 0
