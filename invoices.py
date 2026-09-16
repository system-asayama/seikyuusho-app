"""請求書の作成・編集・発行・印刷・PDF 出力。"""
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import current_user, login_required
from billing import line_amount, next_invoice_number, parse_decimal, summarize
from models import (
    INVOICE_STATUSES,
    STATUS_DRAFT,
    STATUS_ISSUED,
    STATUS_LABELS,
    STATUS_PAID,
    TAX_RATES,
    Client,
    CompanySetting,
    Invoice,
    InvoiceItem,
    db,
)
from pdf import render_invoice_pdf

bp = Blueprint("invoices", __name__, url_prefix="/invoices")


def _parse_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_items(form) -> tuple[list[InvoiceItem], str | None]:
    """フォームの明細行を InvoiceItem の一覧に変換する。"""
    descriptions = form.getlist("item_description")
    quantities = form.getlist("item_quantity")
    units = form.getlist("item_unit")
    prices = form.getlist("item_unit_price")
    rates = form.getlist("item_tax_rate")

    items: list[InvoiceItem] = []
    for idx, description in enumerate(descriptions):
        description = (description or "").strip()
        qty_raw = quantities[idx] if idx < len(quantities) else ""
        price_raw = prices[idx] if idx < len(prices) else ""
        if not description and not (qty_raw or "").strip() and not (price_raw or "").strip():
            continue  # 完全に空の行は無視
        if not description:
            return [], f"{len(items) + 1}行目の品目を入力してください。"
        try:
            quantity = parse_decimal(qty_raw, default=None)
            unit_price = parse_decimal(price_raw, default=None)
        except ValueError:
            return [], f"{len(items) + 1}行目の数量または単価が数値ではありません。"
        if quantity is None or unit_price is None:
            return [], f"{len(items) + 1}行目の数量と単価を入力してください。"
        try:
            tax_rate = int(rates[idx]) if idx < len(rates) else 10
        except ValueError:
            tax_rate = 10
        if tax_rate not in TAX_RATES:
            return [], f"{len(items) + 1}行目の税率が不正です。"

        items.append(
            InvoiceItem(
                position=len(items),
                description=description[:200],
                quantity=quantity,
                unit=(units[idx] if idx < len(units) else "")[:20].strip(),
                unit_price=unit_price,
                tax_rate=tax_rate,
                amount=line_amount(quantity, unit_price),
            )
        )
    if not items:
        return [], "明細を1行以上入力してください。"
    return items, None


def _apply_form(invoice: Invoice, company: CompanySetting) -> str | None:
    """フォーム値を invoice に反映し、金額を再計算する。問題があればエラーを返す。"""
    client_id = request.form.get("client_id", type=int)
    client = db.session.get(Client, client_id) if client_id else None
    if client is None:
        return "取引先を選択してください。"

    number = (request.form.get("number") or "").strip()
    if not number:
        return "請求書番号を入力してください。"
    dup = db.session.scalar(db.select(Invoice).where(Invoice.number == number, Invoice.id != (invoice.id or 0)))
    if dup is not None:
        return f"請求書番号「{number}」は既に使われています。"

    issue_date = _parse_date(request.form.get("issue_date"))
    if issue_date is None:
        return "発行日を正しく入力してください。"
    due_raw = (request.form.get("due_date") or "").strip()
    due_date = _parse_date(due_raw)
    if due_raw and due_date is None:
        return "支払期限を正しく入力してください。"

    items, error = _parse_items(request.form)
    if error:
        return error

    invoice.client = client
    invoice.number = number
    invoice.issue_date = issue_date
    invoice.due_date = due_date
    invoice.subject = (request.form.get("subject") or "").strip()[:200]
    invoice.notes = (request.form.get("notes") or "").strip()
    invoice.items = items
    summary = summarize(items, company.tax_rounding)
    invoice.subtotal = summary.subtotal
    invoice.tax = summary.tax
    invoice.total = summary.total
    return None


def _clients_for_select():
    return db.session.scalars(db.select(Client).order_by(Client.name.asc())).all()


def _render_form(invoice: Invoice, company: CompanySetting, is_new: bool):
    return render_template(
        "invoices/form.html",
        invoice=invoice,
        clients=_clients_for_select(),
        company=company,
        tax_rates=TAX_RATES,
        is_new=is_new,
    )


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------
@bp.route("")
@login_required
def index():
    status = request.args.get("status") or ""
    client_id = request.args.get("client_id", type=int)
    q = (request.args.get("q") or "").strip()

    query = db.select(Invoice).join(Client)
    if status in INVOICE_STATUSES:
        query = query.where(Invoice.status == status)
    if client_id:
        query = query.where(Invoice.client_id == client_id)
    if q:
        query = query.where(Invoice.number.contains(q) | Invoice.subject.contains(q) | Client.name.contains(q))
    query = query.order_by(Invoice.issue_date.desc(), Invoice.id.desc())
    invoices = db.session.scalars(query).all()

    return render_template(
        "invoices/index.html",
        invoices=invoices,
        clients=_clients_for_select(),
        status=status,
        client_id=client_id,
        q=q,
        status_labels=STATUS_LABELS,
        total_sum=sum(i.total for i in invoices),
    )


