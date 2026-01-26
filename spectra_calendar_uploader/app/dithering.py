from __future__ import annotations

from PIL import Image

# Spectra6 native palette (MUSS exakt so sein)
SPECTRA6_RGB = [
    (0, 0, 0),         # black
    (0, 0, 255),       # blue
    (41, 204, 20),     # green   (NICHT 0,255,0)
    (255, 0, 0),       # red
    (255, 255, 0),     # yellow
    (255, 255, 255),   # white
]

def quantize_dither_floyd_steinberg(img: Image.Image) -> Image.Image:
    """
...
    """

    pal_img = Image.new("P", (1, 1))
    # palette needs 256*3 entries -> we fill first 6, rest with zeros
    palette = []
    for (r, g, b) in SPECTRA6_RGB:
        palette.extend([r, g, b])
    palette.extend([0, 0, 0] * (256 - len(SPECTRA6_RGB)))
    pal_img.putpalette(palette)

    quant = img.quantize(
        palette=pal_img,
        dither=Image.Dither.FLOYDSTEINBERG,
    )
    return quant.convert("RGB")


def dither_to_spectra6_palette(img: Image.Image) -> Image.Image:
    """Compatibility wrapper used by server.py."""
    return quantize_dither_floyd_steinberg(img)
