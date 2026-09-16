"""請求書アプリ。

- 管理者(admin) / 利用者(user) の2ロール
- 最初の管理者は初回セットアップ画面(/setup)でブートストラップ登録
- 利用者の自己登録は無し。管理者がユーザー管理画面から追加する
- 取引先・請求書の管理、印刷・PDF 出力（clients / invoices / company の各 Blueprint）
"""
import os
from datetime import date, datetime, timedelta, timezone

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import clients
import company
import invoices
from auth import admin_required, current_user, login_required
from billing import dashboard_stats
from models import (
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLE_USER,
    ROLES,
    STATUS_LABELS,
    CompanySetting,
    User,
    db,
    utcnow,
)

MIN_PASSWORD_LENGTH = 8
JST = timezone(timedelta(hours=9))


def _normalize_db_url(url: str) -> str:
    # SQLAlchemy は postgres:// を認識しないため postgresql:// に変換
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        app.config["SQLALCHEMY_DATABASE_URI"] = _normalize_db_url(database_url)
    else:
        # DATABASE_URL が無い場合はローカル SQLite にフォールバック
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    with app.app_context():
        db.create_all()
        _maybe_seed_admin()

    _register_filters(app)
    _register_routes(app)
    app.register_blueprint(clients.bp)
    app.register_blueprint(invoices.bp)
    app.register_blueprint(company.bp)
    return app


def _maybe_seed_admin() -> None:
    """環境変数で明示された場合のみ初期管理者を作成する。

    ADMIN_USERNAME と ADMIN_PASSWORD の両方が指定されているときだけ
    自動作成する（自動デプロイ向け）。指定が無ければ何もせず、
    初回セットアップ画面（/setup）で最初の管理者を登録する。
    """
    admin_username = os.environ.get("ADMIN_USERNAME")
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if not admin_username or not admin_password:
        return

    if User.query.filter_by(username=admin_username).first() is None:
        admin = User(username=admin_username, display_name="管理者", role=ROLE_ADMIN)
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()


def needs_setup() -> bool:
    """管理者が1人も存在しない（初回セットアップが必要な）状態か。"""
    return User.query.filter_by(role=ROLE_ADMIN).count() == 0


def _admin_count() -> int:
    """有効な管理者の人数。"""
    return User.query.filter_by(role=ROLE_ADMIN, is_active=True).count()


def _validate_password(password: str, confirm: str | None = None) -> str | None:
    """パスワードの検証。問題があればエラーメッセージを返す。"""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"パスワードは{MIN_PASSWORD_LENGTH}文字以上で設定してください。"
    if confirm is not None and password != confirm:
        return "パスワードが一致しません。"
    return None


def _safe_next(target: str | None) -> str | None:
    # オープンリダイレクト防止: 同一サイト内の相対パスのみ許可
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


