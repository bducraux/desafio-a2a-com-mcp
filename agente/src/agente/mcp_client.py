"""O agente como host MCP: cliente oficial do SDK `mcp` v2, falando HTTP com o servidor.

Decisão (ver README): o `Client` do SDK resolve `InputRequiredResult` sozinho quando há
`elicitation_callback`. Para a ponte funcionar, toda chamada usa `allow_input_required=True`,
que devolve o `input_required` cru ao agente; o callback existe só para declarar a
capability `elicitation.form` e falha se um dia for chamado. O retry é feito pelo agente
com `input_responses` + `request_state` ecoado — o SDK atribui um id JSON-RPC novo.

O SDK monta o `_meta` obrigatório (protocolVersion, clientCapabilities) e os headers
`MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` em cada request; o agente acrescenta o
`traceparent` (mesmo trace-id da Task, span-id novo por request).
"""
from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from typing import Any

from mcp.client import Client
from mcp_types import CallToolResult, ElicitResult, InputRequiredResult, PaginatedRequestParams

MCP_URL = os.environ.get("MCP_URL") or "http://127.0.0.1:7301/mcp"
TOOL_RESERVA = "reservar_sala"
URI_POLITICA = "politica://uso"


async def _nunca_responder(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("A elicitation volta ao cliente A2A; o agente nao responde sozinho.")


@dataclass(frozen=True)
class Trace:
    """W3C trace context: o trace-id da Task é preservado; o span-id muda a cada request."""

    trace_id: str
    flags: str = "01"

    @classmethod
    def do_header(cls, valor: str | None) -> "Trace":
        partes = (valor or "").strip().split("-")
        if len(partes) == 4 and len(partes[1]) == 32 and partes[1] != "0" * 32:
            try:
                int(partes[1], 16)
                return cls(trace_id=partes[1].lower(), flags=partes[3][:2] or "01")
            except ValueError:
                pass
        return cls(trace_id=secrets.token_hex(16))

    def meta(self) -> dict[str, str]:
        return {"traceparent": f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"}


@dataclass
class Pausa:
    """O que o agente guarda entre o input_required e o retry (opaco: nunca abre o requestState)."""

    chave: str
    alternativas: list[str]
    request_state: str
    argumentos: dict[str, str]


class ClienteMCP:
    """Mantém um `Client` do SDK vivo entre chamadas (normal e recomendado: estado de protocolo
    continua por request). A conexão é aberta e fechada pela MESMA task dona (`_dono`), como o
    anyio exige para os cancel scopes do SDK."""

    def __init__(self, url: str = MCP_URL) -> None:
        self.url = url
        self._cliente: Client | None = None
        self._dono: asyncio.Task | None = None
        self._pronto: asyncio.Future | None = None
        self._encerrar = asyncio.Event()
        self._conectando = asyncio.Lock()

    async def _manter_conexao(self) -> None:
        try:
            async with Client(self.url, elicitation_callback=_nunca_responder) as cliente:
                self._cliente = cliente
                self._pronto.set_result(cliente)
                await self._encerrar.wait()
        except BaseException as exc:
            if not self._pronto.done():
                self._pronto.set_exception(exc)
            if not isinstance(exc, Exception):
                raise
        finally:
            self._cliente = None

    async def _sessao(self):
        async with self._conectando:
            if self._dono is None or self._dono.done():
                self._pronto = asyncio.get_running_loop().create_future()
                self._dono = asyncio.create_task(self._manter_conexao())
            pronto = self._pronto
        try:
            cliente = await pronto
        except Exception:
            self._dono = None      # próxima chamada tenta conectar de novo
            raise
        return cliente.session

    async def fechar(self) -> None:
        if self._dono is not None:
            self._encerrar.set()
            await asyncio.gather(self._dono, return_exceptions=True)
            self._dono = None

    async def descobrir_ferramentas(self, trace: Trace) -> list[str]:
        """tools/list em runtime: o agente não carrega lista fixa de ferramentas."""
        sessao = await self._sessao()
        resultado = await sessao.list_tools(params=PaginatedRequestParams(meta=trace.meta()))
        return [t.name for t in resultado.tools]

    async def versao_politica(self, trace: Trace) -> str:
        """Lê o resource politica://uso e extrai a versão da primeira linha (`versao: AAAA-MM-DD`)."""
        sessao = await self._sessao()
        resultado = await sessao.read_resource(URI_POLITICA, meta=trace.meta())
        primeira = resultado.contents[0].text.splitlines()[0]
        return primeira.split(":", 1)[1].strip()

    async def reservar(self, argumentos: dict[str, str], trace: Trace) -> CallToolResult | InputRequiredResult:
        sessao = await self._sessao()
        return await sessao.call_tool(TOOL_RESERVA, argumentos, meta=trace.meta(), allow_input_required=True)

    async def retomar(self, pausa: Pausa, resposta: ElicitResult, trace: Trace) -> CallToolResult | InputRequiredResult:
        """Retry do tools/call original: mesmos argumentos, mesma chave, requestState ecoado sem modificação."""
        sessao = await self._sessao()
        return await sessao.call_tool(
            TOOL_RESERVA,
            pausa.argumentos,
            meta=trace.meta(),
            input_responses={pausa.chave: resposta},
            request_state=pausa.request_state,
            allow_input_required=True,
        )


def pausa_de(resultado: InputRequiredResult, argumentos: dict[str, str]) -> Pausa:
    """Extrai a única elicitation do input_required: chave atribuída pelo servidor e o enum de salas."""
    chave, pedido = next(iter(resultado.input_requests.items()))
    campo = (pedido.params.requested_schema.get("properties") or {}).get("sala") or {}
    alternativas = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
    return Pausa(chave=chave, alternativas=alternativas, request_state=resultado.request_state,
                 argumentos=dict(argumentos))


def aceitar(sala: str) -> ElicitResult:
    return ElicitResult(action="accept", content={"sala": sala})


def recusar() -> ElicitResult:
    return ElicitResult(action="decline")
