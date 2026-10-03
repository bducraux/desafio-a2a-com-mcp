"""Interpretação determinística dos textos em formato fixo (sem LLM)."""
from __future__ import annotations

import re
from dataclasses import dataclass

_PEDIDO = re.compile(
    r"^\s*reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+?)\s*$"
)
_ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")

RECUSAR = "recusar"


@dataclass(frozen=True)
class Pedido:
    sala: str
    inicio: str
    fim: str
    responsavel: str

    def argumentos(self) -> dict[str, str]:
        return {"sala": self.sala, "inicio": self.inicio, "fim": self.fim, "responsavel": self.responsavel}


def ler_pedido(texto: str) -> Pedido | None:
    m = _PEDIDO.match(texto or "")
    return Pedido(**m.groupdict()) if m else None


def ler_escolha(texto: str) -> str | None:
    m = _ESCOLHA.match(texto or "")
    return m.group("valor") if m else None
