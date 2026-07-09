"""데모용 초기 데이터 시드 (플랫폼 MVP).

최초 실행 시 DB가 비어 있으면 관리자·계리인·예시 기업/담당자 계정을 만든다.
⚠️ 데모 비밀번호이므로 실제 운영 전 반드시 교체·삭제할 것.
"""

from __future__ import annotations

from .auth import create_company, create_user, user_exists
from .db import get_conn, init_db

# (username, password, role, company, display)
DEMO_USERS = [
    ("admin", "admin123", "admin", None, "회사 관리자"),
    ("actuary", "act123", "actuary", None, "담당 계리인"),
    ("clientA", "ca123", "client", "가나전자", "가나전자 인사담당"),
    ("clientB", "cb123", "client", "다라물산", "다라물산 인사담당"),
]


def seed_if_empty(db_path, now: str) -> bool:
    """DB가 비어 있으면 데모 데이터를 생성한다. 생성했으면 True."""
    init_db(db_path)
    conn = get_conn(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    finally:
        conn.close()
    if n > 0:
        return False

    company_ids: dict = {}
    for _, _, _, company, _ in DEMO_USERS:
        if company and company not in company_ids:
            company_ids[company] = create_company(db_path, company, now)

    for username, pw, role, company, display in DEMO_USERS:
        if not user_exists(db_path, username):
            create_user(db_path, username, pw, role, now,
                        company_id=company_ids.get(company), display_name=display)
    return True
