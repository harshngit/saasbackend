"""Regular (A4/A5) invoice PDF renderer.

Mirrors the frontend's single regular-invoice component -- `RegularInvoiceDocument` in
src/features/invoices/invoiceTemplates.jsx -- for every catalog theme. That component
draws all 14 themes from one layout and five style axes (header, table, totals, accent,
density), so this module does the same instead of keeping 14 separate renderers.

Geometry: the frontend Live Preview lays an A4 page out as a 640px-wide box with 20px
padding (InvoiceSettings.jsx BASE_WIDTH_PX / ScaledDocument), i.e. a 600px content box;
A5 is 480px. Every frontend pixel value below is scaled by `usable width / content px`,
so the PDF keeps the preview's proportions inside the firm's own configured margins.
Typography sizes are CSS points inside that box, converted the same way.

Text: latin-1 text uses the firm's core font (Helvetica/Times/Courier -- the frontend's
font stack is Helvetica/Arial). Characters a core font cannot encode (the rupee sign,
ellipsis, em dash, non-Latin names) are drawn with the bundled DM Sans -- the frontend's
own web font, chosen because its Open Sans files lack the rupee glyph
(app/assets/fonts/dm-sans, SIL Open Font License 1.1, see OFL.txt there).

Thermal receipts never come here; they keep their own renderer in pdf_docs.py.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fpdf import FPDF

from app.core.workflow import REGULAR_THEMES_BY_ID

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "dm-sans"
_UNICODE_FAMILY = "dmsans"

DEFAULT_PRIMARY = "#00092A"  # withDefaults() in invoiceTemplates.jsx

# Tailwind neutral scale as overridden in the frontend's src/index.css.
NEUTRAL = {
    50: (0xF8, 0xF9, 0xFA), 100: (0xF1, 0xF2, 0xF4), 200: (0xE4, 0xE6, 0xE9), 300: (0xD1, 0xD5, 0xDB),
    400: (0x9A, 0xA1, 0xAC), 500: (0x6B, 0x72, 0x80), 600: (0x4B, 0x55, 0x63), 700: (0x37, 0x41, 0x51),
    800: (0x1F, 0x29, 0x37),
}
WHITE = (255, 255, 255)

PREVIEW_CONTENT_PX = {"A4": 600.0, "A5": 440.0}
LINE_HEIGHT = 4 / 3  # Tailwind text-xs line-height, inherited by the whole document
ROW_PAD_PX = {"classic": 6.0, "modern": 8.0, "compact": 4.0}  # rowPad py-1.5 / py-2 / py-1
DENSITY_GAP_PX = {"compact": 8.0, "normal": 12.0, "spacious": 16.0}
HEADER_PAD_PX = {"band": 14.0, "band-sm": 10.0, "banner": 20.0}

# Same keys/weights/alignment as the frontend's invoiceColumns.js (and pdf_docs.COLUMN_WEIGHTS).
COLUMNS = {
    "product": ("Item", 42, "L"),
    "description": ("Description", 30, "L"),
    "product_image": ("Product Image", 16, "C"),
    "hsn_sac": ("HSN/SAC", 18, "C"),
    "quantity": ("Quantity", 14, "R"),
    "uom": ("UOM", 12, "C"),
    "rate": ("Rate", 20, "R"),
    "mrp": ("MRP", 18, "R"),
    "discount": ("Discount", 16, "R"),
    "tax_rate": ("Tax Rate", 14, "R"),
    "tax_amount": ("Tax Amount", 18, "R"),
    "batch_number": ("Batch Number", 16, "C"),
    "expiry_date": ("Expiry Date", 18, "C"),
    "amount": ("Amount", 24, "R"),
}
DEFAULT_COLUMNS = ["product", "hsn_sac", "quantity", "rate", "amount"]

# The frontend's only definition of the simple format (InvoicePrintView.jsx): the same
# document with payment details, the footer's terms / footer text / signature, and the
# Discount and Tax total rows hidden.


# ------------------------------------------------------------------ formatting


def inr(value) -> str:
    """The frontend's money(): rupee sign and Indian digit grouping, e.g. ₹ 1,23,456.00."""
    amount = float(value or 0)
    sign = "-" if amount < 0 else ""
    whole, frac = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"₹ {sign}{whole}.{frac}"


def date_label(value) -> str:
    """The frontend's formatDateLabel: '01 Jul 2026', or an em dash when unset."""
    if not value:
        return "—"
    return value.strftime("%d %b %Y")


