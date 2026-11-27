"""PDF export for GET /reports/{type}/export?format=pdf -- one renderer for every report.

The report builders already describe their table (meta.columns: key, label, type). This
turns that into a readable document:

- column widths from the header and the real content of each column, bounded by per-type
  minimums / maximums, then fitted to the page (never equal slices);
- every cell wraps inside its own box, rows grow to the tallest cell; long numbers without
  spaces break by character; nothing is clipped or drawn over a neighbour;
- portrait when the table fits, landscape when it does not, smaller type only if even
  landscape is too narrow;
- the table header repeats on every page, a row never splits across pages, the footer
  ("Page X of Y") has its own space;
- values are formatted for people: currency "₹ 1,180.00" (Indian grouping), dates
  "26 Nov 2026", statuses "Partially Paid", percentages "12.50%".

Only presentation: rows, their order and their values are exactly what the report returned.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fpdf import FPDF

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "dm-sans"
FAMILY = "DMSansReport"
IST = ZoneInfo("Asia/Kolkata")

MARGIN = 10.0
PAD_X = 1.4
PAD_Y = 1.1
HEADER_FILL = (30, 64, 175)
HEADER_TEXT = (255, 255, 255)
ZEBRA = (245, 247, 251)
BORDER = (203, 213, 225)
TEXT = (17, 24, 39)
MUTED = (75, 85, 99)

# (min mm, max mm) per column type; text columns may grow to the max, then wrap.
WIDTH_LIMITS = {
    "date": (14.0, 24.0),
    "datetime": (22.0, 34.0),
    "number": (9.0, 24.0),
    "percent": (11.0, 22.0),
    "currency": (13.0, 34.0),
    "status": (13.0, 34.0),
    "reference": (16.0, 60.0),
    "text": (18.0, 80.0),
}
WRAPPING = {"text", "reference"}
RIGHT_ALIGNED = {"number", "percent", "currency"}
_ID_KEY = re.compile(r"(^id$|_id$|_ids$)")
_CODE = re.compile(r"^[a-z]{2,20}(_[a-z]+)*$")  # stored codes: upi, adjustment, supplier_invoice
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Words kept as written when a status / label is title-cased.
_UPPER = {"gst", "gstin", "upi", "neft", "rtgs", "imps", "cgst", "sgst", "igst", "hsn", "sku", "cogs", "grn", "po",
          "pl", "p&l", "id", "tds", "itc"}


# ----------------------------------------------------------------------------- formatting

def _indian_group(int_part: str) -> str:
    if len(int_part) <= 3:
        return int_part
    head, tail = int_part[:-3], int_part[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def _number(value: float, decimals: int) -> str:
    neg = value < 0
    text = f"{abs(value):.{decimals}f}"
    int_part, _, frac = text.partition(".")
    out = _indian_group(int_part) + (f".{frac}" if frac else "")
    return f"-{out}" if neg else out


def money(value) -> str:  # noqa: ANN001
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _text(value)
    if v < 0:
        return f"-₹ {_number(-v, 2)}"
    return f"₹ {_number(v, 2)}"


_BUCKET = re.compile(r"^(\d+)_(\d+|plus)$")


def _bucket(text: str) -> str | None:
    """Ageing buckets: 0_30 -> 0–30 days, 90_plus -> 90+ days."""
    hit = _BUCKET.match(str(text).strip())
    if not hit:
        return None
    lo, hi = hit.groups()
    return f"{lo}+ days" if hi == "plus" else f"{lo}–{hi} days"


def label(text: str) -> str:
    text = re.sub(r"(\d+)_(\d+)", r"\1–\2", str(text))
    text = re.sub(r"(\d+)_plus", r"\1+", text)
    words = re.split(r"[_\s]+", str(text).strip())
    return " ".join(w.upper() if w.lower() in _UPPER else w[:1].upper() + w[1:] for w in words if w)


def _parse_dt(value):  # noqa: ANN001, ANN202
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_date(value, with_time: bool = False) -> str:  # noqa: ANN001
    parsed = _parse_dt(value)
    if parsed is None:
        return _text(value)
    if isinstance(parsed, datetime):
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(IST)
        elif with_time:
            parsed = parsed.replace(tzinfo=timezone.utc).astimezone(IST)
        if with_time:
            return parsed.strftime("%d %b %Y, %I:%M %p")
        parsed = parsed.date()
    return parsed.strftime("%d %b %Y")


def _text(value) -> str:  # noqa: ANN001
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return _number(value, 0 if value.is_integer() else 2)
    if isinstance(value, int):
        return _number(value, 0)
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{label(k)}: {_text(v)}" for k, v in value.items())
    return str(value)


def format_value(value, col_type: str) -> str:  # noqa: ANN001
    if value is None or value == "":
        return ""
    if col_type == "currency":
        return money(value)
    if col_type == "date":
        return fmt_date(value)
    if col_type == "datetime":
        return fmt_date(value, with_time=True)
    if col_type == "percent":
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return _text(value)
    if col_type == "number":
        try:
            v = float(value)
        except (TypeError, ValueError):
            return _text(value)
        return _number(v, 0 if v.is_integer() else 2)
    if isinstance(value, str) and _bucket(value):
        return _bucket(value)
    if col_type == "status":
        return label(value) if isinstance(value, str) else _text(value)
    if isinstance(value, str) and _CODE.match(value):
        return label(value)  # a stored code such as supplier_invoice -> Supplier Invoice
    return _text(value)


_SUMMARY_PLAIN = ("count", "qty", "quantity", "units", "number", "days", "items", "invoices", "orders",
                  "transactions", "customers", "suppliers", "products", "rows", "entries")
_SUMMARY_PERCENT = ("percent", "margin_pct", "_pct", "rate")


def format_summary(key: str, value) -> str:  # noqa: ANN001
    k = key.lower()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
            return fmt_date(value, with_time="T" in value)
        return label(value) if isinstance(value, str) and "_" in value and " " not in value else _text(value)
    if any(t in k for t in _SUMMARY_PERCENT):
        return f"{float(value):.2f}%"
    if any(t in k for t in _SUMMARY_PLAIN) or (isinstance(value, int) and not any(
            t in k for t in ("amount", "total", "value", "balance", "sales", "paid", "tax", "profit"))):
        return format_value(value, "number")
    return money(value)


# ----------------------------------------------------------------------------- layout

class _Doc(FPDF):
    footer_text = ""

    def normalize_text(self, text):  # noqa: ANN001, ANN201
        """Every string fpdf draws or measures passes here. A character the font has no glyph
        for (Telugu, Devanagari, emoji...) becomes "?" instead of crashing the export; the
        marks / joiners attached to it are dropped so one syllable gives one "?"."""
        text = super().normalize_text(text)
        font = self.current_font
        glyphs = getattr(font, "cmap", None)
        if not glyphs or all(ord(ch) in glyphs for ch in text):
            return text
        out = []
        for ch in text:
            cat = unicodedata.category(ch)
            if ord(ch) in glyphs or ch.isspace():
                out.append(ch)
            elif cat in ("Mn", "Mc", "Me", "Cf") or (cat == "Sk" and out and out[-1] == "?"):
                continue  # vowel sign / virama / variation selector / ZWJ / skin tone of a replaced character
            elif not (out and out[-1] == "?" and cat.startswith("S")):
                out.append("?")  # one "?" per unsupported letter; an emoji sequence gives one "?"
        return "".join(out)

    def footer(self) -> None:  # noqa: D401
        self.set_y(-MARGIN + 1)
        self.set_font(FAMILY, "", 7)
        self.set_text_color(*MUTED)
        self.cell(0, 4, self.footer_text, align="L")
        self.set_x(self.l_margin)
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="R")


def _columns(report: dict) -> list[dict]:
    meta = report.get("meta") or {}
    cols = meta.get("columns") or []
    rows = report.get("rows") or []
    if cols:
        return [{"key": c["key"], "label": c.get("label") or label(c["key"]), "type": c.get("type") or "text"}
                for c in cols]
    if not rows:
        return []
    out = []
    for key in rows[0].keys():
        if _ID_KEY.search(key):
            continue  # never show raw ids when a column list is not given
        sample = next((r.get(key) for r in rows if r.get(key) not in (None, "")), None)
        if isinstance(sample, str) and _UUID.match(sample):
            continue
        out.append({"key": key, "label": label(key), "type": "number" if isinstance(sample, (int, float))
                    and not isinstance(sample, bool) else "text"})
    return out


class _Measure:
    """String widths from cached per-character advances (no kerning or shaping is used, so
    this equals fpdf's own get_string_width) -- fast enough for thousands of cells."""

    def __init__(self, pdf: FPDF) -> None:
        self.pdf = pdf
        self.cache: dict[str, dict[str, float]] = {"": {}, "B": {}}

    def w(self, text: str, style: str, size: float) -> float:
        table = self.cache[style]
        total = 0.0
        for ch in text:
            adv = table.get(ch)
            if adv is None:
                self.pdf.set_font(FAMILY, style, 10)
                adv = table[ch] = self.pdf.get_string_width(ch) / 10
            total += adv
        return total * size

    def wrap(self, text: str, width: float, style: str, size: float) -> list[str]:
        """Greedy word wrap inside `width`; a word longer than the line breaks by character."""
        if not text:
            return [""]
        out: list[str] = []
        for para in str(text).splitlines() or [""]:
            line = ""
            for word in para.split(" "):
                candidate = f"{line} {word}" if line else word
                if self.w(candidate, style, size) <= width:
                    line = candidate
                    continue
                if line:
                    out.append(line)
                    line = ""
                while self.w(word, style, size) > width and len(word) > 1:
                    cut = 1
                    while cut < len(word) and self.w(word[:cut + 1], style, size) <= width:
                        cut += 1
                    # Prefer to break a long number / code after a separator (/ - _ , .).
                    sep = max(word.rfind(ch, 0, cut) for ch in "/-_,.")
                    if sep >= cut // 3:
                        cut = sep + 1
                    out.append(word[:cut])
                    word = word[cut:]
                line = word
            out.append(line)
        return out or [""]


def _natural_widths(m: "_Measure", cols: list[dict], cells: list[list[str]], size: float) -> list[tuple[float, float, float]]:
    """(min, natural, max) width per column at font `size`."""
    out = []
    for i, col in enumerate(cols):
        lo, hi = WIDTH_LIMITS.get(col["type"], WIDTH_LIMITS["text"])
        # Header: its longest word must fit; the whole label may wrap to two lines.
        words = col["label"].split()
        head_word = max((m.w(w, "B", size) for w in words), default=0) + 2 * PAD_X + 0.4
        head_full = m.w(col["label"], "B", size) + 2 * PAD_X
        widest = 0.0
        longest_word = 0.0
        for row in cells:
            text = row[i]
            if not text:
                continue
            widest = max(widest, m.w(text, "", size))
            if col["type"] in ("text", "reference", "status"):
                longest_word = max(longest_word, max(m.w(w, "", size) for w in text.split() or [text]))
        content = widest + 2 * PAD_X + 0.4  # a hair of slack so a full-width value never tips over
        word_cap = min(hi, 40.0) if col["type"] == "reference" else hi  # longer codes break at / or -
        lo = max(lo, head_word, min(longest_word + 2 * PAD_X + 0.4, word_cap))
        if col["type"] in RIGHT_ALIGNED or col["type"] in ("date", "datetime", "status"):
            lo = max(lo, content)  # amounts, dates and statuses stay on one line
            hi = max(hi, lo)
        natural = max(lo, min(max(content, head_full * 0.6), hi))
        out.append((lo, natural, max(hi, lo), max(content, natural)))
    return out


def _fit(widths: list[tuple[float, float, float]], avail: float) -> list[float] | None:
    """Fit to `avail`: extra space goes to wrapping columns, a shortfall is taken from them
    down to their minimum. None when even the minimums do not fit."""
    mins = [w[0] for w in widths]
    if sum(mins) > avail + 0.01:
        return None
    nat = [w[1] for w in widths]
    total = sum(nat)
    if total <= avail:
        # Spare room first goes to columns whose text still wraps (up to their full
        # content width), then evenly to all.
        extra = avail - total
        need = [max(w[3] - w[1], 0.0) for w in widths]
        if sum(need) > 0:
            give = min(extra, sum(need))
            nat = [n + give * d / sum(need) for n, d in zip(nat, need)]
            extra -= give
        if extra > 0:
            nat = [n + extra / len(nat) for n in nat]
        return nat
    give = [n - lo for n, lo in zip(nat, mins)]
    short = total - avail
    return [n - short * g / sum(give) if sum(give) else n for n, g in zip(nat, give)]


def _put(pdf: FPDF, m: "_Measure", line: str, x: float, w: float, top: float, style: str, size: float,
         line_h: float, right: bool) -> None:
    """One line of cell text at its baseline (fpdf's text(); cell() is ~10x slower)."""
    if not line:
        return
    tx = x + w - PAD_X - m.w(line, style, size) if right else x + PAD_X
    pdf.text(tx, top + line_h / 2 + size * 0.3528 * 0.35, line)


def render(report: dict, org_name: str | None = None, stats: dict | None = None) -> bytes:
    """The PDF bytes. `stats`, when given, is filled with layout facts for tests (rows drawn,
    pages carrying the table header, any cell text that did not fit, orientation...)."""
    cols = _columns(report)
    rows = report.get("rows") or []
    cells = [[format_value(r.get(c["key"]), c["type"]) for c in cols] for r in rows]

    probe = _Doc(orientation="P", unit="mm", format="A4")
    for style, name in (("", "Regular"), ("B", "Bold")):
        probe.add_font(FAMILY, style, str(FONT_DIR / f"DMSans-{name}.ttf"))
    m = _Measure(probe)

    layout = None
    for orientation, page_w in (("P", 210.0), ("L", 297.0)):
        for size in (8.0, 7.5, 7.0, 6.5, 6.0):
            widths = _natural_widths(m, cols, cells, size) if cols else []
            avail = page_w - 2 * MARGIN
            if orientation == "P" and sum(w[1] for w in widths) > avail:
                break  # does not fit portrait at its natural width: go landscape
            fitted = _fit(widths, avail) if cols else []
            if fitted is not None:
                layout = (orientation, size, fitted)
                break
        if layout:
            break
    if layout is None:
        # Very many columns: smallest type; amounts, dates and headers keep their minimum and
        # the wrapping (text / reference) columns share what is left, breaking long words.
        widths = _natural_widths(m, cols, cells, 6.0)
        mins = [w[0] for w in widths]
        avail = 297.0 - 2 * MARGIN
        # Short single-token codes (GSTIN, HSN, SKU) keep their width: breaking a 15-character
        # GSTIN over two lines makes it unreadable.
        def _code_column(i: int) -> bool:
            vals = [row[i] for row in cells if row[i]]
            return bool(vals) and all(len(v) <= 16 and " " not in v for v in vals)

        flex = [i for i, col in enumerate(cols) if col["type"] in WRAPPING and not _code_column(i)]
        fixed = sum(mins[i] for i in range(len(cols)) if i not in flex)
        room = avail - fixed
        if flex and room > 10.0 * len(flex):
            share = sum(mins[i] for i in flex)
            col_w = [mins[i] * room / share if i in flex else mins[i] for i in range(len(cols))]
        else:
            col_w = [x * avail / sum(mins) for x in mins]
        layout = ("L", 6.0, col_w)
    orientation, size, col_w = layout
    if stats is not None:
        stats.update(orientation=orientation, font_size=size, col_widths=col_w, rows_drawn=0,
                     header_pages=[], lost_text=[], overflow=[], row_bottom_max=0.0, bottom=None)

    pdf = _Doc(orientation=orientation, unit="mm", format="A4")
    for style, name in (("", "Regular"), ("B", "Bold")):
        pdf.add_font(FAMILY, style, str(FONT_DIR / f"DMSans-{name}.ttf"))
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(False)
    pdf.alias_nb_pages()
    generated = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    title = (report.get("meta") or {}).get("title") or label(str(report.get("type") or "Report").replace("-", " "))
    if not title.lower().endswith("report"):
        title = f"{title} Report"
    pdf.footer_text = f"{org_name + '  ·  ' if org_name else ''}{title}  ·  Generated {generated}"
    bottom = pdf.h - MARGIN - 6  # room for the footer
    width = pdf.w - 2 * MARGIN
    if stats is not None:
        stats.update(bottom=bottom, width=width, table_width=sum(col_w))
    line_h = size * 0.3528 * 1.25

    pdf.add_page()
    pdf.set_text_color(*TEXT)
    if org_name:
        pdf.set_font(FAMILY, "B", 13)
        pdf.multi_cell(width, 6, org_name, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FAMILY, "B", 11)
    pdf.multi_cell(width, 5.5, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FAMILY, "", 8.5)
    pdf.set_text_color(*MUTED)
    if report.get("as_of_date"):
        period = f"As of {fmt_date(report['as_of_date'])}"
    else:
        start = fmt_date(report["date_from"]) if report.get("date_from") else "All time"
        end = fmt_date(report["date_to"]) if report.get("date_to") else "Present"
        period = f"Period: {start} to {end}"
    pdf.multi_cell(width, 4.5, f"{period}    ·    {len(rows)} row{'s' if len(rows) != 1 else ''}",
                   new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*TEXT)

    # Summary: label / value tiles, as many per line as fit.
    summary = {k: v for k, v in (report.get("summary") or {}).items() if not isinstance(v, (dict, list))}
    if summary:
        pdf.ln(2)
        tile_w = 46.0 if orientation == "L" else 45.0
        per_line = max(1, int(width // tile_w))
        tile_w = width / per_line
        items = list(summary.items())
        for start in range(0, len(items), per_line):
            chunk = items[start:start + per_line]
            y = pdf.get_y()
            heights = []
            for j, (k, v) in enumerate(chunk):
                pdf.set_xy(MARGIN + j * tile_w, y)
                pdf.set_font(FAMILY, "", 7)
                pdf.set_text_color(*MUTED)
                pdf.multi_cell(tile_w - 2, 3.4, label(k), new_x="LEFT", new_y="NEXT")
                pdf.set_font(FAMILY, "B", 9)
                pdf.set_text_color(*TEXT)
                pdf.multi_cell(tile_w - 2, 4.4, format_summary(k, v), new_x="LEFT", new_y="NEXT")
                heights.append(pdf.get_y() - y)
            pdf.set_xy(MARGIN, y + max(heights) + 1.5)
            if pdf.get_y() > bottom - 20:
                pdf.add_page()
    pdf.ln(2)

    if not cols:
        pdf.set_font(FAMILY, "", 9)
        pdf.multi_cell(width, 5, "No rows for the selected filters.")
        return bytes(pdf.output())

    def header_height() -> float:
        return max(len(m.wrap(c["label"], w - 2 * PAD_X, "B", size)) for c, w in zip(cols, col_w)) * line_h + 2 * PAD_Y

    def draw_header() -> None:
        h = header_height()
        y = pdf.get_y()
        x = MARGIN
        pdf.set_draw_color(*BORDER)
        for c, w in zip(cols, col_w):
            pdf.set_fill_color(*HEADER_FILL)
            pdf.rect(x, y, w, h, style="DF")
            pdf.set_text_color(*HEADER_TEXT)
            pdf.set_xy(x + PAD_X, y + PAD_Y)
            pdf.set_font(FAMILY, "B", size)
            for i, line in enumerate(m.wrap(c["label"], w - 2 * PAD_X, "B", size)):
                _put(pdf, m, line, x, w, y + PAD_Y + i * line_h, "B", size, line_h, c["type"] in RIGHT_ALIGNED)
            x += w
        pdf.set_text_color(*TEXT)
        pdf.set_xy(MARGIN, y + h)
        if stats is not None:
            stats["header_pages"].append(pdf.page_no())

    draw_header()
    if not rows:
        pdf.set_font(FAMILY, "", 8)
        pdf.multi_cell(width, 5, "No rows for the selected filters.")
    pdf.set_font(FAMILY, "", size)
    for n, row in enumerate(cells):
        wrapped = [m.wrap(text, w - 2 * PAD_X, "", size) for text, w in zip(row, col_w)]
        h = max(len(lines) for lines in wrapped) * line_h + 2 * PAD_Y
        if pdf.get_y() + h > bottom:
            pdf.add_page()
            draw_header()
            pdf.set_font(FAMILY, "", size)
        y = pdf.get_y()
        x = MARGIN
        for c, w, lines in zip(cols, col_w, wrapped):
            pdf.set_draw_color(*BORDER)
            pdf.set_fill_color(*(ZEBRA if n % 2 else (255, 255, 255)))
            pdf.rect(x, y, w, h, style="DF")
            align = "R" if c["type"] in RIGHT_ALIGNED else "L"
            right = align == "R"
            for i, line in enumerate(lines):
                _put(pdf, m, line, x, w, y + PAD_Y + i * line_h, "", size, line_h, right)
            if stats is not None:
                text = row[cols.index(c)]
                if "".join("".join(lines).split()) != "".join(text.split()):
                    stats["lost_text"].append((n, c["key"], text))
                widest = max((pdf.get_string_width(line) for line in lines), default=0.0)
                if widest > w - 2 * PAD_X + 0.05:
                    stats["overflow"].append((n, c["key"], round(widest, 2), round(w - 2 * PAD_X, 2)))
            x += w
        pdf.set_xy(MARGIN, y + h)
        if stats is not None:
            stats["rows_drawn"] += 1
            stats["row_bottom_max"] = max(stats["row_bottom_max"], y + h)
    if stats is not None:
        stats["pages"] = pdf.page_no()
    return bytes(pdf.output())
