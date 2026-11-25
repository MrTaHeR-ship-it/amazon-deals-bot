"""Split a picture of several numbered products (a grid with a label under each one)
into one picture per product."""
from __future__ import annotations

import io
import re

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
    return _square(img.crop(tuple(box)))


def crop_frame(data: bytes) -> bytes | None:
    """A designed picture with the product inside a light frame: just the product.
    None when there is no such frame."""
    try:
        img = _load(data)
    except Exception:
        return None
    a = np.asarray(img).astype(int)
    h, w, _ = a.shape
    bright = a.mean(axis=2) > 205
    lab, _ = ndimage.label(ndimage.binary_closing(bright, iterations=2))
    best = None
    for i, (ys, xs) in enumerate(ndimage.find_objects(lab), 1):
        bh, bw = ys.stop - ys.start, xs.stop - xs.start
        if bw < w * 0.25 or bh < h * 0.25 or bw > w * 0.95 or bh > h * 0.95:
            continue
        if (lab[ys, xs] == i).mean() > 0.3:  # a frame is a thin outline, not a filled shape
            continue
        if not best or bw * bh > best[2] * best[3]:
            best = (xs.start, ys.start, bw, bh)
    if not best:
        return None
    x, y, bw, bh = best
    inset = max(6, round(min(bw, bh) * 0.04))
    return _square(img.crop((x + inset, y + inset, x + bw - inset, y + bh - inset)))


def _square(tile: Image.Image) -> bytes:
    """Trim to the product and centre it on a square of the same background."""
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


# --- matching separate pictures to the products in a list ----------------------------

ARABIC_FOLD = str.maketrans({"ى": "ي", "أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ـ": ""})
WORD = re.compile(r"[0-9A-Za-z" + chr(0x621) + "-" + chr(0x64A) + r"]{2,}")


def words(text: str) -> set[str]:
    return set(WORD.findall((text or "").translate(ARABIC_FOLD).lower()))


def read_text(data: bytes) -> str:
    """The words printed on a picture (Arabic and English). Empty when OCR is not installed."""
    try:
        import pytesseract
        img = _load(data).convert("L")
        w, h = img.size
        parts = []
        for k in range(10):  # line by line works much better than the whole design at once
            top = int(h * k * 0.09)
            band = img.crop((0, top, w, min(h, top + int(h * 0.18))))
            parts.append(pytesseract.image_to_string(band, lang="ara", config="--psm 6"))
        return "\n".join(parts)
    except Exception:
        return ""


def match_pictures(picture_texts: list[str], titles: list[str]) -> list[int]:
    """For each title, the index of the picture whose printed words fit it best. Pictures keep
    their order when the words do not tell them apart."""
    n = len(titles)
    order = list(range(n))
    if len(picture_texts) != n:
        return order
    scores = [[len(words(t) & words(p)) for p in picture_texts] for t in titles]
    pairs = sorted(((scores[i][j], i, j) for i in range(n) for j in range(n)), reverse=True)
    chosen, used_t, used_p = {}, set(), set()
    for score, i, j in pairs:
        if score == 0:
            break
        if i not in used_t and j not in used_p:
            chosen[i] = j
            used_t.add(i)
            used_p.add(j)
    if len(chosen) != n:
        return order
    return [chosen[i] for i in range(n)]
