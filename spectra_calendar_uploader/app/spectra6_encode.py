from __future__ import annotations

from typing import Dict, Tuple
from PIL import Image

# Spectra6 "native" RGB palette (muss identisch zu dithering.py sein)
# Codes (nibbles) folgen der NeoFrame Konvention:
# 0x0=Black, 0x1=White, 0x2=Yellow, 0x3=Red, 0x5=Blue, 0x6=Green
SPECTRA6_RGB_TO_CODE: Dict[Tuple[int, int, int], int] = {
    (0, 0, 0): 0x0,
    (255, 255, 255): 0x1,
    (255, 255, 0): 0x2,
    (255, 0, 0): 0x3,
    (0, 0, 255): 0x5,
    (41, 204, 20): 0x6,  # correct green
}

CODE_TO_NAME = {
    0x0: "Black",
    0x1: "White",
    0x2: "Yellow",
    0x3: "Red",
    0x5: "Blue",
    0x6: "Green",
}


def _nearest_palette_code(rgb: Tuple[int, int, int]) -> int:
    # Very small helper to avoid hard failures if a pixel slips through.
    # This should NOT happen in strict mode if dithering is correct.
    best = None
    best_d = None
    r, g, b = rgb
    for (pr, pg, pb), code in SPECTRA6_RGB_TO_CODE.items():
        d = (r - pr) * (r - pr) + (g - pg) * (g - pg) + (b - pb) * (b - pb)
        if best_d is None or d < best_d:
            best_d = d
            best = code
    return int(best if best is not None else 0x1)


def rgb_image_to_packed_4bit_codes(img: Image.Image, strict: bool = True) -> bytes:
    """
...
    """
    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    px = img.load()

    out = bytearray()

    for y in range(h):
        x = 0
        while x < w:
            p1 = px[x, y]
            if p1 not in SPECTRA6_RGB_TO_CODE:
                if strict:
                    raise ValueError(
                        f"Pixel nicht in Spectra6-Palette gefunden: p1={p1}. "
                        f"Dithering muss NUR diese RGB-Werte erzeugen: {sorted(SPECTRA6_RGB_TO_CODE.keys())}"
                    )
                c1 = _nearest_palette_code(p1)
            else:
                c1 = SPECTRA6_RGB_TO_CODE[p1]

            if x + 1 < w:
                p2 = px[x + 1, y]
                if p2 in SPECTRA6_RGB_TO_CODE:
                    c2 = SPECTRA6_RGB_TO_CODE[p2]
                else:
                    if strict:
                        raise ValueError(
                            f"Pixel nicht in Spectra6-Palette gefunden: p2={p2}. "
                            f"Dithering muss NUR diese RGB-Werte erzeugen: {sorted(SPECTRA6_RGB_TO_CODE.keys())}"
                        )
                    c2 = _nearest_palette_code(p2)
            else:
                c2 = 0x1  # pad with white if odd width

            out.append(((c1 & 0x0F) << 4) | (c2 & 0x0F))
            x += 2

    return bytes(out)


def rgb_to_spectra6_codes_packed_4bit(img: Image.Image, strict: bool = True) -> bytes:
    """Compatibility wrapper used by server.py."""
    return rgb_image_to_packed_4bit_codes(img, strict=strict)
