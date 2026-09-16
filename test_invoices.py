"""請求書・取引先・自社情報のテスト。

実行: pytest
"""
import warnings
from datetime import date
from decimal import Decimal

import pytest

from app import create_app
from billing import line_amount, next_invoice_number, parse_decimal, round_yen, summarize
from models import STATUS_DRAFT, STATUS_ISSUED, STATUS_PAID, Client, CompanySetting, Invoice, InvoiceItem, db

ADMIN_PW = "admin-pass-123"
USER_PW = "user-pass-123"

# SQLite は Decimal をネイティブに扱えないという SQLAlchemy の注意喚起（本番は PostgreSQL）
warnings.filterwarnings("ignore", message=".*does \\*not\\* support Decimal objects natively.*")


@pytest.fixture()
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:", "SECRET_KEY": "test"})
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def admin(app):
    """管理者としてログイン済みのクライアント。自社情報も登録済み。"""
    c = app.test_client()
    c.post("/setup", data={"username": "admin", "display_name": "管理者", "password": ADMIN_PW, "confirm": ADMIN_PW})
    c.post(
        "/company",
        data={
            "name": "株式会社テスト",
            "postal_code": "100-0001",
            "address": "東京都千代田区1-1",
            "tel": "03-0000-0000",
            "email": "info@example.com",
            "registration_number": "T1234567890123",
            "bank_info": "テスト銀行 本店 普通 1234567",
            "invoice_prefix": "INV-",
            "tax_rounding": "floor",
            "default_due_days": "30",
            "default_notes": "振込手数料はご負担ください。",
        },
    )
    return c


@pytest.fixture()
def user(app, admin):
    """利用者としてログイン済みの別クライアント。"""
    admin.post("/admin/users/create", data={"username": "taro", "display_name": "太郎", "password": USER_PW, "role": "user"})
    c = app.test_client()
    c.post("/login", data={"username": "taro", "password": USER_PW})
    return c


def _create_client(client, name="テスト商事"):
    res = client.post(
        "/clients/new",
        data={"name": name, "honorific": "御中", "postal_code": "150-0001", "address": "東京都渋谷区2-2", "contact_name": "山田"},
    )
    assert res.status_code == 302
    with client.application.app_context():
        return Client.query.filter_by(name=name).first().id


def _invoice_form(client_id, number="INV-2026-0001", items=None, **extra):
    items = items or [("デザイン費", "1", "式", "100000", "10"), ("書籍", "2", "冊", "1500", "8")]
    data = {
        "client_id": str(client_id),
        "number": number,
        "issue_date": "2026-09-16",
        "due_date": "2026-10-16",
        "subject": "9月分",
        "notes": "備考です",
        "item_description": [i[0] for i in items],
        "item_quantity": [i[1] for i in items],
        "item_unit": [i[2] for i in items],
        "item_unit_price": [i[3] for i in items],
        "item_tax_rate": [i[4] for i in items],
    }
    data.update(extra)
    return data


def _create_invoice(client, client_id, **kw):
    res = client.post("/invoices/new", data=_invoice_form(client_id, **kw))
    assert res.status_code == 302, res.get_data(as_text=True)
    return int(res.headers["Location"].rstrip("/").split("/")[-1])


# ---------------------------------------------------------------------------
# 計算ロジック
# ---------------------------------------------------------------------------
def test_parse_decimal_accepts_japanese_input():
    assert parse_decimal("1,000") == Decimal("1000")
    assert parse_decimal("￥１２３．５") == Decimal("123.5")
    assert parse_decimal("") == Decimal("0")
    assert parse_decimal("", default=None) is None
    with pytest.raises(ValueError):
        parse_decimal("abc")


def test_round_yen_modes():
    assert round_yen(Decimal("10.5"), "floor") == 10
    assert round_yen(Decimal("10.5"), "round") == 11
    assert round_yen(Decimal("10.1"), "ceil") == 11
    assert round_yen(Decimal("10.4"), "round") == 10


def test_line_amount_floors():
    assert line_amount(Decimal("1.5"), Decimal("333")) == 499  # 499.5 → 499
    assert line_amount(Decimal("3"), Decimal("1000")) == 3000


def test_summarize_groups_by_rate_and_rounds_once():
    items = [
        InvoiceItem(amount=1001, tax_rate=10),
        InvoiceItem(amount=1001, tax_rate=10),
        InvoiceItem(amount=333, tax_rate=8),
        InvoiceItem(amount=500, tax_rate=0),
    ]
    s = summarize(items, "floor")
    assert s.subtotal == 2835
    # 10%: 2002 * 0.1 = 200.2 → 200（行ごとではなく合算後に1回だけ端数処理）
    # 8%: 333 * 0.08 = 26.64 → 26
    assert [(r.rate, r.subtotal, r.tax) for r in s.by_rate] == [(10, 2002, 200), (8, 333, 26), (0, 500, 0)]
    assert s.tax == 226
    assert s.total == 3061

    s2 = summarize(items, "round")
    assert s2.tax == 200 + 27


