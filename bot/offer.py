"""Read a deal post written by a person (a channel post or a forwarded message) and pull out
the product name, prices, discount, rating and coupon.

Prices are only reported when the text makes them clear; anything ambiguous is left out
rather than guessed, so a post never shows a wrong discount.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٬٫", "01234567890123456789,.")

CURRENCY = r"(?:ج\s?\.\s?م|جنيه|جنية|جنيها|جنيهاً|جنيهات|ج|EGP|LE|L\.E)(?![ء-ي])"
NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
UNITS = (r"\s*(?:%|جيجا|جيجابايت|تيرا|GB|TB|مللي|ملي|مل\b|ml|لتر|بوصة|بوصه|انش|سم|cm|مم|واط|وات|W\b|mAh|"
         r"كيلو|كجم|جرام|جم\b|g\b|حصة|حصص|قطع|قطعة|قطعه|ساعة|ساعات|سنة|سنين|سنه|يوم|أيام|Hz|MP|متر|حصان|فولت|V\b|x\b)")
OLD_WORDS = r"(?:بدلاً من|بدلا من|بدلًا من|بدل|بدال|قبل الخصم|قبل|كان|كانت|السعر الأصلي|الأصلي|الاصلي|was)"
PRICE_WORDS = r"(?:بسعر|سعره|سعرها|السعر|بعد الخصم|ب|بـ|price|now)"
THRESHOLD = re.compile(r"(لما تشتر|عند شراء|عند الشراء|على مشتريات|لو اشتريت|عند اختيار|مشتريات ب)")
USED = re.compile(r"(مستعمل|مستخدم|used|renewed|ريفيربش)", re.I)
PERCENT = re.compile(r"(?:خصم|تخفيض|discount|off)\s*(\d{1,2})\s*[%٪]|(\d{1,2})\s*[%٪]\s*(?:خصم|off|تخفيض)", re.I)
RATING = re.compile(r"(?:⭐|تقييم(?:ه|ها)?|rating)\W{0,3}([1-5](?:[.,]\d)?)(?!\d)", re.I)
COUPON = re.compile(r"(?:كوبون|كود|الكود|الكوبون|coupon|code|promo)(?:\s*الخصم)?\s*[:：]?\s*([A-Z0-9]*[A-Z][A-Z0-9]*)\b")
FROM_TO = re.compile(rf"من\s*({NUMBER})\s*(?:{CURRENCY})?\s*(?:ل|لـ|الى|إلى|ل\s)\s*({NUMBER})")

PROMO = re.compile(r"(متنساش|انضم|اشترك|تابع|القناه|القناة|قناتنا|جروب|شارك|share|join|t\.me/|واتس|whatsapp|"
                   r"لينك الشراء|رابط الشراء|اطلب من هنا|هنا\s*:?$|^هنا|👇)", re.I)
SEPARATOR = re.compile(r"^\s*[—\-_=━─•.~*]{3,}\s*$")
LINK = re.compile(r"(?:https?://|www\.)\S+|\b(?:link\.amazon|amzn\.(?:eu|to)|a\.co)/\S+", re.I)


@dataclass
class Offer:
    title: str = ""
    price: float | None = None
    old_price: float | None = None
    saving: float | None = None
    pct: int | None = None
    rating: float | None = None
    coupon: str | None = None
    extra: list[str] = field(default_factory=list)
    used: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @property
    def score(self) -> float:
        return (self.pct or 0) + 2 * (self.rating or 0) + (5 if self.price else 0)


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def body_lines(text: str) -> list[str]:
    """The post's own lines: footer after a separator, links, promos and emoji-only lines removed."""
    lines = []
    for raw in (text or "").translate(DIGITS).splitlines():
        if SEPARATOR.match(raw):
            break  # everything after a separator is the channel's footer
        line = LINK.sub("", raw).strip(" :|-–\t")
        if not line or PROMO.search(line):
            continue
        if not re.search(r"\w", line):
            continue  # emoji or punctuation only
        lines.append(re.sub(r"\s+", " ", line))
    return lines


