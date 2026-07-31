"""Schemas Pydantic — contrato único entre providers, engine e alertas."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class PriceQuote(BaseModel):
    """Formato canônico que TODO provider deve produzir (análogo ao CanonicalTransaction do B2F)."""

    origem: str = Field(min_length=3, max_length=3)
    destino: str = Field(min_length=3, max_length=3)
    data_ida: date
    data_volta: date | None = None
    # Cidade de onde o trecho de volta parte, quando difere de `destino`
    # (itinerário open-jaw). None = volta sai do próprio destino, que é o caso
    # de toda cotação redonda comum. Ver Watch.origem_volta_iata.
    origem_volta: str | None = Field(default=None, min_length=3, max_length=3)
    preco: Decimal = Field(gt=0)
    moeda: str = "BRL"
    cia: str | None = None
    escalas: int | None = None
    fonte: str                      # TP_CACHE | SERPAPI
    deep_link: str | None = None
    expires_at: datetime | None = None


class Candidato(BaseModel):
    """Cotação que cruzou um gatilho e aguarda confirmação/alerta."""

    quote: PriceQuote
    watch_id: int
    snapshot_id: int
    gatilho: str                    # LIMITE | NOVO_MINIMO | PERCENTIL | ERROR_FARE
    baseline_min: Decimal | None = None   # contexto p/ mensagem ("menor preço até hoje: R$X")
    confirmado_ao_vivo: bool = False
