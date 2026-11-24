"""Pictures taken from other channels, cleaned up before we post them."""
from __future__ import annotations

import io

from PIL import Image, ImageChops

WHITE = 235        # a pixel is "white" when red, green and blue are all above this
MIN_WIDTH = 640    # small crops are enlarged so Telegram shows them at a good size


def crop_card(data: bytes) -> bytes | None:
    """Some channels put the product photo on a white card inside their own banner, with their
    logo, price and text beside it. Return only the product card, or None when there is none."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    img = img.convert("RGB")
    w, h = img.size
    r, g, b = img.point(lambda v: 255 if v > WHITE else 0).split()
    mask = ImageChops.multiply(ImageChops.multiply(r, g), b)

    cols = list(mask.resize((w, 1), Image.BOX).tobytes())
    xs = [x for x in range(int(w * 0.4), w) if cols[x] > 0.5 * 255]
    if not xs:
        return None
    x1, x2 = xs[0], xs[-1]
    rows = list(mask.crop((x1, 0, x2 + 1, h)).resize((1, h), Image.BOX).tobytes())
    ys = [y for y in range(h) if rows[y] > 0.5 * 255]
    if not ys:
        return None
    y1, y2 = ys[0], ys[-1]

    card_w, card_h = x2 - x1 + 1, y2 - y1 + 1
    if card_w < 0.15 * w or card_w > 0.6 * w or card_h < 0.4 * h:
        return None
    inset = round(0.03 * min(card_w, card_h))  # the card is often tilted: stay off its edges
    out = img.crop((x1 + inset, y1 + inset, x2 + 1 - inset, y2 + 1 - inset))
    if out.width < MIN_WIDTH:
        scale = MIN_WIDTH / out.width
        out = out.resize((MIN_WIDTH, round(out.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=92)
    return buf.getvalue()
