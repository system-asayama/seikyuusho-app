"""請求金額の計算・採番・集計ロジック。"""
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import func

from models import STATUS_DRAFT, STATUS_ISSUED, STATUS_PAID, TAX_RATES, Invoice, db

_ROUNDING = {"floor": ROUND_FLOOR, "round": ROUND_HALF_UP, "ceil": ROUND_CEILING}


def parse_decimal(value: str | None, default: Decimal = Decimal("0")) -> Decimal:
    """フォーム入力を Decimal に変換する。カンマや全角数字も受け付ける。"""
    if value is None:
        return default
    text = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "")
    text = text.translate(str.maketrans("０１２３４５６７８９．－", "0123456789.-"))
    if not text:
        return default
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"数値として解釈できません: {value}") from exc


def round_yen(value: Decimal, mode: str = "floor") -> int:
    return int(value.quantize(Decimal("1"), rounding=_ROUNDING.get(mode, ROUND_FLOOR)))


def line_amount(quantity: Decimal, unit_price: Decimal) -> int:
    """明細行の税抜金額。円未満は切り捨て。"""
    return round_yen(quantity * unit_price, "floor")


@dataclass
class RateSummary:
    rate: int
    subtotal: int
    tax: int

    @property
    def total(self) -> int:
        return self.subtotal + self.tax


@dataclass
class InvoiceSummary:
    subtotal: int
    tax: int
    total: int
    by_rate: list[RateSummary]


def summarize(items, rounding: str = "floor") -> InvoiceSummary:
    """明細から小計・消費税・合計を求める。

    適格請求書の要件に合わせ、消費税は税率ごとに合算してから1回だけ端数処理する。
    items は .amount(int) と .tax_rate(int) を持つオブジェクトの列。
    """
    buckets: dict[int, int] = {}
    for item in items:
        buckets[item.tax_rate] = buckets.get(item.tax_rate, 0) + int(item.amount)

    by_rate = []
    for rate in sorted(buckets, key=lambda r: (r not in TAX_RATES, -r)):
        base = buckets[rate]
        tax = round_yen(Decimal(base) * Decimal(rate) / Decimal(100), rounding)
        by_rate.append(RateSummary(rate=rate, subtotal=base, tax=tax))

    subtotal = sum(b.subtotal for b in by_rate)
    tax = sum(b.tax for b in by_rate)
    return InvoiceSummary(subtotal=subtotal, tax=tax, total=subtotal + tax, by_rate=by_rate)


def next_invoice_number(prefix: str, issue_date: date | None = None) -> str:
    """`<prefix><西暦>-<年内連番4桁>` 形式で次の請求書番号を返す。"""
    year = (issue_date or date.today()).year
    head = f"{prefix}{year}-"
    numbers = db.session.scalars(
        db.select(Invoice.number).where(Invoice.number.like(f"{head}%"))
    ).all()
    max_seq = 0
    for number in numbers:
        tail = number[len(head):]
        if tail.isdigit():
            max_seq = max(max_seq, int(tail))
    return f"{head}{max_seq + 1:04d}"


@dataclass
class DashboardStats:
    counts: dict[str, int]
    unpaid_total: int
    overdue_count: int
    month_total: int
    recent: list[Invoice]


def dashboard_stats(limit: int = 8) -> DashboardStats:
    today = date.today()
    month_start = today.replace(day=1)

    counts = {}
    for status, count in db.session.execute(
        db.select(Invoice.status, func.count(Invoice.id)).group_by(Invoice.status)
    ).all():
        counts[status] = count
    for status in (STATUS_DRAFT, STATUS_ISSUED, STATUS_PAID):
        counts.setdefault(status, 0)

    unpaid_total = db.session.scalar(
        db.select(func.coalesce(func.sum(Invoice.total), 0)).where(Invoice.status == STATUS_ISSUED)
    )
    overdue_count = db.session.scalar(
        db.select(func.count(Invoice.id)).where(
            Invoice.status == STATUS_ISSUED, Invoice.due_date.is_not(None), Invoice.due_date < today
        )
    )
    month_total = db.session.scalar(
        db.select(func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.status != STATUS_DRAFT, Invoice.issue_date >= month_start
        )
    )
    recent = db.session.scalars(
        db.select(Invoice).order_by(Invoice.updated_at.desc(), Invoice.id.desc()).limit(limit)
    ).all()
    return DashboardStats(
        counts=counts,
        unpaid_total=int(unpaid_total or 0),
        overdue_count=int(overdue_count or 0),
        month_total=int(month_total or 0),
        recent=recent,
    )
