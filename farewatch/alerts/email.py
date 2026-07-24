"""Canal e-mail via SMTP (Gmail com senha de app, ou qualquer SMTP)."""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

from farewatch.alerts.mensagem import Mensagem
from farewatch.config import settings

log = logging.getLogger(__name__)


def _html_email(msg: Mensagem) -> str:
    corpo = msg.html.replace("\n", "<br>\n")
    botao = ""
    if msg.deep_link:
        botao = (
            f'<p><a href="{msg.deep_link}" '
            'style="background:#0a7cff;color:#fff;padding:10px 18px;'
            'border-radius:6px;text-decoration:none;display:inline-block">'
            "🛒 Ver oferta / comprar</a></p>"
        )
    return (
        '<div style="font-family:sans-serif;max-width:520px">'
        f"<p>{corpo}</p>{botao}"
        '<p style="color:#888;font-size:12px">FareWatch — seu monitor de passagens</p>'
        "</div>"
    )


class EmailChannel:
    nome = "EMAIL"

    def __init__(self, host: str | None = None, port: int | None = None,
                 user: str | None = None, password: str | None = None,
                 destinatario: str | None = None):
        self.host = host or settings.smtp_host
        self.port = port or settings.smtp_port
        self.user = user or settings.smtp_user
        self.password = password or settings.smtp_password
        self.destinatario = destinatario or settings.alert_email_to

    @property
    def configurado(self) -> bool:
        return bool(self.user and self.password and self.destinatario)

    def enviar(self, msg: Mensagem) -> bool:
        if not self.configurado:
            log.warning("E-mail não configurado (SMTP_USER/SMTP_PASSWORD/ALERT_EMAIL_TO)")
            return False
        mime = MIMEText(_html_email(msg), "html", "utf-8")
        mime["Subject"] = msg.assunto
        mime["From"] = self.user
        mime["To"] = self.destinatario
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(self.user, self.password)
                smtp.sendmail(self.user, [self.destinatario], mime.as_string())
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.error("Falha ao enviar e-mail: %s", exc)
            return False
