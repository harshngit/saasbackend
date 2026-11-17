"""Line GST for orders and invoices -- one calculation, used everywhere a bill is priced.

Rules (agreed with QA, 17 Nov):

* An order-level (or bill-level) discount is a discount BEFORE tax. It is split across the
  lines in proportion to each line's value after its own line discount and before tax,
  rounded to the paisa, the last line taking the remainder so the shares add up to the
  discount exactly. GST is charged on what is left: taxable value = line value - share.
* Tax-inclusive pricing: when the product (or its variant) is priced tax-inclusive, the
  entered price already contains GST. The line's value before tax is price / (1 + rate);
  with no bill discount the customer pays exactly the entered price (GST = price - value).
  A bill discount then reduces the pre-tax value like on any other line.
* A line without a rate carries no GST (or the flat tax its caller gives it).

Amounts are Decimal, rounded half-up to 2 places.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")


def dec(value) -> Decimal:  # noqa: ANN001
    return Decimal(str(value or 0))


def money(value) -> Decimal:  # noqa: ANN001
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def effective_tax_inclusive(product, variant=None) -> bool:  # noqa: ANN001
    """The canonical flag: the variant's own setting when it has one, else the product's
    pricing (`pricing.tax_inclusive`); the old product columns only for a product that
    has no pricing row."""
    if variant is not None and getattr(variant, "tax_inclusive", None) is not None:
        return bool(variant.tax_inclusive)
    pricing = getattr(product, "pricing", None)
    if pricing is not None:
        return bool(pricing.tax_inclusive)
    return bool(getattr(product, "tax_inclusive", False) or getattr(product, "tax_inclusive_price", False))


def pre_tax_value(amount, rate, inclusive: bool) -> tuple[Decimal, Decimal | None]:  # noqa: ANN001
    """(value before tax, GST contained in the price) for an amount as entered. The second
    item is None unless the price was tax-inclusive."""
    amount = money(amount)
    r = dec(rate)
    if not inclusive or r <= 0:
        return amount, None
    value = money(amount / (1 + r / 100))
    return value, amount - value


def split_discount(values: list[Decimal], discount) -> list[Decimal]:  # noqa: ANN001
    """Shares of `discount` per line, proportional to `values`, summing to it exactly."""
    discount = money(discount)
    shares = [ZERO] * len(values)
    base = sum((v for v in values if v > 0), ZERO)
    if discount <= 0 or base <= 0:
        return shares
    discount = min(discount, base)
    positive = [i for i, v in enumerate(values) if v > 0]
    given = ZERO
    for i in positive[:-1]:
        shares[i] = money(discount * values[i] / base)
        given += shares[i]
    shares[positive[-1]] = discount - given
    return shares


@dataclass
class TaxedLine:
    value: Decimal            # before tax, after the line's own discount (stored as line_total)
    share: Decimal            # its part of the bill-level discount
    taxable: Decimal          # value - share
    tax: Decimal              # GST on the taxable value


def tax_lines(lines: list[dict], bill_discount=0) -> list[TaxedLine]:  # noqa: ANN001
    """`lines`: dicts with `value` (pre-tax, after line discount), `rate` (or None) and
    optionally `inclusive_tax` (from pre_tax_value) or `flat_tax` (a fixed amount for a
    line without a rate)."""
    values = [money(line["value"]) for line in lines]
    shares = split_discount(values, bill_discount)
    out = []
    for line, value, share in zip(lines, values, shares):
        taxable = value - share
        rate = line.get("rate")
        if rate is None:
            tax = money(line.get("flat_tax") or 0)
        elif share == 0 and line.get("inclusive_tax") is not None:
            tax = line["inclusive_tax"]         # exactly what the inclusive price contained
        else:
            tax = money(taxable * dec(rate) / 100)
        out.append(TaxedLine(value=value, share=share, taxable=taxable, tax=tax))
    return out


def gst_split(tax, rate, intra_state: bool | None) -> dict:  # noqa: ANN001
    """CGST + SGST (half each) inside one state, IGST between states. None when the
    supply type cannot be told (no state on either side): the whole tax is left unsplit."""
    tax = money(tax)
    r = dec(rate)
    if intra_state is None:
        return {"supply_type": None, "cgst_rate": None, "cgst_amount": None, "sgst_rate": None,
                "sgst_amount": None, "igst_rate": None, "igst_amount": None}
    if intra_state:
        cgst = money(tax / 2)
        return {"supply_type": "intra_state", "cgst_rate": float(r / 2), "cgst_amount": float(cgst),
                "sgst_rate": float(r / 2), "sgst_amount": float(tax - cgst),
                "igst_rate": 0.0, "igst_amount": 0.0}
    return {"supply_type": "inter_state", "cgst_rate": 0.0, "cgst_amount": 0.0, "sgst_rate": 0.0,
            "sgst_amount": 0.0, "igst_rate": float(r), "igst_amount": float(tax)}


# GST state codes: the first two digits of a GSTIN.
_STATE_CODES = {
    "01": "jammu and kashmir", "02": "himachal pradesh", "03": "punjab", "04": "chandigarh",
    "05": "uttarakhand", "06": "haryana", "07": "delhi", "08": "rajasthan", "09": "uttar pradesh",
    "10": "bihar", "11": "sikkim", "12": "arunachal pradesh", "13": "nagaland", "14": "manipur",
    "15": "mizoram", "16": "tripura", "17": "meghalaya", "18": "assam", "19": "west bengal",
    "20": "jharkhand", "21": "odisha", "22": "chhattisgarh", "23": "madhya pradesh", "24": "gujarat",
    "25": "daman and diu", "26": "dadra and nagar haveli and daman and diu", "27": "maharashtra",
    "28": "andhra pradesh", "29": "karnataka", "30": "goa", "31": "lakshadweep", "32": "kerala",
    "33": "tamil nadu", "34": "puducherry", "35": "andaman and nicobar islands", "36": "telangana",
    "37": "andhra pradesh", "38": "ladakh", "97": "other territory",
}


def state_of(gstin: str | None, state: str | None) -> str | None:
    """The state a party is registered in: from the GSTIN's state code when there is a
    valid one, else the state written on the record. Lower-case, for comparison."""
    code = (gstin or "").strip()[:2]
    if code.isdigit() and code in _STATE_CODES:
        return _STATE_CODES[code]
    s = (state or "").strip().lower()
    return s or None


def is_intra_state(seller_gstin, seller_state, buyer_gstin, buyer_state) -> bool | None:  # noqa: ANN001
    seller, buyer = state_of(seller_gstin, seller_state), state_of(buyer_gstin, buyer_state)
    if seller is None or buyer is None:
        return None
    return seller == buyer
