"""Email alerting module for Oracle DB Agent."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from oracle_db_agent.config import AgentConfig

logger = logging.getLogger("oracle_db_agent.alerting")


class EmailAlerter:
    """Sends alert emails via SMTP."""

    def __init__(self, config: AgentConfig) -> None:
        self._email_cfg = config.alerts.email

    @property
    def enabled(self) -> bool:
        return self._email_cfg.enabled

    def send_alert(
        self,
        subject: str,
        body_html: str,
        body_text: str | None = None,
        recipients: list[str] | None = None,
    ) -> bool:
        """Send an alert email. Returns True on success."""
        if not self.enabled:
            logger.info("Email alerting disabled; skipping alert: %s", subject)
            return False

        to_addrs = recipients or self._email_cfg.recipients
        if not to_addrs:
            logger.warning("No email recipients configured; skipping alert: %s", subject)
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Oracle DB Agent] {subject}"
        msg["From"] = self._email_cfg.from_address
        msg["To"] = ", ".join(to_addrs)

        if body_text:
            msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        try:
            if self._email_cfg.smtp_use_tls:
                server = smtplib.SMTP(self._email_cfg.smtp_host, self._email_cfg.smtp_port)
                server.starttls()
            else:
                server = smtplib.SMTP(self._email_cfg.smtp_host, self._email_cfg.smtp_port)

            username = self._email_cfg.smtp_username
            password = self._email_cfg.smtp_password
            if username and password:
                server.login(username, password)

            server.sendmail(self._email_cfg.from_address, to_addrs, msg.as_string())
            server.quit()
            logger.info("Alert email sent: %s -> %s", subject, to_addrs)
            return True
        except Exception as exc:
            logger.error("Failed to send alert email '%s': %s", subject, exc)
            return False

    def send_report(
        self,
        subject: str,
        sections: list[dict[str, str]],
        recipients: list[str] | None = None,
    ) -> bool:
        """Send a structured report email with multiple sections.

        Each section is a dict with keys 'title' and 'content' (HTML).
        """
        html_parts = [
            "<html><body>",
            f"<h2>{subject}</h2>",
            "<hr/>",
        ]
        text_parts = [subject, "=" * len(subject), ""]

        for section in sections:
            title = section.get("title", "")
            content = section.get("content", "")
            html_parts.append(f"<h3>{title}</h3>")
            html_parts.append(content)
            html_parts.append("<br/>")
            text_parts.append(f"\n{title}\n{'-' * len(title)}\n{content}\n")

        html_parts.append("</body></html>")

        return self.send_alert(
            subject=subject,
            body_html="\n".join(html_parts),
            body_text="\n".join(text_parts),
            recipients=recipients,
        )
