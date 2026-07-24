"""Contrato dos provedores de preço — a engine só conhece este protocolo."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from voofacil.models import Watch
from voofacil.schemas import PriceQuote


@runtime_checkable
class Provider(Protocol):
    """Um provedor varre um watch e devolve cotações no formato canônico."""

    nome: str

    def sweep(self, watch: Watch) -> list[PriceQuote]:
        """Busca preços para todos os destinos/meses da janela do watch."""
        ...
