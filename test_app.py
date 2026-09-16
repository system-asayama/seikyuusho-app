"""ログインシステムのテスト。

実行: pytest
"""
import pytest

from app import create_app
from models import ROLE_ADMIN, ROLE_USER, User, db

ADMIN_PW = "admin-pass-123"
USER_PW = "user-pass-123"


@pytest.fixture()
def app():
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SECRET_KEY": "test",
        }
    )
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def _setup_admin(client, username="admin", password=ADMIN_PW):
    return client.post(
        "/setup",
        data={"username": username, "display_name": "管理者", "password": password, "confirm": password},
        follow_redirects=False,
    )


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def _logout(client):
    return client.post("/logout", follow_redirects=False)


def _create_user(client, username="taro", password=USER_PW, role=ROLE_USER, display_name=""):
    return client.post(
        "/admin/users/create",
        data={"username": username, "display_name": display_name, "password": password, "role": role},
        follow_redirects=False,
    )


def _user_id(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).first().id


# ---------------------------------------------------------------------------
# ブートストラップ
# ---------------------------------------------------------------------------
def test_everything_redirects_to_setup_when_no_admin(client):
    for path in ("/", "/login", "/dashboard", "/admin/users"):
        res = client.get(path)
        assert res.status_code == 302, path
        assert res.headers["Location"].endswith("/setup"), path


def test_setup_page_renders(client):
    res = client.get("/setup")
    assert res.status_code == 200
    assert "初期セットアップ" in res.get_data(as_text=True)


def test_setup_creates_admin_and_logs_in(app, client):
    res = _setup_admin(client)
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/admin/users")

    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        assert admin is not None
        assert admin.role == ROLE_ADMIN
        assert admin.check_password(ADMIN_PW)
        assert admin.password_hash != ADMIN_PW

    # そのままログイン済みでユーザー管理へ入れる
    res = client.get("/admin/users")
    assert res.status_code == 200


def test_setup_disabled_after_first_admin(client):
    _setup_admin(client)
    _logout(client)
    res = client.get("/setup")
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/login")

    # POST しても2人目の管理者は作れない
    res = _setup_admin(client, username="admin2")
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/login")


def test_setup_validates_password(app, client):
    res = client.post(
        "/setup", data={"username": "admin", "password": "short", "confirm": "short"}, follow_redirects=True
    )
    assert "8文字以上" in res.get_data(as_text=True)
    res = client.post(
        "/setup",
        data={"username": "admin", "password": ADMIN_PW, "confirm": "different-123"},
        follow_redirects=True,
    )
    assert "一致しません" in res.get_data(as_text=True)
    with app.app_context():
        assert User.query.count() == 0


# ---------------------------------------------------------------------------
# ログイン / ログアウト / アクセス制御
# ---------------------------------------------------------------------------
def test_login_wrong_password(client):
    _setup_admin(client)
    _logout(client)
    res = _login(client, "admin", "wrong-password")
    assert res.status_code == 200
    assert "正しくありません" in res.get_data(as_text=True)
    assert client.get("/dashboard").status_code == 302


def test_admin_login_redirects_to_user_management(client):
    _setup_admin(client)
    _logout(client)
    res = _login(client, "admin", ADMIN_PW)
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/admin/users")


def test_user_login_redirects_to_dashboard_and_cannot_access_admin(client):
    _setup_admin(client)
    _create_user(client)
    _logout(client)

    res = _login(client, "taro", USER_PW)
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/dashboard")

    assert client.get("/dashboard").status_code == 200
    res = client.get("/admin/users")
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/dashboard")

    # 利用者は他人を追加できない
    res = _create_user(client, username="hanako")
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/dashboard")


def test_no_self_registration_route(client):
    _setup_admin(client)
    assert client.get("/register").status_code == 404
    assert client.post("/register", data={"username": "x", "password": "y"}).status_code in (404, 405)


def test_logout_requires_post_and_clears_session(client):
    _setup_admin(client)
    assert client.get("/logout").status_code == 405
    res = _logout(client)
    assert res.status_code == 302
    assert client.get("/admin/users").status_code == 302


def test_login_next_param_is_safe(client):
    _setup_admin(client)
    _logout(client)
    res = client.post("/login?next=//evil.example", data={"username": "admin", "password": ADMIN_PW})
    assert res.headers["Location"].endswith("/admin/users")
    _logout(client)
    res = client.post("/login?next=/settings", data={"username": "admin", "password": ADMIN_PW})
    assert res.headers["Location"].endswith("/settings")


# ---------------------------------------------------------------------------
# 管理者によるユーザー管理
# ---------------------------------------------------------------------------
def test_admin_creates_user(app, client):
    _setup_admin(client)
    res = _create_user(client, display_name="山田 太郎")
    assert res.status_code == 302
    with app.app_context():
        user = User.query.filter_by(username="taro").first()
        assert user.role == ROLE_USER
        assert user.display_name == "山田 太郎"
        assert user.is_active


def test_admin_create_rejects_duplicate_and_short_password(app, client):
    _setup_admin(client)
    _create_user(client)
    res = _create_user(client)
    assert res.status_code == 302
    res = client.get("/admin/users")
    assert "既に使われています" in res.get_data(as_text=True)

    _create_user(client, username="jiro", password="short")
    res = client.get("/admin/users")
    assert "8文字以上" in res.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(username="jiro").first() is None


