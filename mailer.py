"""보고서 이메일 전송 유틸 (플랫폼 MVP).

두 가지 경로를 지원한다.
  1) build_eml(): 표준 .eml 파일(첨부 포함)을 만들어 반환 → 사용자가 내려받아
     아웃룩 등 메일 클라이언트에서 열고 보내면 된다. (SMTP 설정 불필요)
  2) send_smtp(): 환경변수로 SMTP가 설정된 경우 서버를 통해 즉시 전송.

자격증명·서버는 코드에 하드코딩하지 않고 환경변수로만 받는다.
  SMTP_HOST, SMTP_PORT(기본 587), SMTP_USER, SMTP_PASSWORD,
  SMTP_FROM(기본 SMTP_USER), SMTP_STARTTLS(기본 true)
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import List, Optional, Tuple

# 첨부 확장자 → MIME (maintype, subtype)
_MIME = {
    ".xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".pptx": ("application", "vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".pdf": ("application", "pdf"),
    ".md": ("text", "markdown"),
}


def smtp_config_from_env() -> Optional[dict]:
    """환경변수에서 SMTP 설정을 읽는다. 필수값이 없으면 None."""
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASSWORD")
    if not (host and user and pw):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user,
        "password": pw,
        "from_addr": os.environ.get("SMTP_FROM", user),
        "starttls": os.environ.get("SMTP_STARTTLS", "true").lower() != "false",
    }


def _build_message(
    from_addr: str,
    to_addrs: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[Path]] = None,
    from_name: str = "",
    cc_addrs: Optional[List[str]] = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachments or []:
        path = Path(path)
        if not path.exists():
            continue
        maintype, subtype = _MIME.get(path.suffix.lower(), ("application", "octet-stream"))
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                           filename=path.name)
    return msg


def build_eml(
    from_addr: str,
    to_addrs: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[Path]] = None,
    from_name: str = "",
    cc_addrs: Optional[List[str]] = None,
) -> bytes:
    """.eml 바이트를 만든다(내려받아 메일 클라이언트에서 열기용)."""
    msg = _build_message(from_addr, to_addrs, subject, body, attachments, from_name, cc_addrs)
    return msg.as_bytes()


def send_smtp(
    config: dict,
    to_addrs: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[Path]] = None,
    from_name: str = "",
    cc_addrs: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """SMTP로 즉시 전송. (성공여부, 메시지) 반환."""
    msg = _build_message(config["from_addr"], to_addrs, subject, body,
                         attachments, from_name, cc_addrs)
    recipients = list(to_addrs) + list(cc_addrs or [])
    try:
        with smtplib.SMTP(config["host"], config["port"], timeout=30) as srv:
            if config.get("starttls", True):
                srv.starttls()
            srv.login(config["user"], config["password"])
            srv.send_message(msg, to_addrs=recipients)
        return True, f"{', '.join(to_addrs)} 로 전송했습니다."
    except Exception as e:  # noqa: BLE001
        return False, f"전송 실패: {e}"


def valid_email(addr: str) -> bool:
    addr = (addr or "").strip()
    return "@" in addr and "." in addr.split("@")[-1] and " " not in addr
