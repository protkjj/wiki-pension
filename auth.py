"""인증·사용자 관리 (플랫폼 MVP).

비밀번호는 pbkdf2_hmac(sha256)로 해시하여 저장(평문 저장 금지).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Optional, Union

from .db import get_conn

_ITERATIONS = 200_000


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """(hash_hex, salt_hex) 반환."""
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS)
    return h.hex(), salt


def verify_password(password: str, pw_hash: str, salt: str) -> bool:
    calc, _ = hash_password(password, salt)
    return hmac.compare_digest(calc, pw_hash)


def create_company(db_path: Union[str, Path], name: str, now: str) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO companies(name, created) VALUES(?, ?)", (name, now)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def create_user(
    db_path: Union[str, Path],
    username: str,
    password: str,
    role: str,
    now: str,
    company_id: Optional[int] = None,
    display_name: str = "",
) -> int:
    pw_hash, salt = hash_password(password)
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO users(username, pw_hash, pw_salt, role, company_id, display_name, created)"
            " VALUES(?,?,?,?,?,?,?)",
            (username, pw_hash, salt, role, company_id, display_name or username, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def authenticate(db_path: Union[str, Path], username: str, password: str) -> Optional[dict]:
    """성공 시 사용자 dict(회사명 포함) 반환, 실패 시 None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT u.*, c.name AS company_name FROM users u "
            "LEFT JOIN companies c ON u.company_id = c.id WHERE u.username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if not verify_password(password, row["pw_hash"], row["pw_salt"]):
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "company_id": row["company_id"],
        "company_name": row["company_name"],
        "display_name": row["display_name"],
    }


def _row_to_account(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "company_id": row["company_id"],
        "company_name": row["company_name"],
        "display_name": row["display_name"],
    }


def list_users(db_path: Union[str, Path]) -> list[dict]:
    """로그인 선택용 사용자 목록(회사명 포함). role 순서로 정렬."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT u.*, c.name AS company_name FROM users u "
            "LEFT JOIN companies c ON u.company_id = c.id "
            "ORDER BY CASE u.role WHEN 'admin' THEN 0 WHEN 'actuary' THEN 1 ELSE 2 END, u.username"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_account(r) for r in rows]


def login_username_only(db_path: Union[str, Path], username: str) -> Optional[dict]:
    """비밀번호 검증 없이 아이디만으로 로그인(프로토타입용). 성공 시 사용자 dict."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT u.*, c.name AS company_name FROM users u "
            "LEFT JOIN companies c ON u.company_id = c.id WHERE u.username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_account(row) if row else None


def user_exists(db_path: Union[str, Path], username: str) -> bool:
    conn = get_conn(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone() is not None
    finally:
        conn.close()
