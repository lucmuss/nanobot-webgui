"""Tests for nanobot_webgui.auth — AdminUser and AuthService."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot_webgui.auth import (
    AdminUser,
    AuthService,
    _hash_password,
    _verify_password,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_service(tmp_path: Path) -> AuthService:
    service = AuthService(
        db_path=tmp_path / "auth.db",
        secret_path=tmp_path / "session.secret",
    )
    service.init_db()
    return service


@pytest.fixture
def admin(auth_service: AuthService) -> AdminUser:
    return auth_service.create_admin("alice", "alice@example.com", "secret123")


# ---------------------------------------------------------------------------
# AdminUser properties
# ---------------------------------------------------------------------------


def test_admin_user_label_uses_display_name():
    user = AdminUser(id=1, username="alice", email="a@example.com", display_name="Alice Wonder")
    assert user.label == "Alice Wonder"


def test_admin_user_label_falls_back_to_username():
    user = AdminUser(id=1, username="alice", email="a@example.com", display_name="")
    assert user.label == "alice"


def test_admin_user_initials_two_words():
    user = AdminUser(id=1, username="alice", email="a@example.com", display_name="Alice Wonder")
    assert user.initials == "AW"


def test_admin_user_initials_single_word():
    user = AdminUser(id=1, username="alice", email="a@example.com", display_name="Alice")
    assert user.initials == "A"


def test_admin_user_initials_three_words_truncated():
    user = AdminUser(id=1, username="alice", email="a@example.com", display_name="Alice B Wonder")
    assert user.initials == "AB"


def test_admin_user_initials_empty_display_name_uses_username():
    user = AdminUser(id=1, username="alice", email="a@example.com", display_name="")
    assert user.initials == "A"


def test_admin_user_initials_fallback_nb():
    user = AdminUser(id=1, username="", email="a@example.com", display_name="")
    assert user.initials == "NB"


def test_admin_user_avatar_url_none_when_no_path():
    user = AdminUser(id=1, username="alice", email="a@example.com", display_name="Alice", avatar_path=None)
    assert user.avatar_url is None


def test_admin_user_avatar_url_prefixed():
    user = AdminUser(id=1, username="alice", email="a@example.com", display_name="Alice", avatar_path="avatars/alice.png")
    assert user.avatar_url == "/media/avatars/alice.png"


# ---------------------------------------------------------------------------
# AuthService.init_db
# ---------------------------------------------------------------------------


def test_init_db_creates_table(tmp_path: Path):
    service = AuthService(tmp_path / "auth.db", tmp_path / "secret")
    service.init_db()
    assert service.db_path.exists()


def test_init_db_is_idempotent(auth_service: AuthService):
    # calling init_db twice should not raise
    auth_service.init_db()


# ---------------------------------------------------------------------------
# AuthService.ensure_session_secret
# ---------------------------------------------------------------------------


def test_ensure_session_secret_creates_file(auth_service: AuthService):
    secret = auth_service.ensure_session_secret()
    assert auth_service.secret_path.exists()
    assert len(secret) > 20


def test_ensure_session_secret_returns_same_value(auth_service: AuthService):
    first = auth_service.ensure_session_secret()
    second = auth_service.ensure_session_secret()
    assert first == second


# ---------------------------------------------------------------------------
# AuthService.has_admin / create_admin
# ---------------------------------------------------------------------------


def test_has_admin_false_initially(auth_service: AuthService):
    assert auth_service.has_admin() is False


def test_has_admin_true_after_create(auth_service: AuthService, admin: AdminUser):
    assert auth_service.has_admin() is True


def test_create_admin_returns_user(auth_service: AuthService):
    user = auth_service.create_admin("bob", "bob@example.com", "pass456")
    assert user.username == "bob"
    assert user.email == "bob@example.com"
    assert user.id > 0


def test_create_admin_normalizes_email(auth_service: AuthService):
    user = auth_service.create_admin("bob", "BOB@EXAMPLE.COM", "pass456")
    assert user.email == "bob@example.com"


def test_create_admin_strips_username(auth_service: AuthService):
    user = auth_service.create_admin("  bob  ", "bob@example.com", "pass456")
    assert user.username == "bob"


def test_create_admin_rejects_second_admin(auth_service: AuthService, admin: AdminUser):
    with pytest.raises(ValueError, match="already exists"):
        auth_service.create_admin("bob", "bob@example.com", "pass456")


def test_create_admin_rejects_empty_username(auth_service: AuthService):
    with pytest.raises(ValueError, match="required"):
        auth_service.create_admin("", "a@example.com", "pass456")


def test_create_admin_rejects_empty_email(auth_service: AuthService):
    with pytest.raises(ValueError, match="required"):
        auth_service.create_admin("alice", "", "pass456")


def test_create_admin_rejects_empty_password(auth_service: AuthService):
    with pytest.raises(ValueError, match="required"):
        auth_service.create_admin("alice", "a@example.com", "")


# ---------------------------------------------------------------------------
# AuthService.authenticate
# ---------------------------------------------------------------------------


def test_authenticate_by_username(auth_service: AuthService, admin: AdminUser):
    user = auth_service.authenticate("alice", "secret123")
    assert user is not None
    assert user.username == "alice"


def test_authenticate_by_email(auth_service: AuthService, admin: AdminUser):
    user = auth_service.authenticate("alice@example.com", "secret123")
    assert user is not None
    assert user.email == "alice@example.com"


def test_authenticate_wrong_password(auth_service: AuthService, admin: AdminUser):
    assert auth_service.authenticate("alice", "wrongpass") is None


def test_authenticate_nonexistent_user(auth_service: AuthService):
    assert auth_service.authenticate("nobody", "pass") is None


def test_authenticate_empty_identifier(auth_service: AuthService):
    assert auth_service.authenticate("", "pass") is None


def test_authenticate_empty_password(auth_service: AuthService, admin: AdminUser):
    assert auth_service.authenticate("alice", "") is None


def test_authenticate_email_case_insensitive(auth_service: AuthService, admin: AdminUser):
    user = auth_service.authenticate("ALICE@EXAMPLE.COM", "secret123")
    assert user is not None


# ---------------------------------------------------------------------------
# AuthService.get_admin
# ---------------------------------------------------------------------------


def test_get_admin_valid_id(auth_service: AuthService, admin: AdminUser):
    user = auth_service.get_admin(admin.id)
    assert user is not None
    assert user.username == "alice"


def test_get_admin_none_id(auth_service: AuthService):
    assert auth_service.get_admin(None) is None


def test_get_admin_nonexistent_id(auth_service: AuthService):
    assert auth_service.get_admin(9999) is None


# ---------------------------------------------------------------------------
# AuthService.update_admin
# ---------------------------------------------------------------------------


def test_update_admin_changes_username(auth_service: AuthService, admin: AdminUser):
    updated = auth_service.update_admin(
        admin.id, username="alicia", email="alice@example.com", display_name="Alicia"
    )
    assert updated.username == "alicia"


def test_update_admin_changes_password(auth_service: AuthService, admin: AdminUser):
    auth_service.update_admin(
        admin.id, username="alice", email="alice@example.com", display_name="Alice", password="newpass"
    )
    assert auth_service.authenticate("alice", "newpass") is not None
    assert auth_service.authenticate("alice", "secret123") is None


def test_update_admin_empty_password_preserves_old(auth_service: AuthService, admin: AdminUser):
    auth_service.update_admin(
        admin.id, username="alice", email="alice@example.com", display_name="Alice", password=None
    )
    assert auth_service.authenticate("alice", "secret123") is not None


def test_update_admin_display_name_empty_defaults_to_username(auth_service: AuthService, admin: AdminUser):
    updated = auth_service.update_admin(
        admin.id, username="alice", email="alice@example.com", display_name=""
    )
    assert updated.display_name == "alice"


def test_update_admin_sets_avatar_path(auth_service: AuthService, admin: AdminUser):
    updated = auth_service.update_admin(
        admin.id, username="alice", email="alice@example.com", display_name="Alice", avatar_path="avatars/alice.png"
    )
    assert updated.avatar_path == "avatars/alice.png"


def test_update_admin_rejects_empty_username(auth_service: AuthService, admin: AdminUser):
    with pytest.raises(ValueError, match="required"):
        auth_service.update_admin(admin.id, username="", email="alice@example.com", display_name="Alice")


def test_update_admin_rejects_nonexistent_id(auth_service: AuthService, admin: AdminUser):
    with pytest.raises(ValueError, match="not found"):
        auth_service.update_admin(9999, username="ghost", email="ghost@example.com", display_name="Ghost")


# ---------------------------------------------------------------------------
# _hash_password / _verify_password
# ---------------------------------------------------------------------------


def test_hash_password_roundtrip():
    hashed = _hash_password("mypassword")
    assert _verify_password("mypassword", hashed) is True


def test_hash_password_wrong_password():
    hashed = _hash_password("mypassword")
    assert _verify_password("wrongpassword", hashed) is False


def test_hash_password_unique_salts():
    h1 = _hash_password("same")
    h2 = _hash_password("same")
    assert h1 != h2


def test_verify_password_corrupted_hash():
    assert _verify_password("pass", "not$a$valid$hash$at$all$extra") is False


def test_verify_password_malformed_string():
    assert _verify_password("pass", "invalid") is False


def test_hash_password_unicode():
    hashed = _hash_password("pässwörd")
    assert _verify_password("pässwörd", hashed) is True
    assert _verify_password("password", hashed) is False
