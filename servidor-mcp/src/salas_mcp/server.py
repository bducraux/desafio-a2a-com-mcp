"""Servidor MCP da Central de Salas (Streamable HTTP, revisão 2026-07-28 da spec).

MRTR na reserva: o parâmetro `escolha` de `reservar_sala` é preenchido pelo resolver
`escolha_de_sala`. Quando o intervalo conflita e há alternativas, o resolver devolve
`Elicit(...)`; o SDK encerra a resposta com `resultType: input_required` (sem canal de
volta) e um `requestState` selado com `RequestStateSecurity` (AES-GCM, chave de
REQUEST_STATE_SECRET, expiração e vínculo com método, tool e argumentos). No retry o SDK
verifica o estado, roda o resolver de novo e entrega a resposta do usuário à tool.
"""
from __future__ import annotations

import os
import sys
from typing import Annotated, Any, Literal

import uvicorn
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.request_state import RequestStateSecurity
from pydantic import BaseModel, Field, create_model

from . import dominio
from .logging_mw import RequestLogMiddleware

PORTA = int(os.environ.get("MCP_PORT") or "7301")
HOST = os.environ.get("MCP_HOST") or "127.0.0.1"
VALIDADE_REQUEST_STATE_S = 15 * 60   # 15 minutos (o enunciado pede entre 5 e 30)
MENSAGEM_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


def _seguranca_request_state() -> RequestStateSecurity:
    segredo = os.environ.get("REQUEST_STATE_SECRET") or ""
    if len(segredo.encode()) < 32:
        sys.exit("REQUEST_STATE_SECRET ausente ou curta (minimo 32 bytes). "
                 "Gere com: python3 -c \"import secrets; print(secrets.token_hex(32))\"")
    return RequestStateSecurity(keys=[segredo], ttl=VALIDADE_REQUEST_STATE_S)


agenda = dominio.Agenda()

mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    instructions="Consulta e reserva de salas de reuniao da Hill Valley Tech.",
    request_state_security=_seguranca_request_state(),
)


# ----------------------------------------------------------------- modelos de saída

class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ----------------------------------------------------------------- MRTR: resolver da escolha

def _schema_escolha(alternativas: list[str]) -> type[BaseModel]:
    """Schema plano: uma propriedade `sala` restrita às alternativas, na ordem da regra."""
    tipo = Literal[tuple(alternativas)]  # type: ignore[valid-type]
    return create_model(
        "EscolhaDeSala",
        sala=(tipo, Field(title="Sala", description="Sala alternativa escolhida")),
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Any:
    """Pergunta qual alternativa usar quando o intervalo pedido conflita.

    Roda antes do corpo da tool (e de novo no retry). Se o pedido for inválido, estiver
    livre ou não tiver alternativa, não pergunta nada: o corpo da tool decide o resultado.
    """
    try:
        ini, fi = dominio.validar(sala, inicio, fim)
    except dominio.ErroDeRegra:
        return None
    if not agenda.conflitos(sala, ini, fi):
        return None
    alternativas = agenda.alternativas(sala, ini, fi)
    if not alternativas:
        return None
    return Elicit(MENSAGEM_ELICITATION, _schema_escolha(alternativas))


# ----------------------------------------------------------------- tools

@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**dominio.como_dict(s)) for s in dominio.SALAS.values()])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    try:
        ini, fi = dominio.validar(sala, inicio, fim)
    except dominio.ErroDeRegra as e:
        raise ToolError(str(e)) from None
    conflitos = [ConflitoOut(id=r.id, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel)
                 for r in agenda.conflitos(sala, ini, fi)]
    return Disponibilidade(sala=sala, livre=not conflitos, conflitos=conflitos)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)],
) -> ReservaOut:
    try:
        ini, fi = dominio.validar(sala, inicio, fim)
    except dominio.ErroDeRegra as e:
        raise ToolError(str(e)) from None

    destino = sala
    if agenda.conflitos(sala, ini, fi):
        if not isinstance(escolha, AcceptedElicitation) or escolha.data is None:
            if escolha.action in ("decline", "cancel"):
                return ReservaOut(reservado=False, motivo="recusado")
            raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
        destino = escolha.data.sala

    reserva = agenda.reservar(destino, inicio, fim, responsavel)
    if reserva is None:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    return ReservaOut(reserva=reserva.id, reservado=True, sala=reserva.sala, inicio=reserva.inicio,
                      fim=reserva.fim, responsavel=reserva.responsavel, politica=dominio.versao_politica())


# ----------------------------------------------------------------- resource

@mcp.resource("politica://uso", name="politica-de-uso", description="Politica de uso das salas.",
              mime_type="text/markdown")
def politica_de_uso() -> str:
    return dominio.texto_politica()


def app():
    """App ASGI: Streamable HTTP stateless, respostas JSON, log de cada request no stderr."""
    return RequestLogMiddleware(mcp.streamable_http_app(json_response=True, stateless_http=True))


def main() -> None:
    print(f"[salas-mcp] ouvindo em http://{HOST}:{PORTA}/mcp", file=sys.stderr, flush=True)
    uvicorn.run(app(), host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