def test_admin_resets_user_password(app, client):
    _setup_admin(client)
    _create_user(client)
    uid = _user_id(app, "taro")
    client.post(f"/admin/users/{uid}/password", data={"password": "new-pass-456"})
    _logout(client)
    assert _login(client, "taro", USER_PW).status_code == 200  # 旧パスワードは無効
    assert _login(client, "taro", "new-pass-456").status_code == 302


def test_admin_toggles_user_active(app, client):
    _setup_admin(client)
    _create_user(client)
    uid = _user_id(app, "taro")
    client.post(f"/admin/users/{uid}/toggle")
    with app.app_context():
        assert db.session.get(User, uid).is_active is False

    _logout(client)
    res = _login(client, "taro", USER_PW)
    assert res.status_code == 200
    assert "無効化されています" in res.get_data(as_text=True)

    _login(client, "admin", ADMIN_PW)
    client.post(f"/admin/users/{uid}/toggle")
    with app.app_context():
        assert db.session.get(User, uid).is_active is True


def test_deactivated_user_session_is_invalidated(app, client):
    """ログイン中の利用者が無効化されたら、次のリクエストで弾かれる。"""
    _setup_admin(client)
    _create_user(client)
    uid = _user_id(app, "taro")
    _logout(client)
    _login(client, "taro", USER_PW)
    assert client.get("/dashboard").status_code == 200

    admin_client = app.test_client()
    _login(admin_client, "admin", ADMIN_PW)
    admin_client.post(f"/admin/users/{uid}/toggle")

    assert client.get("/dashboard").status_code == 302


def test_admin_changes_role_and_deletes_user(app, client):
    _setup_admin(client)
    _create_user(client)
    uid = _user_id(app, "taro")

    client.post(f"/admin/users/{uid}/role", data={"role": ROLE_ADMIN})
    with app.app_context():
        assert db.session.get(User, uid).role == ROLE_ADMIN

    client.post(f"/admin/users/{uid}/role", data={"role": "superuser"})
    with app.app_context():
        assert db.session.get(User, uid).role == ROLE_ADMIN  # 無効なロールは無視

    client.post(f"/admin/users/{uid}/delete")
    with app.app_context():
        assert db.session.get(User, uid) is None


def test_last_admin_is_protected(app, client):
    _setup_admin(client)
    admin_id = _user_id(app, "admin")

    # 降格できない
    client.post(f"/admin/users/{admin_id}/role", data={"role": ROLE_USER})
    with app.app_context():
        assert db.session.get(User, admin_id).role == ROLE_ADMIN

    # 自分自身は無効化・削除できない
    client.post(f"/admin/users/{admin_id}/toggle")
    client.post(f"/admin/users/{admin_id}/delete")
    with app.app_context():
        admin = db.session.get(User, admin_id)
        assert admin is not None and admin.is_active

    # 2人目の管理者からも、最後の1人になる操作はできない
    _create_user(client, username="admin2", password=ADMIN_PW, role=ROLE_ADMIN)
    admin2_id = _user_id(app, "admin2")
    client.post(f"/admin/users/{admin2_id}/toggle")  # admin2 を無効化 → 有効な管理者は1人
    _logout(client)
    _login(client, "admin", ADMIN_PW)
    client.post(f"/admin/users/{admin_id}/role", data={"role": ROLE_USER})
    with app.app_context():
        assert db.session.get(User, admin_id).role == ROLE_ADMIN


def test_admin_can_delete_other_admin_when_two_active(app, client):
    _setup_admin(client)
    _create_user(client, username="admin2", password=ADMIN_PW, role=ROLE_ADMIN)
    admin2_id = _user_id(app, "admin2")
    client.post(f"/admin/users/{admin2_id}/delete")
    with app.app_context():
        assert db.session.get(User, admin2_id) is None


def test_unknown_user_id_returns_404(client):
    _setup_admin(client)
    assert client.post("/admin/users/9999/delete").status_code == 404


# ---------------------------------------------------------------------------
# 自分のアカウント設定
# ---------------------------------------------------------------------------
def test_settings_requires_current_password(app, client):
    _setup_admin(client)
    _create_user(client)
    _logout(client)
    _login(client, "taro", USER_PW)

    res = client.post(
        "/settings",
        data={"current_password": "wrong", "display_name": "太郎", "new_password": "", "confirm": ""},
        follow_redirects=True,
    )
    assert "現在のパスワードが正しくありません" in res.get_data(as_text=True)

    res = client.post(
        "/settings",
        data={
            "current_password": USER_PW,
            "display_name": "太郎",
            "new_password": "changed-pass-789",
            "confirm": "changed-pass-789",
        },
        follow_redirects=True,
    )
    assert "更新しました" in res.get_data(as_text=True)
    with app.app_context():
        user = User.query.filter_by(username="taro").first()
        assert user.display_name == "太郎"
        assert user.check_password("changed-pass-789")


def test_pages_render_for_admin(client):
    _setup_admin(client)
    _create_user(client, display_name="山田 太郎")
    for path in ("/dashboard", "/settings", "/admin/users"):
        res = client.get(path)
        assert res.status_code == 200, path
    html = client.get("/admin/users").get_data(as_text=True)
    assert "山田 太郎" in html
    assert "利用者" in html and "管理者" in html
