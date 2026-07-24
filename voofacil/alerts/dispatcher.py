"""Despacho de candidatos aprovados pelos canais configurados + registro em `alerta`."""
from __future__ import annotations

import logging
from typing import Protocol

from voofacil.alerts.email import EmailChannel
from voofacil.alerts.mensagem import Mensagem, montar_mensagem
from voofacil.alerts.telegram import TelegramChannel
from voofacil.models import Alerta, Watch
from voofacil.schemas import Candidato

log = logging.getLogger(__name__)


class Canal(Protocol):
    nome: str

    def enviar(self, msg: Mensagem) -> bool: ...


def canais_padrao() -> list[Canal]:
    """Só canais efetivamente configurados no .env entram na lista."""
    canais: list[Canal] = []
    tg = TelegramChannel()
    if tg.configurado:
        canais.append(tg)
    em = EmailChannel()
    if em.configurado:
        canais.append(em)
    return canais


def despachar(session, candidatos: list[Candidato], canais: list[Canal]) -> list[Alerta]:
    """Envia cada candidato por cada canal; registra Alerta por envio bem-sucedido.

    O registro em `alerta` é o que alimenta o cooldown — envio falho não
    registra, então será re-tentado na próxima varredura.
    """
    if not canais:
        if candidatos:
            log.warning("%s candidato(s) a alerta, mas nenhum canal configurado",
                        len(candidatos))
        return []

    enviados: list[Alerta] = []
    for cand in candidatos:
        watch = session.get(Watch, cand.watch_id)
        msg = montar_mensagem(cand, watch)
        for canal in canais:
            ok = canal.enviar(msg)
            if not ok:
                log.warning("Envio falhou: canal=%s watch=%s", canal.nome, cand.watch_id)
                continue
            alerta = Alerta(
                watch_id=cand.watch_id,
                snapshot_id=cand.snapshot_id,
                gatilho=cand.gatilho,
                preco=cand.quote.preco,
                confirmado_ao_vivo=cand.confirmado_ao_vivo,
                canal=canal.nome,
            )
            session.add(alerta)
            enviados.append(alerta)
    session.flush()
    return enviados
