from __future__ import annotations

import numpy as np
from PySide6.QtGui import QImage


def contiguous_mask(image: QImage, x: int, y: int, threshold: int, selection: QImage | None) -> QImage:
    """Four-connected seed-color flood, bounded by the canvas and selection."""
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    height, width = rgba.height(), rgba.width()
    pixels = np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(height, width, 4)
    seed = pixels[y, x].astype(np.int16)
    eligible = np.max(np.abs(pixels.astype(np.int16) - seed), axis=2) <= threshold
    alpha = None
    if selection is not None:
        mask = selection.convertToFormat(QImage.Format.Format_RGBA8888)
        alpha = np.frombuffer(mask.constBits(), dtype=np.uint8).reshape(height, width, 4)[:, :, 3]
        eligible &= alpha > 0
    filled = np.zeros((height, width), dtype=np.uint8)
    pending = [(x, y)]
    while pending:
        px, py = pending.pop()
        if not eligible[py, px]:
            continue
        row = eligible[py]
        left = px
        right = px + 1
        while left > 0 and row[left - 1]:
            left -= 1
        while right < width and row[right]:
            right += 1
        row[left:right] = False
        filled[py, left:right] = 255
        for ny in (py - 1, py + 1):
            if not 0 <= ny < height:
                continue
            segment = eligible[ny, left:right]
            starts = np.flatnonzero(segment & ~np.r_[False, segment[:-1]])
            pending.extend((left + int(offset), ny) for offset in starts)
    if alpha is not None:
        filled = np.minimum(filled, alpha)
    result = np.repeat(filled[:, :, None], 4, axis=2)
    return QImage(result.data, width, height, width * 4, QImage.Format.Format_RGBA8888_Premultiplied).copy()