def test_next_invoice_number(app, admin):
    with app.app_context():
        assert next_invoice_number("INV-", date(2026, 9, 16)) == "INV-2026-0001"
        cid = _create_client(admin)
    _create_invoice(admin, cid, number="INV-2026-0007")
    with app.app_context():
        assert next_invoice_number("INV-", date(2026, 9, 16)) == "INV-2026-0008"
        assert next_invoice_number("INV-", date(2027, 1, 1)) == "INV-2027-0001"
        assert next_invoice_number("X-", date(2026, 1, 1)) == "X-2026-0001"


# ---------------------------------------------------------------------------
# 自社情報
# ---------------------------------------------------------------------------
def test_company_settings_saved_and_admin_only(app, admin, user):
    with app.app_context():
        c = CompanySetting.get()
        assert c.name == "株式会社テスト"
        assert c.registration_number == "T1234567890123"
        assert c.is_configured

    # 利用者は自社情報ページへ入れない
    res = user.get("/company")
    assert res.status_code == 302 and res.headers["Location"].endswith("/dashboard")
    res = user.post("/company", data={"name": "乗っ取り"})
    assert res.status_code == 302
    with app.app_context():
        assert CompanySetting.get().name == "株式会社テスト"


def test_setup_redirects_to_company_settings(app):
    c = app.test_client()
    res = c.post("/setup", data={"username": "admin", "password": ADMIN_PW, "confirm": ADMIN_PW})
    assert res.headers["Location"].endswith("/company")


# ---------------------------------------------------------------------------
# 取引先
# ---------------------------------------------------------------------------
def test_client_crud_and_delete_guard(app, admin, user):
    cid = _create_client(user)  # 利用者も登録できる
    res = user.get("/clients")
    assert "テスト商事 御中" in res.get_data(as_text=True)

    user.post(f"/clients/{cid}/edit", data={"name": "テスト商事", "honorific": "様", "address": "大阪"})
    with app.app_context():
        c = db.session.get(Client, cid)
        assert c.honorific == "様" and c.address == "大阪"

    # 請求書があると削除できない
    _create_invoice(admin, cid)
    admin.post(f"/clients/{cid}/delete")
    with app.app_context():
        assert db.session.get(Client, cid) is not None

    cid2 = _create_client(admin, name="削除できる社")
    admin.post(f"/clients/{cid2}/delete")
    with app.app_context():
        assert db.session.get(Client, cid2) is None


def test_client_requires_name(app, admin):
    res = admin.post("/clients/new", data={"name": "  "}, follow_redirects=True)
    assert "取引先名を入力" in res.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 請求書
# ---------------------------------------------------------------------------
def test_create_invoice_computes_totals(app, admin):
    cid = _create_client(admin)
    iid = _create_invoice(admin, cid)
    with app.app_context():
        inv = db.session.get(Invoice, iid)
        assert inv.number == "INV-2026-0001"
        assert inv.status == STATUS_DRAFT
        assert inv.client_id == cid
        assert inv.created_by.username == "admin"
        assert [i.description for i in inv.items] == ["デザイン費", "書籍"]
        assert inv.items[1].amount == 3000
        # 10%: 100000 → 10000, 8%: 3000 → 240
        assert (inv.subtotal, inv.tax, inv.total) == (103000, 10240, 113240)

    html = admin.get(f"/invoices/{iid}").get_data(as_text=True)
    assert "¥113,240" in html and "8%対象" in html and "テスト商事" in html


def test_new_invoice_form_prefills_defaults(app, admin):
    _create_client(admin)
    html = admin.get("/invoices/new").get_data(as_text=True)
    assert 'value="INV-2026-0001"' in html or "INV-" in html
    assert "振込手数料はご負担ください。" in html  # 備考の既定文


def test_invoice_validation_errors(app, admin):
    cid = _create_client(admin)
    # 明細なし
    res = admin.post("/invoices/new", data=_invoice_form(cid, items=[("", "", "", "", "10")]))
    assert res.status_code == 200 and "明細を1行以上" in res.get_data(as_text=True)
    # 数値でない
    res = admin.post("/invoices/new", data=_invoice_form(cid, items=[("A", "abc", "", "100", "10")]))
    assert "数値ではありません" in res.get_data(as_text=True)
    # 取引先なし
    res = admin.post("/invoices/new", data=_invoice_form(9999))
    assert "取引先を選択" in res.get_data(as_text=True)
    # 番号重複
    _create_invoice(admin, cid, number="DUP-1")
    res = admin.post("/invoices/new", data=_invoice_form(cid, number="DUP-1"))
    assert "既に使われています" in res.get_data(as_text=True)
    with app.app_context():
        assert Invoice.query.count() == 1


def test_edit_invoice_replaces_items(app, admin):
    cid = _create_client(admin)
    iid = _create_invoice(admin, cid)
    res = admin.post(
        f"/invoices/{iid}/edit",
        data=_invoice_form(cid, items=[("保守費", "3", "月", "20000", "10")], subject="変更後"),
    )
    assert res.status_code == 302
    with app.app_context():
        inv = db.session.get(Invoice, iid)
        assert inv.subject == "変更後"
        assert len(inv.items) == 1 and inv.items[0].amount == 60000
        assert inv.total == 66000
        assert InvoiceItem.query.count() == 1  # 古い明細は消えている