# ---------------------------------------------------------------------------
# 作成 / 編集
# ---------------------------------------------------------------------------
@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    company = CompanySetting.get()
    if request.method == "POST":
        invoice = Invoice(status=STATUS_DRAFT, created_by=current_user())
        error = _apply_form(invoice, company)
        if error:
            flash(error, "error")
            # 入力値を保持して再表示するため、フォーム値から仮のオブジェクトを作る
            invoice.items, _ = _parse_items(request.form)
            invoice.number = (request.form.get("number") or "").strip()
            invoice.issue_date = _parse_date(request.form.get("issue_date")) or date.today()
            invoice.due_date = _parse_date(request.form.get("due_date"))
            invoice.subject = request.form.get("subject") or ""
            invoice.notes = request.form.get("notes") or ""
            invoice.client_id = request.form.get("client_id", type=int)
            return _render_form(invoice, company, is_new=True)
        db.session.add(invoice)
        db.session.commit()
        flash(f"請求書 {invoice.number} を作成しました。", "success")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))

    today = date.today()
    invoice = Invoice(
        number=next_invoice_number(company.invoice_prefix, today),
        issue_date=today,
        due_date=today + timedelta(days=company.default_due_days),
        notes=company.default_notes,
        client_id=request.args.get("client_id", type=int),
    )
    invoice.items = [InvoiceItem(quantity=1, unit_price=0, tax_rate=10)]
    return _render_form(invoice, company, is_new=True)


@bp.route("/<int:invoice_id>")
@login_required
def detail(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    company = CompanySetting.get()
    return render_template(
        "invoices/detail.html",
        invoice=invoice,
        company=company,
        summary=summarize(invoice.items, company.tax_rounding),
        today_ymd=date.today().isoformat(),
    )


@bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    company = CompanySetting.get()
    if invoice.is_locked:
        flash("入金済の請求書は編集できません。編集する場合は「発行済に戻す」を行ってください。", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))

    if request.method == "POST":
        error = _apply_form(invoice, company)
        if error:
            db.session.rollback()
            flash(error, "error")
            return redirect(url_for("invoices.edit", invoice_id=invoice.id))
        db.session.commit()
        flash(f"請求書 {invoice.number} を保存しました。", "success")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))

    return _render_form(invoice, company, is_new=False)


# ---------------------------------------------------------------------------
# ステータス操作 / 複製 / 削除
# ---------------------------------------------------------------------------
@bp.route("/<int:invoice_id>/status", methods=["POST"])
@login_required
def change_status(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    action = request.form.get("action")

    if action == "issue" and invoice.status == STATUS_DRAFT:
        invoice.status = STATUS_ISSUED
        message = f"請求書 {invoice.number} を発行済にしました。"
    elif action == "paid" and invoice.status == STATUS_ISSUED:
        invoice.status = STATUS_PAID
        invoice.paid_at = _parse_date(request.form.get("paid_at")) or date.today()
        message = f"請求書 {invoice.number} を入金済にしました。"
    elif action == "reopen" and invoice.status == STATUS_PAID:
        invoice.status = STATUS_ISSUED
        invoice.paid_at = None
        message = f"請求書 {invoice.number} を発行済に戻しました。"
    elif action == "draft" and invoice.status == STATUS_ISSUED:
        invoice.status = STATUS_DRAFT
        message = f"請求書 {invoice.number} を下書きに戻しました。"
    else:
        flash("その操作は現在のステータスでは行えません。", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))

    db.session.commit()
    flash(message, "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice.id))


@bp.route("/<int:invoice_id>/duplicate", methods=["POST"])
@login_required
def duplicate(invoice_id):
    source = db.session.get(Invoice, invoice_id) or abort(404)
    company = CompanySetting.get()
    today = date.today()
    copy = Invoice(
        number=next_invoice_number(company.invoice_prefix, today),
        client_id=source.client_id,
        issue_date=today,
        due_date=today + timedelta(days=company.default_due_days),
        subject=source.subject,
        notes=source.notes,
        status=STATUS_DRAFT,
        created_by=current_user(),
        subtotal=source.subtotal,
        tax=source.tax,
        total=source.total,
    )
    copy.items = [
        InvoiceItem(
            position=item.position,
            description=item.description,
            quantity=item.quantity,
            unit=item.unit,
            unit_price=item.unit_price,
            tax_rate=item.tax_rate,
            amount=item.amount,
        )
        for item in source.items
    ]
    db.session.add(copy)
    db.session.commit()
    flash(f"請求書 {source.number} を複製して {copy.number} を作成しました。", "success")
    return redirect(url_for("invoices.edit", invoice_id=copy.id))


@bp.route("/<int:invoice_id>/delete", methods=["POST"])
@login_required
def delete(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    user = current_user()
    if invoice.status != STATUS_DRAFT and not user.is_admin:
        flash("発行済・入金済の請求書を削除できるのは管理者のみです。", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice.id))
    number = invoice.number
    db.session.delete(invoice)
    db.session.commit()
    flash(f"請求書 {number} を削除しました。", "success")
    return redirect(url_for("invoices.index"))


# ---------------------------------------------------------------------------
# 印刷 / PDF
# ---------------------------------------------------------------------------
@bp.route("/<int:invoice_id>/print")
@login_required
def print_view(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    company = CompanySetting.get()
    return render_template(
        "invoices/print.html",
        invoice=invoice,
        company=company,
        summary=summarize(invoice.items, company.tax_rounding),
    )


@bp.route("/<int:invoice_id>/pdf")
@login_required
def pdf(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    company = CompanySetting.get()
    data = render_invoice_pdf(invoice, company, summarize(invoice.items, company.tax_rounding))
    filename = f"invoice_{invoice.number}.pdf".replace("/", "-")
    disposition = "attachment" if request.args.get("download") else "inline"
    return Response(
        data,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}"},
    )
