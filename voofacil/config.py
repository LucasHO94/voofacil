"""Configuração central do VooFacil — tudo vem do .env (nunca hardcode de segredo)."""
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

# Raiz do projeto (pasta que contém voofacil/)
BASE_DIR = Path(__file__).resolve().parent.parent


def _normalizar_url_postgres(url: str) -> str:
    """Reescreve o scheme do Postgres para o dialeto que o projeto realmente tem.

    O Neon (e o Heroku, e a maioria dos painéis) oferece a connection string como
    `postgres://...` ou `postgresql://...`. O SQLAlchemy 2.0 mapeia AMBOS para o
    driver `psycopg2`, que NÃO está instalado — o requirements.txt só traz o
    `psycopg` v3, cujo dialeto é `postgresql+psycopg://`. Copiar a URL crua do Neon
    para o secret, então, fazia `create_engine` estourar `ModuleNotFoundError:
    psycopg2` no worker: `job_varredura` capturava, dava rollback e devolvia False,
    o horário nunca era carimbado e a automação parecia morta. Reescrever o prefixo
    aqui torna as três formas equivalentes e à prova desse erro de configuração.
    """
    for cru, dialeto in (("postgresql://", "postgresql+psycopg://"),
                         ("postgres://", "postgresql+psycopg://")):
        if url.startswith(cru):
            return dialeto + url[len(cru):]
    return url


def descricao_banco(url: str) -> str:
    """`esquema://host/banco` — sem usuário nem senha, seguro para log.

    Existe para o worker anunciar no arranque CONTRA QUAL banco vai rodar: é o
    sinal que separa "está gravando no Neon" de "caiu no SQLite efêmero do
    runner" num relance do log do GitHub Actions."""
    if url.startswith("sqlite"):
        return "SQLite (arquivo local — efêmero no runner do GitHub Actions)"
    u = urlparse(url)
    return f"{u.scheme}://{u.hostname or '?'}/{(u.path or '').lstrip('/') or '?'}"


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
    serpapi_orcamento_dia: int = 8    # teto diário adicional (independente do mensal)

    # Agendamento das varreduras (configurável — ver voofacil/schedule_utils.py)
    # Padrão espalhado (madrugada/meio-dia/noite): não há evidência forte de que
    # UMA hora do dia tenha preço menor — o que a pesquisa confirma é que dia da
    # semana e antecedência da compra importam mais. O espalhamento aqui serve
    # pra fisgar quedas de preço ao longo do dia, não para acertar hora mágica.
    scan_horarios: str = "06:00,12:30,21:00"   # CSV "HH:MM,HH:MM,..."
    scan_dias_semana: str = "*"                 # "*"=todos; ou CSV "mon,tue,wed,thu,fri,sat,sun"
    explore_horario: str = "05:00"              # busca ampla (SerpApi Explore), 1x/dia

    # Agendamento dinâmico via banco (ver voofacil/engine/agendamento.py). Como o
    # cron do GitHub Actions é YAML estático, o worker roda num POLL de frequência
    # fixa e curta e, a cada execução, decide olhando o banco se varre/explora.
    #
    # MODELO CATCH-UP (recuperação): a pergunta NÃO é mais "o poll caiu em cima do
    # horário?" (janela de tolerância — removida) e sim "algum horário PASSOU desde
    # a última vez que rodei?", via HorarioAgendado.ultima_execucao. Havia uma
    # `agendamento_tolerancia_minutos` aqui; ela sumiu junto com a janela.
    # O motivo é medido, não teórico: o GitHub Actions entrega só 4-7 dos 96
    # disparos/dia prometidos por '*/15' em repo free, em horários imprevisíveis;
    # ~5 polls aleatórios contra janelas somando 112 dos 1440 minutos do dia deram
    # ZERO varreduras em 4 dias.
    #
    # Consequência prática: a frequência do poll passou a afetar só a LATÊNCIA
    # (quão rápido um horário atrasado é recuperado), NUNCA a CORRETUDE. Se o
    # horário das 09:00 passou e o próximo poll real só acontece às 11:37, a
    # varredura acontece às 11:37 — e cada horário dispara no máximo uma vez por
    # dia, porque a comparação é sempre contra a ocorrência mais recente dele.
    # Por isso também não existe mais o risco de disparo em dobro: baixar o poll
    # é seguro, só melhora a latência.
    agendamento_poll_minutos: int = 15         # intervalo do poll (GH Actions/daemon local)

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return _normalizar_url_postgres(self.database_url)
        data_dir = BASE_DIR / "data"
        data_dir.mkdir(exist_ok=True)
        return f"sqlite:///{data_dir / 'voofacil.db'}"


settings = Settings()
