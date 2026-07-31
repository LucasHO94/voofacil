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

    # Num open-jaw a volta parte de outra cidade, e escrever só "RIO → NAP"
    # esconderia metade do itinerário que está sendo cotado.
    if q.origem_volta and q.origem_volta != q.destino:
        rota = f"{q.origem} → {q.destino} · volta {q.origem_volta} → {q.origem}"
    else:
        rota = f"{q.origem} → {q.destino}"

    linhas = [
        f"✈️ <b>{rota}</b> por <b>{fmt_brl(q.preco)}</b>",
        f"📅 {datas}",
        ROTULO_GATILHO.get(candidato.gatilho, candidato.gatilho),
    ]
    # O preço cotado é sempre por 1 passageiro (ver Watch.passageiros); num
    # grupo, o número que decide a compra é o total.
    pessoas = watch.total_passageiros
    if pessoas > 1:
        linhas.append(f"👥 {pessoas} passageiros: <b>{fmt_brl(q.preco * pessoas)}</b> no total")
    if watch.preco_limite is not None:
        linhas.append(f"Seu limite: {fmt_brl(watch.preco_limite)}")
    if candidato.baseline_min is not None:
        linhas.append(f"Menor preço registrado até hoje: {fmt_brl(candidato.baseline_min)}")
    if q.cia:
        # D15: `escalas` None (a fonte não reportou paradas) é DIFERENTE de 0
        # (voo direto de verdade). Antes ambos caíam no ramo "direto" e o alerta
        # afirmava voo direto sem ter o dado — um fato não verificado. Agora só
        # dizemos "direto" quando escalas == 0; None vira "escalas não informadas".
        if q.escalas is None:
            escalas_txt = " · escalas não informadas"
        elif q.escalas == 0:
            escalas_txt = " · direto"
        else:
            escalas_txt = f" · {q.escalas} escala(s)"
        linhas.append(f"Cia: {q.cia}{escalas_txt}")
    if candidato.confirmado_ao_vivo:
        linhas.append("✔️ Preço confirmado ao vivo agora")
    else:
        linhas.append("⚠️ Preço do cache (pode ter mudado) — confirme no link")
    linhas.append(f"🔎 Monitoramento: {watch.nome}")

    assunto = f"✈️ {q.origem}→{q.destino} {fmt_brl(q.preco)} — {candidato.gatilho}"
    return Mensagem(assunto=assunto, html="\n".join(linhas), deep_link=q.deep_link)