def _price_candidates(line: str) -> list[tuple[str, float]]:
    found = []
    for m in re.finditer(NUMBER, line):
        value = _num(m.group(0))
        after = line[m.end():m.end() + 14]
        before = line[max(0, m.start() - 16):m.start()]
        if re.match(UNITS, after, re.I) or value < 5:
            continue
        if re.search(r"(?:خصم|ارخص|أرخص|اغلى|أغلى|الفرق|بفرق|توفير|وفر|زيادة|اكتر|أكتر|اقل من|أقل من)\s*(?:ب|بـ)?\s*$", before):
            continue  # "خصم 50 ج" or "ارخص 20 جنيه" is a difference, not a price
        has_currency = re.match(rf"\s*{CURRENCY}", after) is not None
        is_old = re.search(rf"{OLD_WORDS}\s*$", before) is not None
        priced = re.search(rf"(?:^|\s){PRICE_WORDS}\s*$", before) is not None
        if not (has_currency or is_old or priced):
            continue
        found.append(("old" if is_old else "new", value))
    return found


def _mostly_price(line: str) -> bool:
    rest = re.sub(rf"{NUMBER}|{CURRENCY}|{OLD_WORDS}|{PRICE_WORDS}|خصم|[%٪]", " ", line)
    return len(re.findall(r"[A-Za-zء-ي]{2,}", rest)) < 3


def parse(text: str) -> Offer:
    text = (text or "").translate(DIGITS)
    lines = body_lines(text)
    offer = Offer(used=bool(USED.search(text)))

    m = COUPON.search(text)
    if m:
        offer.coupon = m.group(1)
    m = RATING.search(text)
    if m:
        value = float(m.group(1).replace(",", "."))
        if 1 <= value <= 5:
            offer.rating = value

    explicit_pct = None
    m = PERCENT.search(text)
    if m:
        explicit_pct = int(m.group(1) or m.group(2))

    price_lines: set[int] = set()
    if not offer.used and not THRESHOLD.search(text):
        olds, news = [], []
        for i, line in enumerate(lines):
            ft = FROM_TO.search(line)
            if ft:
                olds.append(_num(ft.group(1)))
                news.append(_num(ft.group(2)))
                price_lines.add(i)
                continue
            cands = _price_candidates(line)
            if cands:
                price_lines.add(i)
            for role, value in cands:
                (olds if role == "old" else news).append(value)
        ambiguous = len(set(news)) > 3 or any(
            sum(1 for role, _ in _price_candidates(l) if role == "new") > 1
            for l in lines if not FROM_TO.search(l))
        if ambiguous:
            olds, news, price_lines = [], [], set()
        if news:
            offer.price = news[0]
        if olds and news and olds[0] > news[0]:
            pct = round((olds[0] - news[0]) / olds[0] * 100)
            if 1 <= pct <= 95:
                offer.old_price = olds[0]
                offer.saving = round(olds[0] - news[0], 2)
                offer.pct = pct
    if offer.pct is None and explicit_pct and 1 <= explicit_pct <= 95 and not THRESHOLD.search(text):
        offer.pct = explicit_pct

    # the product name: the first line that is more than just a price
    title_index = None
    for i, line in enumerate(lines):
        if len(line) < 6 or PERCENT.fullmatch(line.strip()):
            continue
        if i in price_lines and _mostly_price(line):
            continue
        title_index = i
        break
    if title_index is not None:
        offer.title = lines[title_index][:160]

    for i, line in enumerate(lines):
        if i == title_index or (i in price_lines and offer.price is not None):
            continue
        if offer.pct is not None and PERCENT.search(line) and len(line) < 25:
            continue  # "خصم 40%" is already in our discount line
        if offer.coupon and (offer.coupon in line or re.search(r"كوبون|كود|coupon|code", line, re.I)) \
                and len(line) < 40:
            continue
        if offer.rating and RATING.search(line) and len(line) < 30:
            continue
        offer.extra.append(line[:140])
    offer.extra = offer.extra[:3]
    return offer
