"""Split a picture of several numbered products (a grid with a label under each one)
into one picture per product."""
from __future__ import annotations

import io

import numpy as np
from PIL import Image
from scipy import ndimage

MIN_SIDE = 640


def _load(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.convert("RGB")


def find_tiles(data: bytes, count: int) -> list[list[int]]:
    """One box (left, top, right, bottom) per product, in reading order (rows top to bottom,
    left to right). Empty when the picture does not have exactly one label per product."""
    try:
        img = _load(data)
    except Exception:
        return []
    a = np.asarray(img).astype(int)
    h, w, _ = a.shape
    white = a.min(axis=2) > 235
    lab, _ = ndimage.label(ndimage.binary_closing(white, iterations=2))
    labels = []
    for i, (ys, xs) in enumerate(ndimage.find_objects(lab), 1):
        bh, bw = ys.stop - ys.start, xs.stop - xs.start
        fill = (lab[ys, xs] == i).mean()
        if w * 0.1 < bw < w * 0.5 and bw / bh > 2.5 and fill > 0.6:
            labels.append((ys.start, xs.start, ys.stop, xs.stop))
    if len(labels) > count:  # keep the labels that share the common size
        widths = sorted(l[3] - l[1] for l in labels)
        med = widths[len(widths) // 2]
        labels = [l for l in labels if abs((l[3] - l[1]) - med) <= med * 0.1]
    if len(labels) != count:
        return []

    rows: list[list[tuple]] = []
    for lab_box in sorted(labels):
        if rows and abs(rows[-1][0][0] - lab_box[0]) < h * 0.03:
            rows[-1].append(lab_box)
        else:
            rows.append([lab_box])
    boxes, ceiling = [], 0
    for row in rows:
        row_top = min(l[0] for l in row)
        for top, left, _bottom, right in sorted(row, key=lambda l: l[1]):
            side = right - left
            upper = max(ceiling, top - int(side * 1.4))
            boxes.append([left, upper, right, top - 4])
        ceiling = max(l[2] for l in row) + 4
    return boxes


def crop_tile(data: bytes, box: list[int]) -> bytes | None:
    """The product inside one box, trimmed to the product and centred on a square."""
    try:
        img = _load(data)
    except Exception:
        return None
    tile = img.crop(tuple(box))
    a = np.asarray(tile).astype(int)
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    bg = np.median(edge, axis=0)
    ys, xs = np.nonzero(np.abs(a - bg).sum(axis=2) > 60)
    if len(xs):
        tile = tile.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    side = int(max(tile.size) * 1.12)
    square = Image.new("RGB", (side, side), tuple(int(c) for c in bg))
    square.paste(tile, ((side - tile.width) // 2, (side - tile.height) // 2))
    if side < MIN_SIDE:
        square = square.resize((MIN_SIDE, MIN_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    square.save(buf, "JPEG", quality=90)
    return buf.getvalue()
