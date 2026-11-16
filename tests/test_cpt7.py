import contextlib
import io
import os
import tempfile
import unittest

import numpy as np
from PIL import Image

from cpt2png import cpt7
from cpt2png.convert import main, read_cpt

from . import synth

RNG = np.random.default_rng(1234)


def run(*args):
    """Run the command line silently; return its exit code."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return main(list(args))


def write_tmp(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


class TestCodecs(unittest.TestCase):

    def test_lzw(self):
        data = RNG.integers(0, 256, 9000, dtype=np.uint8).tobytes()  # several table resets
        self.assertEqual(cpt7.lzw_decode(synth.lzw_literal(data)), data)

    def test_lzw_repeated_string(self):
        # hand-written stream: "a", then code 258 (= "aa"), which does not exist yet
        bw = synth.BitWriter()
        for code in (256, 97, 258, 257):
            bw.write(code, 9)
        self.assertEqual(cpt7.lzw_decode(bw.getvalue()), b'aaa')

    def test_delta(self):
        for chans in (1, 3):
            tile = RNG.integers(0, 256, (7, 12 * chans), dtype=np.uint8)
            tile[2] = tile[1]  # a row of zero deltas
            d = synth.delta_encode(tile, chans)
            np.testing.assert_array_equal(cpt7.delta_decode(d, tile.shape[1], tile.shape[0], chans), tile)


class TestCpt7(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_rgb_all_compressions(self):
        img = RNG.integers(0, 256, (5, 7, 3), dtype=np.uint8)
        img[2:5, 0:4] = (200, 30, 90)  # tile 0 (bottom left) is a solid color
        block = synth.make_block(img, 24, 4, 3, ['solid', 'raw', 'delta', 'lzw'])
        doc = read_cpt(write_tmp(self.dir, 'rgb.cpt', synth.build_cpt7([block], dpi=200)))
        self.assertEqual((doc.mode, doc.dpi, doc.size), ('RGB', 200, (7, 5)))
        np.testing.assert_array_equal(doc.background, img)

    def test_bilevel(self):
        img = RNG.integers(0, 2, (5, 20), dtype=np.uint8)
        block = synth.make_block(img, 1, 16, 4, ['raw', 'lzw'])
        doc = read_cpt(write_tmp(self.dir, 'bw.cpt', synth.build_cpt7([block], dpi=600)))
        self.assertEqual((doc.mode, doc.dpi), ('1', 600))
        np.testing.assert_array_equal(doc.background, img.astype(bool))

    def test_grayscale_lzw(self):
        img = RNG.integers(0, 256, (70, 70), dtype=np.uint8)
        block = synth.make_block(img, 8, 64, 64, ['lzw', 'delta', 'raw'])
        doc = read_cpt(write_tmp(self.dir, 'gray.cpt', synth.build_cpt7([block])))
        self.assertEqual(doc.mode, 'L')
        np.testing.assert_array_equal(doc.background, img)

    def test_palette(self):
        pal = RNG.integers(0, 256, (256, 3), dtype=np.uint8)
        img = RNG.integers(0, 256, (9, 11), dtype=np.uint8)
        block = synth.make_block(img, 8, 8, 8, ['raw'])
        data = synth.build_cpt7([block], palette=pal, table_in_header=False)
        doc = read_cpt(write_tmp(self.dir, 'pal.cpt', data))
        self.assertEqual(doc.mode, 'P')
        np.testing.assert_array_equal(doc.palette, pal)
        np.testing.assert_array_equal(doc.background_rgb(), pal[img])

    def _layered_file(self):
        H, W = 5, 6
        bg = np.zeros((H, W, 3), np.uint8)
        bg[:] = (10, 20, 30)
        obj = np.zeros((2, 2, 3), np.uint8)
        obj[:] = (200, 100, 50)
        mask = np.array([[255, 255], [255, 0]], np.uint8)
        x0, y0 = 1, 1  # top-left corner of the object, top-down
        top = H - 1 - y0
        ints = {17: x0, 18: top, 19: x0 + 1, 20: top - 1, 25: 50}
        selection = np.zeros((H, W), np.uint8)
        selection[:2, :3] = 255
        blocks = [synth.make_block(bg, 24, 6, 5, ['solid']),
                  synth.make_block(obj, 24, 2, 2, ['raw'], block_type=8, ints=ints, name=b'Test layer'),
                  synth.make_block(mask, 8, 2, 2, ['raw'], block_type=4),
                  synth.make_block(selection, 8, 8, 8, ['raw'], block_type=2)]
        return synth.build_cpt7(blocks, dpi=300), bg, obj, mask, (x0, y0)

    def test_layers_and_masks(self):
        data, bg, obj, mask, (x0, y0) = self._layered_file()
        doc = read_cpt(write_tmp(self.dir, 'layers.cpt', data))
        self.assertEqual([l[0] for l in doc.layers], ['Test layer'])
        self.assertEqual(len(doc.useful_masks()), 1)
        expected = bg.astype(np.float64)
        a = (mask / 255 * 0.5)[..., None]
        expected[y0:y0 + 2, x0:x0 + 2] = obj * a + expected[y0:y0 + 2, x0:x0 + 2] * (1 - a)
        np.testing.assert_array_equal(doc.flatten(), (expected + 0.5).astype(np.uint8))
        rgba = doc.layer_rgba(0)
        self.assertEqual(rgba[y0, x0, 3], 128)
        self.assertEqual(rgba[y0 + 1, x0 + 1, 3], 0)
        self.assertEqual(rgba[0, 0, 3], 0)

    def test_command_line(self):
        data = self._layered_file()[0]
        src = os.path.join(self.dir, 'src')
        out = os.path.join(self.dir, 'out')
        os.makedirs(src)
        write_tmp(src, 'sample.cpt', data)
        write_tmp(src, 'empty.cpt', b'')
        self.assertEqual(run(src, '-o', out), 1)  # the empty file is reported as a failure
        png = os.path.join(out, 'sample.png')
        with Image.open(png) as im:
            self.assertEqual(im.mode, 'RGB')
            self.assertEqual(round(im.info['dpi'][0]), 300)
        self.assertTrue(os.path.exists(os.path.join(out, 'sample_layers', '00_Background.png')))
        self.assertTrue(os.path.exists(os.path.join(out, 'sample_layers', '01_Test layer.png')))
        self.assertTrue(os.path.exists(os.path.join(out, 'masks', 'sample_mask.png')))
        os.remove(os.path.join(src, 'empty.cpt'))
        mtime = os.path.getmtime(png)
        self.assertEqual(run(src, '-o', out), 0)  # already converted: nothing is rewritten
        self.assertEqual(os.path.getmtime(png), mtime)
        self.assertEqual(run(src, '-o', out, '--overwrite', '--no-layers'), 0)


if __name__ == '__main__':
    unittest.main()
