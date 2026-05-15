from __future__ import annotations

from PIL import Image
import logging

logger = logging.getLogger("spectra_uploader.dithering")

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

    rgb = quant.convert("RGB")

    # Diagnostics only: sample-check palette compliance.
    # This does not modify the image and is intentionally low-cost.
    try:
        violations = _sample_palette_violations(rgb)
        if violations:
            logger.warning(
                "Dither produced non-palette RGB samples (showing up to 10): %s",
                violations[:10],
            )
        else:
            logger.debug("Dither palette sample-check: OK")
    except Exception:
        logger.exception("Palette sample-check failed")

    return rgb


def _sample_palette_violations(img: Image.Image, step: int = 64):
    allowed = set(SPECTRA6_RGB)
    w, h = img.size
    px = img.load()
    bad = []
    for y in range(0, h, step):
        for x in range(0, w, step):
            c = px[x, y]
            if c not in allowed:
                bad.append((x, y, c))
                if len(bad) >= 50:
                    return bad
    return bad


def dither_to_spectra6_palette(img: Image.Image) -> Image.Image:
    """Compatibility wrapper used by server.py."""
    return quantize_dither_floyd_steinberg(img)