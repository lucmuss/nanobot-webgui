"""SQLite-backed bootstrap admin authentication for the nanobot GUI."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import IntegrityError


PBKDF2_ITERATIONS = 120_000


@dataclass(slots=True)
class AdminUser:
    """Authenticated admin account."""

    id: int
    username: str
    email: str
    display_name: str
    avatar_path: str | None = None

    @property
    def label(self) -> str:
        """Return the human-friendly label used in the GUI."""
        return self.display_name or self.username

    @property
    def initials(self) -> str:
        """Return initials for the avatar fallback."""
        source = self.label.strip() or self.username.strip()
        parts = [part[0].upper() for part in source.split() if part]
        if not parts:
            return "NB"
        return "".join(parts[:2])

    @property
    def avatar_url(self) -> str | None:
        """Return the public avatar URL when an avatar is configured."""
        if not self.avatar_path:
            return None
        return f"/media/{self.avatar_path}"


class AuthService:
    """Store and validate the single bootstrap admin for the GUI."""

    def __init__(self, db_path: Path, secret_path: Path) -> None:
        self.db_path = db_path
        self.secret_path = secret_path

    def init_db(self) -> None:
        """Create the authentication store if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    avatar_path TEXT,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(admin_users)").fetchall()
            }
            if "display_name" not in columns:
                conn.execute(
                    "ALTER TABLE admin_users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
                )
            if "avatar_path" not in columns:
                conn.execute("ALTER TABLE admin_users ADD COLUMN avatar_path TEXT")
            conn.commit()

    def ensure_session_secret(self) -> str:
        """Return a stable session secret for Starlette's session middleware."""
        self.secret_path.parent.mkdir(parents=True, exist_ok=True)
        if self.secret_path.exists():
            return self.secret_path.read_text(encoding="utf-8").strip()

        secret = secrets.token_urlsafe(48)
        self.secret_path.write_text(secret, encoding="utf-8")
        return secret

    def has_admin(self) -> bool:
        """Return True once the first admin account exists."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone()
        return row is not None

    def create_admin(self, username: str, email: str, password: str) -> AdminUser:
        """Create the first admin account and reject any additional bootstrap attempt."""
        if self.has_admin():
            raise ValueError("An admin account already exists.")

        normalized_username = username.strip()
        normalized_email = email.strip().lower()
        if not normalized_username or not normalized_email or not password:
            raise ValueError("Username, email, and password are required.")

        password_hash = _hash_password(password)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO admin_users (username, email, display_name, password_hash)
                VALUES (?, ?, ?, ?)
                """,
                (normalized_username, normalized_email, normalized_username, password_hash),
            )
            conn.commit()
            admin_id = int(cursor.lastrowid)

        return AdminUser(
            id=admin_id,
            username=normalized_username,
            email=normalized_email,
            display_name=normalized_username,
        )

    def authenticate(self, identifier: str, password: str) -> AdminUser | None:
        """Authenticate by username or email."""
        if not identifier or not password:
            return None

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, username, email, display_name, avatar_path, password_hash
                FROM admin_users
                WHERE username = ? OR email = ?
                LIMIT 1
                """,
                (identifier.strip(), identifier.strip().lower()),
            ).fetchone()

        if row is None or not _verify_password(password, row["password_hash"]):
            return None

        return AdminUser(
            id=int(row["id"]),
            username=row["username"],
            email=row["email"],
            display_name=row["display_name"] or row["username"],
            avatar_path=row["avatar_path"],
        )

    def get_admin(self, admin_id: int | None) -> AdminUser | None:
        """Return the current admin user from its session identifier."""
        if admin_id is None:
            return None

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, username, email, display_name, avatar_path
                FROM admin_users
                WHERE id = ?
                LIMIT 1
                """,
                (admin_id,),
            ).fetchone()

        if row is None:
            return None

        return AdminUser(
            id=int(row["id"]),
            username=row["username"],
            email=row["email"],
            display_name=row["display_name"] or row["username"],
            avatar_path=row["avatar_path"],
        )

    def update_admin(
        self,
        admin_id: int,
        *,
        username: str,
        email: str,
        display_name: str,
        password: str | None = None,
        avatar_path: str | None = None,
    ) -> AdminUser:
        """Update the admin profile, optional password, and avatar path."""
        normalized_username = username.strip()
        normalized_email = email.strip().lower()
        normalized_display_name = display_name.strip() or normalized_username

        if not normalized_username or not normalized_email:
            raise ValueError("Username and email are required.")

        fields = [
            ("username", normalized_username),
            ("email", normalized_email),
            ("display_name", normalized_display_name),
        ]
        if password:
            fields.append(("password_hash", _hash_password(password)))
        if avatar_path is not None:
            fields.append(("avatar_path", avatar_path))

        set_clause = ", ".join(f"{column} = ?" for column, _ in fields)
        values = [value for _, value in fields]
        values.append(admin_id)

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    f"UPDATE admin_users SET {set_clause} WHERE id = ?",
                    values,
                )
                conn.commit()
        except IntegrityError as exc:
            message = "That username or email is already in use."
            raise ValueError(message) from exc

        if cursor.rowcount == 0:
            raise ValueError("Admin account not found.")

        updated = self.get_admin(admin_id)
        if updated is None:
            raise ValueError("Admin account not found.")
        return updated


def _hash_password(password: str) -> str:
    """Hash a password with PBKDF2 for local GUI login."""
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    """Validate a password against the stored PBKDF2 string."""
    try:
        _, iterations, salt_hex, hash_hex = encoded.split("$", 3)
    except ValueError:
        return False

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        int(iterations),
    )
    return hmac.compare_digest(derived.hex(), hash_hex)
