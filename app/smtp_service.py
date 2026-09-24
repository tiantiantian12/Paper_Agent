"""QQ 邮箱验证码发送（SMTP over SSL）。"""

from __future__ import annotations

import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from app.config import MAIL_SUBJECT, SMTP_CONFIG, SMTP_TIMEOUT


class MailError(RuntimeError):
    """发信失败。"""


def _build_message(to: str, code: str, purpose: str, nickname: str = "") -> MIMEText:
    if purpose == "reset":
        title = "重置登录密码"
        action = "你正在重置 Paper Agent 的登录密码"
    else:
        title = "注册账号"
        action = "你正在注册 Paper Agent 账号"

    hello = f"{nickname}，你好！" if nickname else "你好！"
    body = (
        f"{hello}\n\n"
        f"{action}，本次验证码为：\n\n"
        f"        {code}\n\n"
        f"验证码 {int(_ttl_minutes())} 分钟内有效，请勿转发给他人。\n"
        f"如果不是你本人操作，请忽略本邮件。\n\n"
        f"—— {MAIL_SUBJECT.split(' 注册验证码')[0]}"
    )
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = Header(f"【Paper Agent】{title}验证码：{code}", "utf-8")
    message["From"] = formataddr(("Paper Agent", str(SMTP_CONFIG["sender"])))
    message["To"] = to
    return message


def _ttl_minutes() -> float:
    from app.config import CODE_TTL_SECONDS

    return CODE_TTL_SECONDS / 60


def send_code_email(to: str, code: str, purpose: str = "register", nickname: str = "") -> None:
    """向 ``to`` 发送验证码邮件；失败抛 :class:`MailError`。"""
    host = str(SMTP_CONFIG["smtp_host"])
    port = int(SMTP_CONFIG["smtp_port"])  # type: ignore[arg-type]
    sender = str(SMTP_CONFIG["sender"])
    password = str(SMTP_CONFIG["password"])

    if not host or not sender or not password:
        raise MailError("SMTP 配置不完整：请检查 smtp_host / sender / password")

    message = _build_message(to, code, purpose, nickname)
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT)
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
            try:
                server.starttls()
            except smtplib.SMTPException:
                pass
        try:
            server.login(sender, password)
            server.sendmail(sender, [to], message.as_string())
        finally:
            try:
                server.quit()
            except Exception:  # pragma: no cover - 关闭失败不影响结果
                pass
    except MailError:
        raise
    except Exception as exc:  # pragma: no cover - 网络异常统一包装
        raise MailError(f"邮件发送失败：{exc}") from exc
