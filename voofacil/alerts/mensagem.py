"""Montagem da mensagem de alerta — compartilhada por Telegram e e-mail."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from voofacil.models import Gatilho, Watch
from voofacil.schemas import Candidato

ROTULO_GATILHO = {
    Gatilho.ERROR_FARE.value: "🚨 POSSÍVEL TARIFA-ERRO (compre rápido, pode durar minutos)",
    Gatilho.LIMITE.value: "✅ Abaixo do seu limite",
    Gatilho.NOVO_MINIMO.value: "📉 Novo menor preço já registrado",
    Gatilho.PERCENTIL.value: "💰 Entre os mais baratos do histórico",
}


def fmt_brl(valor: Decimal) -> str:
    """R$ 3.123,45 (formato brasileiro)."""
    s = f"{valor:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")
    return f"R$ {s}"


@dataclass(frozen=True)
class Mensagem:
    assunto: str
    html: str            # HTML aceito pelo Telegram (<b>, <i>, <a>) e reusado no e-mail
    deep_link: str | None


def montar_mensagem(candidato: Candidato, watch: Watch) -> Mensagem:
    q = candidato.quote
    datas = q.data_ida.strftime("%d/%m/%Y")
    if q.data_volta:
        datas += f" → volta {q.data_volta.strftime('%d/%m/%Y')}"

    linhas = [
        f"✈️ <b>{q.origem} → {q.destino}</b> por <b>{fmt_brl(q.preco)}</b>",
        f"📅 {datas}",
        ROTULO_GATILHO.get(candidato.gatilho, candidato.gatilho),
    ]
    if watch.preco_limite is not None:
        linhas.append(f"Seu limite: {fmt_brl(watch.preco_limite)}")
    if candidato.baseline_min is not None:
        linhas.append(f"Menor preço registrado até hoje: {fmt_brl(candidato.baseline_min)}")
    if q.cia:
        linhas.append(f"Cia: {q.cia}" + (f" · {q.escalas} escala(s)" if q.escalas else " · direto"))
    if candidato.confirmado_ao_vivo:
        linhas.append("✔️ Preço confirmado ao vivo agora")
    else:
        linhas.append("⚠️ Preço do cache (pode ter mudado) — confirme no link")
    linhas.append(f"🔎 Monitoramento: {watch.nome}")

    assunto = f"✈️ {q.origem}→{q.destino} {fmt_brl(q.preco)} — {candidato.gatilho}"
    return Mensagem(assunto=assunto, html="\n".join(linhas), deep_link=q.deep_link)