def test_status_transitions_and_lock(app, admin):
    cid = _create_client(admin)
    iid = _create_invoice(admin, cid)

    admin.post(f"/invoices/{iid}/status", data={"action": "paid"})  # 下書きからは入金済にできない
    with app.app_context():
        assert db.session.get(Invoice, iid).status == STATUS_DRAFT

    admin.post(f"/invoices/{iid}/status", data={"action": "issue"})
    admin.post(f"/invoices/{iid}/status", data={"action": "paid", "paid_at": "2026-10-01"})
    with app.app_context():
        inv = db.session.get(Invoice, iid)
        assert inv.status == STATUS_PAID and inv.paid_at == date(2026, 10, 1)

    # 入金済は編集不可
    res = admin.get(f"/invoices/{iid}/edit")
    assert res.status_code == 302
    res = admin.post(f"/invoices/{iid}/edit", data=_invoice_form(cid, subject="改ざん"))
    with app.app_context():
        assert db.session.get(Invoice, iid).subject == "9月分"

    admin.post(f"/invoices/{iid}/status", data={"action": "reopen"})
    with app.app_context():
        inv = db.session.get(Invoice, iid)
        assert inv.status == STATUS_ISSUED and inv.paid_at is None


def test_duplicate_invoice(app, admin):
    cid = _create_client(admin)
    iid = _create_invoice(admin, cid)
    res = admin.post(f"/invoices/{iid}/duplicate")
    assert res.status_code == 302 and "/edit" in res.headers["Location"]
    with app.app_context():
        copies = Invoice.query.order_by(Invoice.id).all()
        assert len(copies) == 2
        src, dup = copies
        assert dup.number != src.number
        assert dup.status == STATUS_DRAFT
        assert dup.total == src.total
        assert [i.description for i in dup.items] == [i.description for i in src.items]


def test_delete_permissions(app, admin, user):
    cid = _create_client(admin)
    draft_id = _create_invoice(user, cid, number="D-1")
    issued_id = _create_invoice(user, cid, number="I-1")
    admin.post(f"/invoices/{issued_id}/status", data={"action": "issue"})

    # 利用者: 下書きは削除可、発行済は不可
    user.post(f"/invoices/{issued_id}/delete")
    with app.app_context():
        assert db.session.get(Invoice, issued_id) is not None
    user.post(f"/invoices/{draft_id}/delete")
    with app.app_context():
        assert db.session.get(Invoice, draft_id) is None

    # 管理者は発行済も削除可
    admin.post(f"/invoices/{issued_id}/delete")
    with app.app_context():
        assert db.session.get(Invoice, issued_id) is None
        assert InvoiceItem.query.count() == 0


def test_invoice_list_filters(app, admin):
    cid = _create_client(admin)
    cid2 = _create_client(admin, name="別の会社")
    a = _create_invoice(admin, cid, number="A-1", subject="あ")
    _create_invoice(admin, cid2, number="B-1", subject="い")
    admin.post(f"/invoices/{a}/status", data={"action": "issue"})

    html = admin.get("/invoices").get_data(as_text=True)
    assert "A-1" in html and "B-1" in html
    html = admin.get("/invoices?status=issued").get_data(as_text=True)
    assert "A-1" in html and "B-1" not in html
    html = admin.get(f"/invoices?client_id={cid2}").get_data(as_text=True)
    assert "B-1" in html and "A-1" not in html
    html = admin.get("/invoices?q=別の").get_data(as_text=True)
    assert "B-1" in html and "A-1" not in html


def test_print_view_and_pdf(app, admin):
    cid = _create_client(admin)
    iid = _create_invoice(admin, cid)

    html = admin.get(f"/invoices/{iid}/print").get_data(as_text=True)
    assert "請求書" in html and "株式会社テスト" in html and "T1234567890123" in html
    assert "¥113,240" in html and "軽減税率" in html

    res = admin.get(f"/invoices/{iid}/pdf")
    assert res.status_code == 200
    assert res.mimetype == "application/pdf"
    assert res.data[:5] == b"%PDF-"
    assert "inline" in res.headers["Content-Disposition"]

    res = admin.get(f"/invoices/{iid}/pdf?download=1")
    assert "attachment" in res.headers["Content-Disposition"]


def test_dashboard_stats(app, admin):
    cid = _create_client(admin)
    today = date.today().isoformat()
    a = _create_invoice(admin, cid, number="A", issue_date=today, due_date="2000-01-01")
    _create_invoice(admin, cid, number="B", issue_date=today)
    admin.post(f"/invoices/{a}/status", data={"action": "issue"})

    html = admin.get("/dashboard").get_data(as_text=True)
    assert "¥113,240" in html  # 今月の請求額 / 未入金
    assert "期限超過" in html
    assert "1 / 1 / 0" in html  # 下書き / 発行済 / 入金済


def test_requires_login(app, admin):
    anon = app.test_client()
    for path in ("/invoices", "/invoices/new", "/clients", "/dashboard"):
        res = anon.get(path)
        assert res.status_code == 302 and "/login" in res.headers["Location"], path
