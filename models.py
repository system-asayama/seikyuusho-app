"""データベースモデル定義。"""
from datetime import date, datetime, timezone
from decimal import Decimal

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow() -> datetime:
    """タイムゾーン無しの UTC 現在時刻（DB の DateTime 列と揃える）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# ユーザー
# ---------------------------------------------------------------------------
ROLE_ADMIN = "admin"  # 管理者
ROLE_USER = "user"    # 利用者
ROLES = (ROLE_ADMIN, ROLE_USER)
ROLE_LABELS = {ROLE_ADMIN: "管理者", ROLE_USER: "利用者"}


class User(db.Model):
    """ログインユーザー。admin / user の2種類のロールを持つ。"""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    display_name = db.Column(db.String(120), nullable=False, default="")
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_USER)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime, nullable=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def name(self) -> str:
        return self.display_name or self.username

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<User {self.username} ({self.role})>"


# ---------------------------------------------------------------------------
# 自社情報（請求書の発行者）
# ---------------------------------------------------------------------------
TAX_ROUNDINGS = ("floor", "round", "ceil")
TAX_ROUNDING_LABELS = {"floor": "切り捨て", "round": "四捨五入", "ceil": "切り上げ"}


class CompanySetting(db.Model):
    """自社情報。1行だけ存在する。"""

    __tablename__ = "company_settings"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, default="")
    postal_code = db.Column(db.String(20), nullable=False, default="")
    address = db.Column(db.String(255), nullable=False, default="")
    tel = db.Column(db.String(40), nullable=False, default="")
    email = db.Column(db.String(120), nullable=False, default="")
    representative = db.Column(db.String(80), nullable=False, default="")
    # 適格請求書発行事業者登録番号（T + 13桁）
    registration_number = db.Column(db.String(20), nullable=False, default="")
    bank_info = db.Column(db.Text, nullable=False, default="")
    invoice_prefix = db.Column(db.String(20), nullable=False, default="INV-")
    tax_rounding = db.Column(db.String(10), nullable=False, default="floor")
    default_due_days = db.Column(db.Integer, nullable=False, default=30)
    default_notes = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    @classmethod
    def get(cls) -> "CompanySetting":
        row = cls.query.first()
        if row is None:
            row = cls()
            db.session.add(row)
            db.session.commit()
        return row

    @property
    def is_configured(self) -> bool:
        return bool(self.name)


# ---------------------------------------------------------------------------
# 取引先
# ---------------------------------------------------------------------------
HONORIFICS = ("御中", "様", "")


class Client(db.Model):
    """請求先（取引先）。"""

    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    honorific = db.Column(db.String(10), nullable=False, default="御中")
    postal_code = db.Column(db.String(20), nullable=False, default="")
    address = db.Column(db.String(255), nullable=False, default="")
    contact_name = db.Column(db.String(80), nullable=False, default="")
    email = db.Column(db.String(120), nullable=False, default="")
    tel = db.Column(db.String(40), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    invoices = db.relationship("Invoice", back_populates="client")

    @property
    def display_name(self) -> str:
        return f"{self.name} {self.honorific}".strip()


# ---------------------------------------------------------------------------
# 請求書
# ---------------------------------------------------------------------------
STATUS_DRAFT = "draft"
STATUS_ISSUED = "issued"
STATUS_PAID = "paid"
INVOICE_STATUSES = (STATUS_DRAFT, STATUS_ISSUED, STATUS_PAID)
STATUS_LABELS = {STATUS_DRAFT: "下書き", STATUS_ISSUED: "発行済", STATUS_PAID: "入金済"}

TAX_RATES = (10, 8, 0)


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    issue_date = db.Column(db.Date, nullable=False, default=date.today)
    due_date = db.Column(db.Date, nullable=True)
    subject = db.Column(db.String(200), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default=STATUS_DRAFT)
    paid_at = db.Column(db.Date, nullable=True)
    # 集計値（一覧表示用に保存しておく。明細保存時に再計算する）
    subtotal = db.Column(db.Integer, nullable=False, default=0)
    tax = db.Column(db.Integer, nullable=False, default=0)
    total = db.Column(db.Integer, nullable=False, default=0)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    client = db.relationship("Client", back_populates="invoices")
    created_by = db.relationship("User")
    items = db.relationship(
        "InvoiceItem",
        back_populates="invoice",
        order_by="InvoiceItem.position",
        cascade="all, delete-orphan",
    )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_locked(self) -> bool:
        """入金済の請求書は編集不可。"""
        return self.status == STATUS_PAID

    @property
    def is_overdue(self) -> bool:
        return self.status == STATUS_ISSUED and self.due_date is not None and self.due_date < date.today()


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    description = db.Column(db.String(200), nullable=False)
    quantity = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("1"))
    unit = db.Column(db.String(20), nullable=False, default="")
    unit_price = db.Column(db.Numeric(14, 2), nullable=False, default=Decimal("0"))
    tax_rate = db.Column(db.Integer, nullable=False, default=10)
    amount = db.Column(db.Integer, nullable=False, default=0)  # 税抜金額（円）

    invoice = db.relationship("Invoice", back_populates="items")
