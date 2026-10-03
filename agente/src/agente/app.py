"""O agente como servidor A2A v1.0 (binding JSON-RPC 2.0 sobre HTTP).

- GET  /.well-known/agent-card.json  → Agent Card v1.0
- POST /a2a                          → SendMessage e GetTask
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .mcp_client import ClienteMCP
from .tasks import ErroA2A, Tasks, publico

PORTA = int(os.environ.get("AGENTE_PORT") or "7300")
HOST = os.environ.get("AGENTE_HOST") or "127.0.0.1"
URL_PUBLICA = (os.environ.get("AGENTE_URL") or f"http://127.0.0.1:{PORTA}").rstrip("/")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

cliente_mcp = ClienteMCP()
tarefas = Tasks(cliente_mcp)


def agent_card() -> dict:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


def _erro(id_req, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_req, "error": {"code": codigo, "message": mensagem}})


async def card(_request: Request) -> JSONResponse:
    return JSONResponse(agent_card())


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = json.loads(await request.body())
    except ValueError:
        return _erro(None, PARSE_ERROR, "JSON invalido")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or "method" not in corpo:
        return _erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Request JSON-RPC invalido")

    id_req, metodo, params = corpo.get("id"), corpo["method"], corpo.get("params") or {}
    traceparent = request.headers.get("traceparent")
    print(f"[agente] a2a method={metodo} id={id_req} traceparent={traceparent or '-'}", file=sys.stderr, flush=True)
    try:
        if metodo == "SendMessage":
            mensagem = params.get("message")
            if not isinstance(mensagem, dict):
                return _erro(id_req, INVALID_PARAMS, "params.message e obrigatorio")
            task = await tarefas.receber(mensagem, traceparent)
        elif metodo == "GetTask":
            if not params.get("id"):
                return _erro(id_req, INVALID_PARAMS, "params.id e obrigatorio")
            task = tarefas.obter(params["id"])
        else:
            return _erro(id_req, METHOD_NOT_FOUND, f"Metodo nao suportado: {metodo}")
    except ErroA2A as e:
        return _erro(id_req, e.codigo, e.mensagem)
    return JSONResponse({"jsonrpc": "2.0", "id": id_req, "result": {"task": publico(task)}})


@asynccontextmanager
async def ciclo_de_vida(_app):
    yield
    await cliente_mcp.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", card, methods=["GET"]),
        Route("/a2a", a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


def main() -> None:
    print(f"[agente] A2A em {URL_PUBLICA}/a2a | MCP em {cliente_mcp.url}", file=sys.stderr, flush=True)
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
