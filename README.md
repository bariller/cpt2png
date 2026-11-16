# cpt2png

Convert **Corel PHOTO-PAINT** images (`.cpt`) to **PNG**, losslessly.

The CPT format is proprietary and was never documented by Corel. Few current programs
can open it, and images made with old versions of PHOTO-PAINT are at risk of becoming
unreadable. `cpt2png` decodes these files on its own, based on an analysis of the format
(see [FORMAT.md](FORMAT.md)): it needs neither Corel software nor Windows.

## What it does

| Source file | Result |
| --- | --- |
| 1-bit black and white image | `name.png` in 1 bit, same resolution (dpi) |
| Grayscale or 256-color image | `name.png` in 8 bits, with its palette |
| 24-bit color image | `name.png` in color |
| Image with objects (layers) | `name.png`: the composited image as PHOTO-PAINT displays it<br>`name_layers/`: the background and each object as a transparent PNG, at page size, ready to edit in GIMP, Photoshop, Krita… |
| Saved selection mask | `masks/name_mask.png` (empty and "select all" masks are skipped) |

Original files are never modified. By default, an existing PNG is not replaced (use
`--overwrite` to force it).

### Supported versions

| Version | Signature | Support |
| --- | --- | --- |
| PHOTO-PAINT 6 | TIFF (`II*`) | yes: background, objects, masks |
| PHOTO-PAINT 7 and 8 | `CPT7FILE` | yes: 1 bit, 8-bit grayscale, 256 colors, 24-bit RGB, objects, masks |
| PHOTO-PAINT 8 (variant), 9 and later | `CPT8FILE`, `CPT9FILE` | no: the file is reported and skipped |

The program has been checked against real PHOTO-PAINT 6 and 7 files: black and white,
24-bit color, palette, and compositions of several objects with transparency.

## Installation

Requires **Python 3.9 or later**.

```sh
git clone https://github.com/bariller/cpt2png.git
cd cpt2png
python3 -m pip install .
```

The `cpt2png` command is then available. Without installing, you can also run it from
the project folder, after installing the two dependencies
(`python3 -m pip install numpy Pillow`):

```sh
python3 -m cpt2png ...
```

## Usage

```sh
# Convert one file: the PNG is created next to the original
cpt2png drawing.cpt

# Convert a whole folder into an output folder
cpt2png ~/Pictures/old -o ~/Pictures/converted

# Also process subfolders (the folder tree is recreated in the output)
cpt2png -r ~/Archives -o ~/Archives-PNG

# Redo a conversion, without exporting the layers
cpt2png drawing.cpt --overwrite --no-layers
```

| Option | Effect |
| --- | --- |
| `-o FOLDER`, `--output FOLDER` | output folder (default: next to each source file) |
| `-r`, `--recursive` | also process subfolders |
| `--overwrite` | replace existing PNG files |
| `--no-layers` | only produce the composited image |
| `--no-masks` | do not export selection masks |
| `--version` | show the version |

The program prints one line per file (size, resolution, image type, number of layers)
and a summary. It exits with code 1 if at least one file could not be converted.

### From Python

```python
from cpt2png import read_cpt, write

doc = read_cpt('drawing.cpt')
print(doc.description())         # e.g. "2480×3508, 300 dpi, 24-bit color"
image = doc.flatten()            # numpy RGB array of the composited image
write(doc, 'output', 'drawing')  # same files as the command line
```

## Known limitations

- CPT8 and CPT9 formats (PHOTO-PAINT 8 in some cases, 9 and later) are not supported.
- CMYK, Lab, 48-bit RGB and 16-bit grayscale color models are not supported (no sample
  files available).
- Objects are composited in "normal" mode. Other blend modes, lenses and effects are not
  reproduced.
- The preview thumbnail stored in the file is ignored.

If a file fails to convert, please open an issue with the error message. Attach the file
if you can share it.

## Tests

The tests build synthetic CPT files; they do not depend on any real image.

```sh
python3 -m unittest
```

## See also

For **CorelDRAW** vector drawings (`.cdr`), [Inkscape](https://inkscape.org) can open
them (through the libcdr library) and export them to SVG or PDF.

## References

- [CPT (file format), Wikipedia](https://en.wikipedia.org/wiki/CPT_(file_format))
- [PRONOM x-fmt/144, The National Archives](https://pronom.nationalarchives.gov.uk/x-fmt/144)
- [Corel Photo-Paint's CPT File Format, jpchacha](https://www.jpchacha.com/blog/view.php?id=8)
- [CPT file format specification, draft 0.01 (2011, archived)](http://web.archive.org/web/20220715204225/http://blog.argasinski.eu/wp-content/uploads/2011/08/cpt-specification-0.01.pdf):
  header and versions, without image compression
- [CPT file detection in `file(1)`](https://mailman.astron.com/pipermail/file/2020-June/000377.html)

## License

[MIT](LICENSE). Corel and PHOTO-PAINT are trademarks of Corel Corporation. This project
is not affiliated with Corel.
