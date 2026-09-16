"""自社情報（請求書の発行者情報）。管理者のみ編集できる。"""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from auth import admin_required
from models import TAX_ROUNDING_LABELS, TAX_ROUNDINGS, CompanySetting, db

bp = Blueprint("company", __name__, url_prefix="/company")


@bp.route("", methods=["GET", "POST"])
@admin_required
def edit():
    company = CompanySetting.get()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        prefix = (request.form.get("invoice_prefix") or "").strip()
        rounding = request.form.get("tax_rounding") or "floor"
        due_days_raw = (request.form.get("default_due_days") or "30").strip()

        error = None
        if not name:
            error = "自社名を入力してください。"
        elif rounding not in TAX_ROUNDINGS:
            error = "端数処理の指定が不正です。"
        elif not due_days_raw.isdigit() or int(due_days_raw) > 365:
            error = "支払期限までの日数は 0〜365 の整数で入力してください。"

        if error:
            flash(error, "error")
        else:
            company.name = name
            company.postal_code = (request.form.get("postal_code") or "").strip()
            company.address = (request.form.get("address") or "").strip()
            company.tel = (request.form.get("tel") or "").strip()
            company.email = (request.form.get("email") or "").strip()
            company.representative = (request.form.get("representative") or "").strip()
            company.registration_number = (request.form.get("registration_number") or "").strip().upper()
            company.bank_info = (request.form.get("bank_info") or "").strip()
            company.invoice_prefix = prefix
            company.tax_rounding = rounding
            company.default_due_days = int(due_days_raw)
            company.default_notes = (request.form.get("default_notes") or "").strip()
            db.session.commit()
            flash("自社情報を保存しました。", "success")
            return redirect(url_for("company.edit"))

    return render_template("company/edit.html", company=company, rounding_labels=TAX_ROUNDING_LABELS)
