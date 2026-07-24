"""Configuração central do VooFacil — tudo vem do .env (nunca hardcode de segredo)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Raiz do projeto (pasta que contém voofacil/)
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Fontes de dados
    tp_token: str = ""            # Travelpayouts / Aviasales Data API
    serpapi_key: str = ""         # SerpApi (confirmação + explore)

    # Alertas
    telegram_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_to: str = ""

    # Banco
    database_url: str = ""

    # Regras de negócio (defaults sensatos, ajustáveis via .env)
    moeda_padrao: str = "BRL"
    cooldown_horas: int = 12          # não re-alertar mesma rota/data nesse intervalo
    warmup_snapshots: int = 5         # mínimo de snapshots antes do gatilho trailing-min
    warmup_percentil: int = 20        # mínimo de snapshots p/ o gatilho percentil (~3-6 semanas de coleta)
    margem_novo_minimo: float = 0.02  # preço precisa ser 2% menor que o mínimo histórico
    fator_error_fare: float = 0.5     # preço <= 50% do baseline => error fare
    serpapi_orcamento_mes: int = 250  # free tier

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        data_dir = BASE_DIR / "data"
        data_dir.mkdir(exist_ok=True)
        return f"sqlite:///{data_dir / 'voofacil.db'}"


settings = Settings()
