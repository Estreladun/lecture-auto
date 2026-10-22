"""邮件通知：只发事件摘要，正文不含密码等敏感信息。"""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage

from . import log
from .config import Smtp
from .model import SHANGHAI

TITLES = {
    "reservation_succeeded": ("预约成功", "已成功预约一场目标讲座，详见下方摘要。"),
    "login_failed": ("登录失败", "账号登录未成功，程序已停止自动尝试，请检查配置或账号状态。"),
    "session_expired": ("会话失效", "登录会话过期且自动重登失败，程序已停止。"),
    "captcha_locked": ("验证码反复失败", "连续多次无法通过图形验证码，请人工确认。"),
    "api_unavailable": ("接口不可用", "未找到可用的讲座接口，可能系统已升级，请查看日志。"),
    "daily_report": ("运行日报", "以下为程序截至当前的运行情况，时间均为北京时间。"),
    "test": ("配置测试", "邮件通道可用，你正在阅读的这封就是测试邮件。"),
}


class Mailer:
    def __init__(self, smtp: Smtp) -> None:
        self.smtp = smtp
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return True

    def send(self, event: str, body: str = "") -> bool:
        title, explain = TITLES.get(event, ("运行通知", "请查看本地运行状态。"))
        message = EmailMessage()
        message["From"] = self.smtp.sender
        message["To"] = self.smtp.recipient
        message["Subject"] = f"东南大学讲座预约：{title}"

        stamp = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        text = [explain]
        if body:
            text += ["", body]
        text += ["", f"时间：{stamp}", "本邮件由本地脚本自动发送，回复无效。"]
        message.set_content("\n".join(text))

        try:
            with smtplib.SMTP_SSL(
                self.smtp.host,
                self.smtp.port,
                timeout=20,
                context=ssl.create_default_context(),
            ) as server:
                server.login(self.smtp.username, self.smtp.password)
                server.send_message(message)
        except Exception as exc:  # 邮件失败绝不影响抢讲座主流程
            self.last_error = type(exc).__name__
            log.warn(f"邮件发送失败（{event}）：{type(exc).__name__}")
            return False
        log.info(f"邮件已发送：{title} → {self.smtp.recipient}")
        return True

    def __call__(self, event: str, body: str = "") -> bool:
        return self.send(event, body)


def build(smtp: Smtp | None) -> Mailer | None:
    if smtp is None:
        return None
    return Mailer(smtp)
