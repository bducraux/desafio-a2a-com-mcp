"""Regras de sala: validação, conflito e alternativas (funções puras + estado em memória).

As salas, as reservas iniciais e a política vêm de `dados/` (somente leitura). Reservas
criadas pelo processo ficam em memória e somem no restart, como o enunciado permite.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS_DIR = Path(os.environ.get("SALAS_DADOS_DIR") or Path(__file__).resolve().parents[3] / "dados")

FUSO_SP = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class ErroDeRegra(Exception):
    """Erro de execução da tool: vira `isError: true` com esta mensagem exata."""


@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


@dataclass(frozen=True)
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str


def _ler(nome: str):
    return json.loads((DADOS_DIR / nome).read_text(encoding="utf-8"))


SALAS: dict[str, Sala] = {s["id"]: Sala(**s) for s in _ler("salas.json")}


def texto_politica() -> str:
    return (DADOS_DIR / "politica-de-uso.md").read_text(encoding="utf-8")


def versao_politica() -> str:
    primeira = texto_politica().splitlines()[0]
    return primeira.split(":", 1)[1].strip()


class Agenda:
    """Reservas em memória (iniciais de dados/reservas.json + criadas pelo processo)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reservas: list[Reserva] = [Reserva(**r) for r in _ler("reservas.json")]
        self._seq = max(int(r.id.split("-")[1]) for r in self._reservas) if self._reservas else 0

    def conflitos(self, sala: str, inicio: datetime, fim: datetime) -> list[Reserva]:
        """Intervalos meio-abertos: conflita se inicio < outra.fim e outra.inicio < fim."""
        return [r for r in self._reservas
                if r.sala == sala and inicio < _instante(r.fim) and _instante(r.inicio) < fim]

    def alternativas(self, sala: str, inicio: datetime, fim: datetime) -> list[str]:
        """Salas livres no intervalo com capacidade >= a pedida, por capacidade e depois id, no máximo 3."""
        minima = SALAS[sala].capacidade
        candidatas = [s for s in SALAS.values()
                      if s.id != sala and s.capacidade >= minima and not self.conflitos(s.id, inicio, fim)]
        candidatas.sort(key=lambda s: (s.capacidade, s.id))
        return [s.id for s in candidatas[:MAX_ALTERNATIVAS]]

    def reservar(self, sala: str, inicio: str, fim: str, responsavel: str) -> Reserva | None:
        """Grava se ainda estiver livre (checagem e escrita sob o mesmo lock)."""
        ini, fi = _instante(inicio), _instante(fim)
        with self._lock:
            if self.conflitos(sala, ini, fi):
                return None
            self._seq += 1
            reserva = Reserva(f"res-{self._seq:04d}", sala, inicio, fim, responsavel)
            self._reservas.append(reserva)
            return reserva


def _instante(texto: str) -> datetime:
    dt = datetime.fromisoformat(texto)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO_SP)
    return dt


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Validações na ordem: sala, intervalo, janela, duração. Levanta ErroDeRegra."""
    if sala not in SALAS:
        raise ErroDeRegra(f"Sala inexistente: {sala}")
    try:
        ini, fi = _instante(inicio), _instante(fim)
    except (TypeError, ValueError):
        raise ErroDeRegra(ERRO_INTERVALO) from None
    if fi <= ini:
        raise ErroDeRegra(ERRO_INTERVALO)
    ini_sp, fim_sp = ini.astimezone(FUSO_SP), fi.astimezone(FUSO_SP)
    fora = (
        ini_sp.time() < JANELA_INICIO
        or fim_sp.date() != ini_sp.date()
        or fim_sp.time() > JANELA_FIM
    )
    if fora:
        raise ErroDeRegra(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ErroDeRegra(ERRO_DURACAO)
    return ini, fi


def como_dict(obj) -> dict:
    return asdict(obj)