# ---------------------------------------------------------------------------
# テンプレートフィルタ
# ---------------------------------------------------------------------------
def _register_filters(app: Flask) -> None:
    @app.template_filter("jst")
    def format_jst(value: datetime | None) -> str:
        """DB に保存した UTC 時刻を日本時間で表示する。"""
        if value is None:
            return "—"
        return value.replace(tzinfo=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M")

    @app.template_filter("yen")
    def format_yen(value) -> str:
        if value is None:
            return "—"
        return f"¥{int(value):,}"

    @app.template_filter("num")
    def format_num(value) -> str:
        """数量・単価。整数なら小数点なし、小数なら末尾の 0 を落とす。"""
        if value is None:
            return ""
        f = float(value)
        if f == int(f):
            return f"{int(f):,}"
        return f"{f:,.2f}".rstrip("0").rstrip(".")

    @app.template_filter("plain")
    def format_plain(value) -> str:
        """input 用の生の数値文字列。"""
        if value is None:
            return ""
        f = float(value)
        return str(int(f)) if f == int(f) else f"{f:.2f}".rstrip("0").rstrip(".")

    @app.template_filter("ymd")
    def format_ymd(value: date | None) -> str:
        return value.strftime("%Y-%m-%d") if value else ""

    @app.template_filter("jdate")
    def format_jdate(value: date | None) -> str:
        return value.strftime("%Y年%m月%d日") if value else "—"


# ---------------------------------------------------------------------------
# ルーティング（認証・ユーザー管理・ダッシュボード）
# ---------------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    @app.context_processor
    def inject_globals():
        return {
            "current_user": current_user(),
            "role_labels": ROLE_LABELS,
            "status_labels": STATUS_LABELS,
        }

    @app.before_request
    def enforce_setup():
        """管理者が存在しない間は、セットアップ画面以外を全て /setup へ誘導する。"""
        if request.endpoint in ("setup", "static"):
            return None
        if needs_setup():
            return redirect(url_for("setup"))
        return None

    @app.route("/")
    def index():
        if current_user() is not None:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    # --- 初回セットアップ（ブートストラップ） -----------------------------
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """管理者が1人もいないときだけ最初の管理者を登録できる。"""
        if not needs_setup():
            flash("初期セットアップは既に完了しています。", "error")
            return redirect(url_for("login"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            display_name = (request.form.get("display_name") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""

            error = None
            if not username or not password:
                error = "ログインIDとパスワードを入力してください。"
            else:
                error = _validate_password(password, confirm)
            if error is None and User.query.filter_by(username=username).first() is not None:
                error = "そのログインIDは既に使われています。"

            if error:
                flash(error, "error")
            else:
                admin = User(username=username, display_name=display_name, role=ROLE_ADMIN)
                admin.set_password(password)
                admin.last_login_at = utcnow()
                db.session.add(admin)
                db.session.commit()
                # そのままログインさせて自社情報の登録へ
                session.clear()
                session["user_id"] = admin.id
                flash("最初の管理者を登録しました。続けて請求書に載せる自社情報を登録してください。", "success")
                return redirect(url_for("company.edit"))

        return render_template("setup.html")

    # --- ログイン / ログアウト ------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user() is not None:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            user = User.query.filter_by(username=username).first()
            if user is not None and user.check_password(password):
                if not user.is_active:
                    flash("このアカウントは無効化されています。管理者にお問い合わせください。", "error")
                    return render_template("login.html")
                session.clear()
                session["user_id"] = user.id
                user.last_login_at = utcnow()
                db.session.commit()
                flash(f"ようこそ、{user.name} さん。", "success")
                return redirect(_safe_next(request.args.get("next")) or url_for("dashboard"))

            flash("ログインIDまたはパスワードが正しくありません。", "error")

        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        flash("ログアウトしました。", "success")
        return redirect(url_for("login"))

    # --- ログイン後（共通） ---------------------------------------------
    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template(
            "dashboard.html",
            user=current_user(),
            stats=dashboard_stats(),
            company=CompanySetting.get(),
        )

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        """自分の表示名・パスワードを変更する（本人確認に現在のパスワードが必須）。"""
        user = current_user()

        if request.method == "POST":
            current_password = request.form.get("current_password") or ""
            display_name = (request.form.get("display_name") or "").strip()
            new_password = request.form.get("new_password") or ""
            confirm = request.form.get("confirm") or ""

            error = None
            if not user.check_password(current_password):
                error = "現在のパスワードが正しくありません。"
            elif new_password:
                error = _validate_password(new_password, confirm)

            if error:
                flash(error, "error")
            else:
                user.display_name = display_name
                if new_password:
                    user.set_password(new_password)
                db.session.commit()
                flash("アカウント設定を更新しました。", "success")
                return redirect(url_for("settings"))

        return render_template("settings.html", user=user)

    # --- 管理者専用: ユーザー管理 ---------------------------------------
    def _get_user_or_404(user_id: int) -> User:
        user = db.session.get(User, user_id)
        if user is None:
            abort(404)
        return user

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        users = User.query.order_by(User.created_at.asc(), User.id.asc()).all()
        return render_template("admin_users.html", users=users, roles=ROLES)

    @app.route("/admin/users/create", methods=["POST"])
    @admin_required
    def admin_create_user():
        username = (request.form.get("username") or "").strip()
        display_name = (request.form.get("display_name") or "").strip()
        password = request.form.get("password") or ""
        role = request.form.get("role") or ROLE_USER
        if role not in ROLES:
            role = ROLE_USER

        error = None
        if not username or not password:
            error = "ログインIDとパスワードを入力してください。"
        else:
            error = _validate_password(password)
        if error is None and User.query.filter_by(username=username).first() is not None:
            error = "そのログインIDは既に使われています。"

        if error:
            flash(error, "error")
        else:
            user = User(username=username, display_name=display_name, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"{ROLE_LABELS[role]}「{user.name}」を追加しました。", "success")

        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/role", methods=["POST"])
    @admin_required
    def admin_update_role(user_id):
        user = _get_user_or_404(user_id)
        new_role = request.form.get("role")
        if new_role not in ROLES:
            flash("無効なロールです。", "error")
            return redirect(url_for("admin_users"))

        # 最後の有効な管理者を降格させないよう保護
        if user.is_admin and new_role != ROLE_ADMIN and user.is_active and _admin_count() <= 1:
            flash("最後の管理者の権限は変更できません。", "error")
            return redirect(url_for("admin_users"))

        user.role = new_role
        db.session.commit()
        flash(f"「{user.name}」のロールを{ROLE_LABELS[new_role]}に変更しました。", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/password", methods=["POST"])
    @admin_required
    def admin_reset_password(user_id):
        """管理者が利用者のパスワードを再設定する。"""
        user = _get_user_or_404(user_id)
        new_password = request.form.get("password") or ""
        error = _validate_password(new_password)
        if error:
            flash(error, "error")
        else:
            user.set_password(new_password)
            db.session.commit()
            flash(f"「{user.name}」のパスワードを再設定しました。", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
    @admin_required
    def admin_toggle_active(user_id):
        """アカウントの有効/無効を切り替える（削除せずにログインを止める）。"""
        user = _get_user_or_404(user_id)
        if user.id == current_user().id:
            flash("自分自身は無効化できません。", "error")
            return redirect(url_for("admin_users"))
        if user.is_admin and user.is_active and _admin_count() <= 1:
            flash("最後の管理者は無効化できません。", "error")
            return redirect(url_for("admin_users"))

        user.is_active = not user.is_active
        db.session.commit()
        state = "有効" if user.is_active else "無効"
        flash(f"「{user.name}」を{state}にしました。", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_user(user_id):
        user = _get_user_or_404(user_id)
        if user.id == current_user().id:
            flash("自分自身は削除できません。", "error")
            return redirect(url_for("admin_users"))
        if user.is_admin and user.is_active and _admin_count() <= 1:
            flash("最後の管理者は削除できません。", "error")
            return redirect(url_for("admin_users"))

        db.session.delete(user)
        db.session.commit()
        flash(f"「{user.name}」を削除しました。", "success")
        return redirect(url_for("admin_users"))


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
