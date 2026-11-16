"""cpt2png: convert Corel PHOTO-PAINT images (.cpt) to PNG."""
__version__ = '1.0.0'

from .convert import Document, UnsupportedFile, read_cpt, write  # noqa: E402,F401