def _hex_rgb(value) -> tuple[int, int, int] | None:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _mix(fg: tuple[int, int, int], bg: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    """`fg` at `alpha` opacity over `bg` -- the solid colour of e.g. text-white/85."""
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


def resolve_theme(settings: dict) -> dict:
    """resolveRegularPresetId(): the saved variant if the catalog knows it, else the theme
    named after the base template, else classic."""
    variant = str(settings.get("template_variant") or "").strip().lower()
    if variant in REGULAR_THEMES_BY_ID:
        return REGULAR_THEMES_BY_ID[variant]
    base = str(settings.get("template") or "classic").strip().lower()
    return REGULAR_THEMES_BY_ID.get(base, REGULAR_THEMES_BY_ID["classic"])


# ------------------------------------------------------------------ text engine


class _Doc:
    """Page cursor plus run-aware text measuring and drawing."""

    def __init__(self, pdf: FPDF, settings: dict, theme: dict):
        self.pdf = pdf
        self.settings = settings
        self.theme = theme
        typo = settings.get("typography") or {}
        family = str(typo.get("font_family") or "Helvetica").strip().title()
        self.family = family.lower() if family in ("Helvetica", "Times", "Courier") else "helvetica"
        paper = str(((settings.get("regular_print") or {}).get("paper_size")) or settings.get("paper_size") or "A4").upper()
        self.left = pdf.l_margin
        self.width = pdf.w - pdf.l_margin - pdf.r_margin
        self.k = self.width / PREVIEW_CONTENT_PX.get(paper, PREVIEW_CONTENT_PX["A4"])  # mm per preview px
        self.bottom = pdf.h - pdf.b_margin
        self.y = pdf.t_margin
        pdf.set_auto_page_break(False)
        for style, name in (("", "Regular"), ("B", "Bold"), ("I", "Italic")):
            pdf.add_font(_UNICODE_FAMILY, style, str(_FONT_DIR / f"DMSans-{name}.ttf"))

        primary = _hex_rgb((settings.get("branding") or {}).get("primary_color"))
        self.primary = primary or _hex_rgb(DEFAULT_PRIMARY)
        self.body_px = float(typo.get("body_size", 9)) * 4 / 3
        self.heading_px = float(typo.get("heading_size", 16)) * 4 / 3
        self.table_px = float(typo.get("table_size", 8)) * 4 / 3

    # -- units
    def px(self, value: float) -> float:
        return value * self.k

    def pt(self, size_px: float) -> float:
        return size_px * self.k * 72 / 25.4

    def line(self, size_px: float) -> float:
        return self.px(size_px * LINE_HEIGHT)

    # -- pages
    def ensure(self, height: float) -> bool:
        """Start a new page when `height` does not fit; True when it did."""
        if self.y + height <= self.bottom:
            return False
        self.pdf.add_page()
        self.y = self.pdf.t_margin
        return True

    # -- runs
    @staticmethod
    def _runs(text: str) -> list[tuple[bool, str]]:
        runs: list[tuple[bool, str]] = []
        for ch in text:
            uni = ord(ch) > 255
            if runs and runs[-1][0] == uni:
                runs[-1] = (uni, runs[-1][1] + ch)
            else:
                runs.append((uni, ch))
        return runs

    def _font(self, uni: bool, size_px: float, style: str) -> None:
        style = "B" if "B" in style else ("I" if "I" in style else "")
        self.pdf.set_font(_UNICODE_FAMILY if uni else self.family, style, max(self.pt(size_px), 3.0))

    def measure(self, text: str, size_px: float, style: str = "", spacing_em: float = 0.0) -> float:
        total = 0.0
        for uni, chunk in self._runs(text):
            self._font(uni, size_px, style)
            total += self.pdf.get_string_width(chunk)
        return total + len(text) * self.px(size_px * spacing_em)

    def fit(self, text: str, width: float, size_px: float, style: str = "") -> str:
        """Truncate with an ellipsis to fit `width` (the frontend's `truncate`)."""
        text = str(text or "")
        if self.measure(text, size_px, style) <= width:
            return text
        while text and self.measure(text + "…", size_px, style) > width:
            text = text[:-1]
        return (text + "…") if text else ""

    def wrap(self, text: str, width: float, size_px: float, style: str = "") -> list[str]:
        lines: list[str] = []
        for paragraph in str(text or "").splitlines() or [""]:
            current = ""
            for word in paragraph.split(" "):
                candidate = f"{current} {word}" if current else word
                if self.measure(candidate, size_px, style) <= width:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                while self.measure(word, size_px, style) > width and len(word) > 1:
                    cut = len(word)
                    while cut > 1 and self.measure(word[:cut], size_px, style) > width:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
                current = word
            lines.append(current)
        return lines

    def text(
        self, x: float, y: float, width: float, text: str, size_px: float, *,
        style: str = "", color=NEUTRAL[600], align: str = "L", spacing_em: float = 0.0,
        height: float | None = None,
    ) -> None:
        """One line of text in the box (x, y, width, height), vertically centred."""
        text = str(text or "")
        if not text:
            return
        height = height if height is not None else self.line(size_px)
        total = self.measure(text, size_px, style, spacing_em)
        if align == "R":
            x = x + width - total
        elif align == "C":
            x = x + (width - total) / 2
        self.pdf.set_text_color(*color)
        self.pdf.set_char_spacing(self.pt(size_px * spacing_em) if spacing_em else 0)
        for uni, chunk in self._runs(text):
            self._font(uni, size_px, style)
            w = self.pdf.get_string_width(chunk) + len(chunk) * self.px(size_px * spacing_em)
            self.pdf.set_xy(x, y)
            self.pdf.cell(w, height, chunk)
            x += w
        self.pdf.set_char_spacing(0)
        self.pdf.set_text_color(0, 0, 0)

    def rule(self, x1: float, y: float, x2: float, width_px: float, color) -> None:
        self.pdf.set_draw_color(*color)
        self.pdf.set_line_width(self.px(width_px))
        self.pdf.line(x1, y, x2, y)

    def box(self, x, y, w, h, *, fill=None, border=None, border_px: float = 1.0, radius_px: float = 0.0) -> None:
        style = ("F" if fill else "") + ("D" if border else "")
        if not style:
            return
        if fill:
            self.pdf.set_fill_color(*fill)
        if border:
            self.pdf.set_draw_color(*border)
            self.pdf.set_line_width(self.px(border_px))
        kwargs = {"round_corners": True, "corner_radius": self.px(radius_px)} if radius_px else {}
        self.pdf.rect(x, y, w, h, style=style, **kwargs)

    def image(self, data: bytes | None, x: float, y: float, w: float, h: float, align: str = "C") -> float:
        """object-contain: fit inside (w, h) keeping the aspect ratio. Returns the drawn
        width (0 when the asset is missing or unreadable -- the frontend's onError hide)."""
        if not data:
            return 0.0
        try:
            from PIL import Image

            with Image.open(BytesIO(data)) as img:
                iw, ih = img.size
            scale = min(w / iw, h / ih)
            dw, dh = iw * scale, ih * scale
            dx = x + (w - dw) / 2 if align == "C" else (x + w - dw if align == "R" else x)
            self.pdf.image(BytesIO(data), x=dx, y=y + (h - dh) / 2, w=dw, h=dh)
            return dw
        except Exception:  # noqa: BLE001 -- a broken asset is skipped, never fatal
            return 0.0


# ------------------------------------------------------------------ data


def _company(org) -> dict:
    """buildInvoicePreviewData().company -- the same fields and fallbacks."""
    if org is None:
        return {"name": "Your Company"}
    city_line = ", ".join(str(v) for v in (getattr(org, "city", None), getattr(org, "state", None), getattr(org, "pin_code", None)) if v)
    return {
        "name": org.name or "Your Company",
        "address": getattr(org, "registered_address", None) or getattr(org, "address", None) or "",
        "city_line": city_line,
        "gstin": getattr(org, "gst_number", None) or "",
        "phone": getattr(org, "phone", None) or "",
        "email": getattr(org, "email", None) or "",
        "pan": getattr(org, "pan_number", None) or getattr(org, "gstin_pan", None) or "",
    }


def _mrp(item):
    """MRP is not snapshotted on invoice lines; use the product's own MRP field
    (variant, then product pricing, then the product profile), never the sale price."""
    variant = getattr(item, "variant", None)
    product = getattr(item, "product", None)
    for value in (
        getattr(variant, "mrp", None),
        getattr(getattr(product, "pricing", None), "mrp", None),
        getattr(product, "msrp_mrp", None),
    ):
        if value is not None:
            return value
    return None


def _cell(item, key: str) -> str:
    """The frontend's ItemTable cellText(), on the backend's real line fields."""
    dash = "—"
    if key == "product":
        return item.product_name or ""
    if key == "description":
        product = getattr(item, "product", None)
        return (getattr(product, "description", None) or dash) if product else dash
    if key == "hsn_sac":
        return item.hsn_code or dash
    if key == "quantity":
        return f"{item.quantity:g}"
    if key == "uom":
        product = getattr(item, "product", None)
        return getattr(item, "uom", None) or getattr(product, "uom", None) or dash
    if key == "rate":
        return inr(item.unit_price)
    if key == "mrp":
        mrp = _mrp(item)
        return inr(mrp) if mrp is not None else dash
    if key == "discount":
        # Stored as a rupee amount (see routers/invoices.py), so it prints as money.
        return inr(item.discount) if item.discount else dash
    if key == "tax_rate":
        return f"{item.tax_rate:g}%" if item.tax_rate else dash
    if key == "tax_amount":
        amount = item.tax_amount if item.tax_amount is not None else item.tax
        return inr(amount) if amount is not None else dash
    if key == "batch_number":
        return item.batch_number or dash
    if key == "expiry_date":
        return item.expiry_date.date().isoformat() if item.expiry_date else dash
    if key == "amount":
        return inr(item.line_total)
    return dash


def _columns(settings: dict) -> list[str]:
    """resolveActiveColumns(): configured order, unknown keys dropped, product/amount
    guaranteed, product_image only with show_product_image."""
    cfg = settings.get("item_table") or {}
    configured = cfg.get("columns") or DEFAULT_COLUMNS
    seen: list[str] = []
    for key in configured:
        if key in COLUMNS and key not in seen:
            seen.append(key)
    for key in ("product", "amount"):
        if key not in seen:
            seen.append(key)
    return [key for key in seen if key != "product_image" or cfg.get("show_product_image")]


# ------------------------------------------------------------------ sections


def _company_lines(doc: _Doc, company: dict, biz: dict, on_color: bool) -> list[tuple[str, float, str, tuple]]:
    sub = _mix(WHITE, doc.primary, 0.85) if on_color else NEUTRAL[500]
    lines = []
    if biz.get("show_business_name", True):
        lines.append((company["name"], doc.heading_px, "B", WHITE if on_color else doc.primary))
    if biz.get("show_address", True):
        for part in (company.get("address"), company.get("city_line")):
            if part:
                lines.append((part, doc.body_px, "", sub))
    for flag, key, label in (("show_gstin", "gstin", "GSTIN: "), ("show_pan", "pan", "PAN: "),
                             ("show_phone", "phone", "Ph: "), ("show_email", "email", "")):
        if biz.get(flag, flag != "show_pan") and company.get(key):
            lines.append((f"{label}{company[key]}", doc.body_px, "", sub))
    return lines


def _meta_lines(doc: _Doc, invoice, inv_cfg: dict, fields: dict, color) -> list[tuple[str, float, str, tuple]]:
    order = getattr(invoice, "order", None)
    vehicle = getattr(invoice, "vehicle_number", None)
    delivery = getattr(invoice, "delivery", None)
    if not vehicle and delivery is not None and getattr(delivery, "vehicle", None) is not None:
        vehicle = getattr(delivery.vehicle, "vehicle_number", None)
    rows = [
        (inv_cfg.get("show_invoice_number", True), f"Invoice No: {invoice.invoice_number}"),
        (inv_cfg.get("show_invoice_date", True), f"Date: {date_label(invoice.invoice_date)}"),
        (inv_cfg.get("show_due_date", True), f"Due: {date_label(invoice.due_date)}"),
        (inv_cfg.get("show_order_reference", True) and order is not None, f"Order: {getattr(order, 'order_number', '')}"),
        (inv_cfg.get("show_po_number", fields.get("show_po_number", False)) and getattr(order, "customer_po_number", None),
         f"PO: {getattr(order, 'customer_po_number', '')}"),
        (inv_cfg.get("show_eway_bill_number", fields.get("show_eway_bill_number", False)) and getattr(invoice, "eway_bill_number", None),
         f"E-Way Bill: {getattr(invoice, 'eway_bill_number', '')}"),
        (inv_cfg.get("show_vehicle_number", fields.get("show_vehicle_number", False)) and vehicle, f"Vehicle: {vehicle}"),
    ]
    return [(text, doc.body_px, "", color) for show, text in rows if show]


def _stack_height(doc: _Doc, lines, extra_first_px: float = 0.0) -> float:
    return sum(doc.line(size) for _, size, _, _ in lines) + doc.px(extra_first_px)


def _draw_stack(doc: _Doc, x: float, y: float, width: float, lines, align: str = "L", spacing=None) -> float:
    for index, (text, size, style, color) in enumerate(lines):
        doc.text(x, y, width, doc.fit(text, width, size, style), size, style=style, color=color,
                 align=align, spacing_em=(spacing or {}).get(index, 0.0))
        y += doc.line(size)
    return y


def _header(doc: _Doc, org, invoice, settings: dict, logo: bytes | None) -> None:
    theme = doc.theme
    header = theme["header"]
    biz = settings.get("business_details") or {}
    inv_cfg = settings.get("invoice_details") or {}
    fields = settings.get("fields") or {}
    company = _company(org)
    show_logo = biz.get("show_logo", True) and logo
    x0, width = doc.left, doc.width

    if header == "banner-split":
        pad = doc.px(16)
        name_lines = [(company["name"], doc.heading_px, "B", WHITE)] if biz.get("show_business_name", True) else []
        sub = (("Tax Invoice").upper(), 12.0, "", _mix(WHITE, doc.primary, 0.85))
        banner_h = pad * 2 + _stack_height(doc, name_lines) + doc.px(2) + doc.line(12.0)
        doc.ensure(banner_h)
        doc.box(x0, doc.y, width, banner_h, fill=doc.primary, radius_px=12)
        y = _draw_stack(doc, x0 + pad, doc.y + pad, width - 2 * pad, name_lines, align="C", spacing={0: 0.025})
        _draw_stack(doc, x0 + pad, y + doc.px(2), width - 2 * pad, [sub], align="C", spacing={0: 0.2})
        doc.y += banner_h + doc.px(10)

        left = []
        if biz.get("show_address", True):
            address = ", ".join(p for p in (company.get("address"), company.get("city_line")) if p)
            if address:
                left.append((address, doc.body_px, "", NEUTRAL[500]))
        if biz.get("show_gstin", True) and company.get("gstin"):
            left.append((f"GSTIN: {company['gstin']}", doc.body_px, "", NEUTRAL[500]))
        right = _meta_lines(doc, invoice, inv_cfg, fields, NEUTRAL[500])
        right_w = max((doc.measure(t, s, st) for t, s, st, _ in right), default=0)
        left_w = width - right_w - doc.px(12)
        h = max(_stack_height(doc, left), _stack_height(doc, right))
        _draw_stack(doc, x0, doc.y, left_w, left)
        _draw_stack(doc, x0 + width - right_w, doc.y, right_w, right, align="R")
        doc.y += h + doc.px(10)
        doc.rule(x0, doc.y, x0 + width, 1, NEUTRAL[100])
        return

    on_color = header in HEADER_PAD_PX
    pad = doc.px(HEADER_PAD_PX[header]) if on_color else 0.0
    inner_x, inner_w = x0 + pad, width - 2 * pad

    # Right-hand block: title + invoice details.
    if on_color:
        title = ("INVOICE", doc.body_px * (1.1 if header == "banner" else 1.0), "B", WHITE)
        meta_color, title_spacing = _mix(WHITE, doc.primary, 0.85), 0.1
    elif header in ("split", "split-alt"):
        title, meta_color, title_spacing = ("TAX INVOICE", doc.body_px, "B", doc.primary), NEUTRAL[600], 0.025
    else:
        spacious = header == "plain-spacious"
        title = ("TAX INVOICE", doc.body_px * (1.05 if spacious else 1.0), "B", NEUTRAL[600])
        meta_color, title_spacing = NEUTRAL[600], 0.025
    meta = _meta_lines(doc, invoice, inv_cfg, fields, meta_color)
    right = [title] + meta
    right_w = max(doc.measure(t, s, st, title_spacing if i == 0 else 0) for i, (t, s, st, _) in enumerate(right))
    right_h = _stack_height(doc, right) + (doc.px(2) if meta and inv_cfg.get("show_invoice_number", True) else 0)

    # Left-hand block: logo beside the company text.
    logo_px = 36.0
    lines = _company_lines(doc, company, biz, on_color)
    gap = doc.px(16 if header in ("split", "split-alt") else 12)
    text_x_off = doc.px(logo_px + 10) if show_logo else 0.0
    company_w = max(inner_w - right_w - gap - text_x_off, inner_w * 0.3)
    left_h = max(_stack_height(doc, lines) + (doc.px(2) if len(lines) > 1 else 0), doc.px(logo_px) if show_logo else 0)

    content_h = max(left_h, right_h)
    if on_color:
        block_h = content_h + 2 * pad
    else:
        block_h = content_h + doc.px(14 if header == "plain-spacious" else 10)
    doc.ensure(block_h + doc.px(2))
    top = doc.y
    if on_color:
        doc.box(x0, top, width, block_h, fill=doc.primary, radius_px=12)

    swap = header == "split-alt"
    company_x = inner_x + inner_w - (company_w + text_x_off) if swap else inner_x
    right_x = inner_x if swap else inner_x + inner_w - right_w

    y = top + pad
    if show_logo:
        if on_color:
            doc.box(company_x, y, doc.px(logo_px), doc.px(logo_px), fill=_mix(WHITE, doc.primary, 0.9), radius_px=4)
            doc.image(logo, company_x + doc.px(2), y + doc.px(2), doc.px(logo_px - 4), doc.px(logo_px - 4))
        else:
            doc.image(logo, company_x, y, doc.px(logo_px), doc.px(logo_px))
    ty = y
    for index, (text, size, style, color) in enumerate(lines):
        if index == 1:
            ty += doc.px(2)  # mt-0.5 above the address
        doc.text(company_x + text_x_off, ty, company_w, doc.fit(text, company_w, size, style), size, style=style, color=color)
        ty += doc.line(size)

    ry = y
    for index, (text, size, style, color) in enumerate(right):
        if index == 1 and inv_cfg.get("show_invoice_number", True):
            ry += doc.px(2)  # mt-0.5 above Invoice No
        doc.text(right_x, ry, right_w, text, size, style=style, color=color, align="R",
                 spacing_em=title_spacing if index == 0 else 0.0)
        ry += doc.line(size)

    doc.y = top + block_h
    if not on_color:
        border_px = 1 if header == "plain-spacious" else 2
        doc.rule(x0, doc.y + doc.px(border_px) / 2, x0 + width, border_px, doc.primary)
        doc.y += doc.px(border_px)


def _party_column(doc: _Doc, heading: str, rows: list[tuple[str, str, tuple]], width: float, accent) -> list:
    out = [(heading, doc.body_px, "B", accent)]
    for text, style, color in rows:
        for wrapped in doc.wrap(text, width, doc.body_px, style):
            out.append((wrapped, doc.body_px, style, color))
    return out


def _party(doc: _Doc, customer, invoice, settings: dict) -> bool:
    party = settings.get("party_details") or {}
    fields = settings.get("fields") or {}
    if not party.get("show_customer_name", True) and not party.get("show_billing_address", fields.get("show_billing_address", True)):
        return False
    accent = doc.primary if doc.theme["accent"] == "bold" else NEUTRAL[600]

    name = (customer.business_name or customer.name) if customer is not None else (
        getattr(invoice, "walk_in_name", None) or "Walk-in Customer")
    phone = customer.phone if customer is not None else getattr(invoice, "walk_in_phone", None)
    gstin = customer.gst_number if customer is not None else None
    billing = invoice.billing_address or (customer.billing_address if customer is not None else None)
    shipping = getattr(customer, "delivery_address", None) if customer is not None else None
    show_ship = party.get("show_shipping_address", fields.get("show_shipping_address", True)) and shipping

    rows = []
    if party.get("show_customer_name", True):
        rows.append((name, "", NEUTRAL[800]))
    if party.get("show_billing_address", fields.get("show_billing_address", True)) and billing:
        rows.append((billing, "", NEUTRAL[500]))
    if party.get("show_customer_phone", True) and phone:
        rows.append((f"Ph: {phone}", "", NEUTRAL[500]))
    if party.get("show_customer_gstin", fields.get("show_customer_gstin", True)) and gstin:
        rows.append((f"GSTIN: {gstin}", "", NEUTRAL[500]))

    col_w = (doc.width - doc.px(16)) / 2 if show_ship else doc.width
    bill = _party_column(doc, "Bill To", rows, col_w, accent)
    ship = _party_column(doc, "Ship To", [(shipping, "", NEUTRAL[500])], col_w, accent) if show_ship else []
    height = max(_stack_height(doc, bill), _stack_height(doc, ship)) + doc.px(2)
    doc.ensure(height)
    for column, x in ((bill, doc.left), (ship, doc.left + col_w + doc.px(16))):
        y = doc.y
        for index, (text, size, style, color) in enumerate(column):
            if index == 1:
                y += doc.px(2)  # mt-0.5 under the heading
            doc.text(x, y, col_w, text, size, style=style, color=color)
            y += doc.line(size)
    doc.y += height
    return True


_HEADER_BORDER = {"grid": ("box", 2, 300), "minimal": ("bottom", 1, 200)}
_ROW_BORDER = {"grid": ("box", 1, 200), "banded": ("bottom", 1, 50), "minimal": None}


def _table(doc: _Doc, invoice, settings: dict, item_images: dict | None) -> None:
    style = doc.theme["table"]
    keys = _columns(settings)
    total_weight = sum(COLUMNS[k][1] for k in keys)
    pad_x = doc.px(12 if style == "wide" else 6)
    # flex-grow with flex-basis 0: each cell keeps its padding, the rest is shared by weight.
    free = doc.width - len(keys) * 2 * pad_x
    widths = [free * COLUMNS[k][1] / total_weight + 2 * pad_x for k in keys]
    row_pad = doc.px(ROW_PAD_PX.get(doc.theme["base_template"], 6.0))
    head_size = doc.table_px * 0.62
    head_color = doc.primary if doc.theme["accent"] == "bold" else NEUTRAL[400]
    head_h = doc.px(12) + doc.line(head_size)
    # As in the frontend, the picture sits inside the product cell whenever
    # show_product_image is on; a "product_image" column itself only prints a dash.
    show_images = bool((settings.get("item_table") or {}).get("show_product_image") and item_images)
    img_px = 24.0
    row_h = 2 * row_pad + max(doc.line(doc.table_px), doc.px(img_px) if show_images else 0)

    def draw_header():
        y = doc.y
        kind, bpx, shade = _HEADER_BORDER.get(style, ("bottom", 2, 200))
        x = doc.left
        for key, w in zip(keys, widths):
            label = COLUMNS[key][0].upper()
            doc.text(x + pad_x, y, w - 2 * pad_x, doc.fit(label, w - 2 * pad_x, head_size, "B"), head_size,
                     style="B", color=head_color, align=COLUMNS[key][2], spacing_em=0.025, height=head_h)
            x += w
        if kind == "box":
            doc.box(doc.left, y, doc.width, head_h, border=NEUTRAL[shade], border_px=bpx)
        else:
            doc.rule(doc.left, y + head_h, doc.left + doc.width, bpx, NEUTRAL[shade])
        doc.y = y + head_h + doc.px(bpx)

    doc.ensure(head_h + row_h)
    draw_header()
    for index, item in enumerate(invoice.items):
        if doc.ensure(row_h):
            draw_header()  # repeat the column headings on every continuation page
        y = doc.y
        if style == "banded" and index % 2 == 1:
            doc.box(doc.left, y, doc.width, row_h, fill=_mix(NEUTRAL[50], WHITE, 0.7))
        x = doc.left
        for key, w in zip(keys, widths):
            inner = w - 2 * pad_x
            if key == "product":
                text_x, text_w = x + pad_x, inner
                if show_images and item.id in item_images:
                    drawn = doc.image(item_images[item.id], text_x, y + row_pad, doc.px(img_px), doc.px(img_px), align="L")
                    if drawn:
                        text_x += doc.px(img_px + 6)
                        text_w -= doc.px(img_px + 6)
                doc.text(text_x, y, text_w, doc.fit(_cell(item, key), text_w, doc.table_px), doc.table_px,
                         align="L", height=row_h)
            else:
                doc.text(x + pad_x, y, inner, doc.fit(_cell(item, key), inner, doc.table_px), doc.table_px,
                         align=COLUMNS[key][2], height=row_h)
            x += w
        border = _ROW_BORDER.get(style, ("bottom", 1, 100))
        if border and border[0] == "box":
            doc.box(doc.left, y, doc.width, row_h, border=NEUTRAL[border[2]], border_px=border[1])
        elif border:
            doc.rule(doc.left, y + row_h, doc.left + doc.width, border[1], NEUTRAL[border[2]])
        doc.y = y + row_h + doc.px(1)


def _totals(doc: _Doc, invoice, settings: dict, simple: bool = False) -> None:
    """SummaryTotals(). Every amount is the invoice's stored figure; Additional Charges and
    Round Off are shown whenever set, since the Total includes them. The simple format hides
    the Discount and Tax rows (InvoicePrintView.jsx's preset turns show_discount and
    show_tax_amount off); the Total still includes both."""
    fields = settings.get("fields") or {}
    style = doc.theme["totals"]
    rows = [("Subtotal", inr(invoice.subtotal))]
    if not simple and fields.get("show_discount", True) and invoice.discount:
        rows.append(("Discount", f"-{inr(invoice.discount)}"))
    if not simple and fields.get("show_tax_amount", True) and invoice.tax:
        rows.append(("Tax", inr(invoice.tax)))
    if invoice.additional_charges:
        rows.append(("Additional Charges", inr(invoice.additional_charges)))
    if invoice.round_off:
        rows.append(("Round Off", inr(invoice.round_off)))
    total = ("Total", inr(invoice.total))
    size, lh = doc.body_px, doc.line(doc.body_px)

    if style == "inline":
        parts = [f"{label}: {value}" for label, value in rows]
        pill = f"{total[0]}: {total[1]}"
        gap, pad_x, pad_y = doc.px(16), doc.px(8), doc.px(4)
        pill_w = doc.measure(pill, size, "B") + 2 * pad_x
        widths = [doc.measure(p, size) for p in parts]
        lines, current, used = [], [], 0.0
        for part, w in list(zip(parts, widths)) + [(pill, pill_w)]:
            need = w + (gap if current else 0)
            if current and used + need > doc.width:
                lines.append((current, used))
                current, used = [], 0.0
                need = w
            current.append((part, w))
            used += need
        lines.append((current, used))
        line_h = lh + 2 * pad_y
        doc.ensure(line_h * len(lines))
        for line, used in lines:
            x = doc.left + doc.width - used
            for part, w in line:
                if part == pill:
                    doc.box(x, doc.y, w, line_h, fill=doc.primary, radius_px=4)
                    doc.text(x, doc.y, w, part, size, style="B", color=WHITE, align="C", height=line_h)
                else:
                    doc.text(x, doc.y, w, part, size, height=line_h)
                x += w + gap
            doc.y += line_h
        return

    block_w = min(doc.px(224), doc.width)
    x0 = doc.left + doc.width - block_w
    frame = style in ("boxed", "grid")
    pad_x = doc.px(8) if frame else 0.0
    pad_y = doc.px(8 if style == "boxed" else 4) if frame else 0.0
    gap = doc.px(4)
    banded = style == "banded"
    total_h = lh + (doc.px(12) if banded else doc.px(4) + doc.px(1))
    height = 2 * pad_y + len(rows) * lh + len(rows) * gap + total_h
    doc.ensure(height)
    top = doc.y
    if frame:
        doc.box(x0, top, block_w, height, border=NEUTRAL[200], radius_px=8)
    y = top + pad_y
    inner_x, inner_w = x0 + pad_x, block_w - 2 * pad_x
    for label, value in rows:
        doc.text(inner_x, y, inner_w, label, size)
        doc.text(inner_x, y, inner_w, value, size, align="R")
        y += lh + gap
    if banded:
        doc.box(inner_x, y, inner_w, total_h, fill=doc.primary, radius_px=4)
        doc.text(inner_x + doc.px(8), y, inner_w - doc.px(16), total[0], size, style="B", color=WHITE, height=total_h)
        doc.text(inner_x + doc.px(8), y, inner_w - doc.px(16), total[1], size, style="B", color=WHITE, align="R", height=total_h)
    else:
        doc.rule(inner_x, y, inner_x + inner_w, 1, NEUTRAL[200])
        ty = y + doc.px(1) + doc.px(4)
        doc.text(inner_x, ty, inner_w, total[0], size, style="B", color=doc.primary)
        doc.text(inner_x, ty, inner_w, total[1], size, style="B", color=doc.primary, align="R")
    doc.y = top + height


def _footer(doc: _Doc, org, invoice, settings: dict, simple: bool, signature, qr, stamp) -> None:
    """InvoiceFooter(). The simple format hides payment details, terms, footer text and the
    signature (InvoicePrintView.jsx); the invoice note still prints."""
    fields = settings.get("fields") or {}
    payment = settings.get("payment_details") or {}
    footer = settings.get("footer") or {}
    show_bank = not simple and payment.get("show_bank_details", fields.get("show_bank_details", True))
    show_qr = not simple and payment.get("show_upi_qr", fields.get("show_upi_qr", True))
    show_terms = not simple and footer.get("show_terms", fields.get("show_terms", True))
    show_sig = not simple and footer.get("show_signature", fields.get("show_signature", True))
    show_stamp = footer.get("show_stamp", True)
    terms = footer.get("terms") or settings.get("terms")
    notes = getattr(invoice, "notes", None) or footer.get("notes") or settings.get("notes")
    footer_text = None if simple else (footer.get("footer_text") or settings.get("footer_text"))
    size, lh, gap = doc.body_px, doc.line(doc.body_px), doc.px(6)

    doc.ensure(doc.px(12) + lh)
    doc.rule(doc.left, doc.y, doc.left + doc.width, 1, NEUTRAL[200])
    doc.y += doc.px(12)

    def paragraph(label: str | None, text: str):
        content = f"{label}{text}" if label else text
        lines = doc.wrap(content, doc.width, size)
        for index, line in enumerate(lines):
            doc.ensure(lh)
            if index == 0 and label and line.startswith(label):
                doc.text(doc.left, doc.y, doc.width, label, size, style="B", color=NEUTRAL[700])
                lw = doc.measure(label, size, "B")
                doc.text(doc.left + lw, doc.y, doc.width - lw, line[len(label):], size, color=NEUTRAL[500])
            else:
                doc.text(doc.left, doc.y, doc.width, line, size, color=NEUTRAL[500])
            doc.y += lh
        doc.y += gap

    if show_bank and org is not None and getattr(org, "bank_name", None):
        bank = " | ".join(p for p in (
            f"Bank: {org.bank_name}",
            f"A/c {org.bank_account_details}" if getattr(org, "bank_account_details", None) else None,
            f"IFSC {org.bank_ifsc}" if getattr(org, "bank_ifsc", None) else None,
        ) if p)
        paragraph(None, bank)

    if show_qr:
        qr_side = doc.px(34 * 96 / 25.4)  # 34mm CSS inside the preview box
        label = "Scan to pay via UPI"
        upi = getattr(org, "upi_id", None) if org is not None else None
        block_h = doc.px(8) + lh + doc.px(8) + (qr_side if qr else lh) + (lh if upi else 0) + doc.px(8)
        doc.ensure(block_h)
        y = doc.y + doc.px(8)
        col_w = max(qr_side, doc.measure(label, size, "B"))
        doc.text(doc.left, y, col_w, label, size, color=NEUTRAL[700], align="C")
        y += lh + doc.px(8)
        if qr:
            x = doc.left + (col_w - qr_side) / 2
            doc.box(x, y, qr_side, qr_side, fill=WHITE, border=NEUTRAL[200])
            doc.image(qr, x + doc.px(8), y + doc.px(8), qr_side - doc.px(16), qr_side - doc.px(16))
            y += qr_side
        else:
            doc.text(doc.left, y, col_w, "Payment QR unavailable", size, color=NEUTRAL[500], align="C")
            y += lh
        if upi:
            doc.text(doc.left, y, col_w, f"UPI: {upi}", size, color=NEUTRAL[500], align="C")
            y += lh
        doc.y = y + doc.px(8) + gap

    if show_terms and terms:
        paragraph("Terms: ", terms)
    if notes:
        paragraph("Note: ", notes)
    if footer_text:
        paragraph(None, footer_text)

    if show_sig:
        sig_h, stamp_side = doc.px(36), doc.px(44)
        area_h = doc.px(12) + max(sig_h, stamp_side if (show_stamp and stamp) else 0)
        doc.ensure(area_h + lh)
        y = doc.y + doc.px(12)
        right = doc.left + doc.width
        sig_w = 0.0
        if signature:
            sig_w = doc.image(signature, right - doc.px(112), y + area_h - doc.px(12) - sig_h, doc.px(112), sig_h, align="R")
        if not sig_w:
            placeholder = "Signature"
            sig_w = doc.measure(placeholder, size, "I")
            doc.text(right - sig_w, y + area_h - doc.px(12) - lh, sig_w, placeholder, size, style="I", color=NEUTRAL[400])
        if show_stamp and stamp:
            doc.image(stamp, right - sig_w - doc.px(8) - stamp_side, y + area_h - doc.px(12) - stamp_side,
                      stamp_side, stamp_side, align="R")
        doc.y = y + area_h - doc.px(12)
        doc.text(doc.left, doc.y, doc.width, "Authorised Signatory", size, color=NEUTRAL[500], align="R")
        doc.y += lh


# ------------------------------------------------------------------ entry point


def render_regular_invoice(
    pdf: FPDF, org, customer, invoice, settings: dict, *, simple: bool,
    logo=None, signature=None, qr=None, stamp=None, letterhead=None, item_images=None,
) -> bytes:
    """Draw the whole regular invoice on `pdf` (a page already set up by
    pdf_docs._invoice_pdf_page) and return the PDF bytes."""
    theme = resolve_theme(settings)
    doc = _Doc(pdf, settings, theme)
    gap = doc.px(DENSITY_GAP_PX.get(theme["density"], 12.0))

    if letterhead:
        drawn_w = doc.image(letterhead, doc.left, doc.y, doc.width, doc.px(56))
        if drawn_w:
            from PIL import Image

            with Image.open(BytesIO(letterhead)) as img:
                iw, ih = img.size
            doc.y += min(doc.px(56), doc.width * ih / iw) + gap

    _header(doc, org, invoice, settings, logo)
    doc.y += gap
    if _party(doc, customer, invoice, settings):
        doc.y += gap
    _table(doc, invoice, settings, item_images)
    doc.y += gap
    _totals(doc, invoice, settings, simple)
    doc.y += gap
    _footer(doc, org, invoice, settings, simple, signature, qr, stamp)
    return bytes(pdf.output())
