"""取引先管理。"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from auth import login_required
from models import HONORIFICS, Client, Invoice, db

bp = Blueprint("clients", __name__, url_prefix="/clients")


def _form_to_client(client: Client) -> str | None:
    """フォーム値を client に反映する。問題があればエラーメッセージを返す。"""
    name = (request.form.get("name") or "").strip()
    if not name:
        return "取引先名を入力してください。"
    honorific = request.form.get("honorific") or "御中"
    if honorific not in HONORIFICS:
        honorific = "御中"
    client.name = name
    client.honorific = honorific
    client.postal_code = (request.form.get("postal_code") or "").strip()
    client.address = (request.form.get("address") or "").strip()
    client.contact_name = (request.form.get("contact_name") or "").strip()
    client.email = (request.form.get("email") or "").strip()
    client.tel = (request.form.get("tel") or "").strip()
    client.notes = (request.form.get("notes") or "").strip()
    return None


@bp.route("")
@login_required
def index():
    q = (request.args.get("q") or "").strip()
    query = db.select(Client).order_by(Client.name.asc())
    if q:
        query = query.where(Client.name.contains(q) | Client.contact_name.contains(q))
    clients = db.session.scalars(query).all()
    return render_template("clients/index.html", clients=clients, q=q)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    client = Client()
    if request.method == "POST":
        error = _form_to_client(client)
        if error:
            flash(error, "error")
        else:
            db.session.add(client)
            db.session.commit()
            flash(f"取引先「{client.name}」を登録しました。", "success")
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect(url_for("clients.index"))
    return render_template("clients/form.html", client=client, honorifics=HONORIFICS, is_new=True)


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def edit(client_id):
    client = db.session.get(Client, client_id) or abort(404)
    if request.method == "POST":
        error = _form_to_client(client)
        if error:
            flash(error, "error")
        else:
            db.session.commit()
            flash(f"取引先「{client.name}」を更新しました。", "success")
            return redirect(url_for("clients.index"))
    invoices = db.session.scalars(
        db.select(Invoice).where(Invoice.client_id == client.id).order_by(Invoice.issue_date.desc(), Invoice.id.desc())
    ).all()
    return render_template(
        "clients/form.html", client=client, honorifics=HONORIFICS, is_new=False, invoices=invoices
    )


@bp.route("/<int:client_id>/delete", methods=["POST"])
@login_required
def delete(client_id):
    client = db.session.get(Client, client_id) or abort(404)
    if client.invoices:
        flash("この取引先には請求書があるため削除できません。", "error")
        return redirect(url_for("clients.edit", client_id=client.id))
    db.session.delete(client)
    db.session.commit()
    flash(f"取引先「{client.name}」を削除しました。", "success")
    return redirect(url_for("clients.index"))
