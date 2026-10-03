"""Middleware ASGI que registra no stderr cada request JSON-RPC recebido em /mcp.

Registra método, id, nome (tool/URI) e o `traceparent` do `_meta`, inclusive dos requests
que o SDK rejeita depois (ex.: `_meta` incompleto), porque lê o corpo antes de repassar.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone


class RequestLogMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        corpo = b""
        mensagens = []
        while True:
            msg = await receive()
            mensagens.append(msg)
            if msg["type"] == "http.request":
                corpo += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            else:
                break
        cabecalhos = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        self._registrar(corpo, cabecalhos)

        async def replay():
            return mensagens.pop(0) if mensagens else await receive()

        await self.app(scope, replay, send)

    @staticmethod
    def _registrar(corpo: bytes, cabecalhos: dict[str, str]) -> None:
        try:
            dados = json.loads(corpo or b"{}")
        except ValueError:
            print(f"[mcp] corpo nao-JSON ({len(corpo)} bytes)", file=sys.stderr, flush=True)
            return
        for req in dados if isinstance(dados, list) else [dados]:
            if not isinstance(req, dict):
                continue
            params = req.get("params") or {}
            meta = params.get("_meta") or {}
            nome = params.get("name") or params.get("uri") or "-"
            retry = " retry" if "requestState" in params else ""
            agora = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            print(f"[mcp] {agora} method={req.get('method')} id={req.get('id')} name={nome}{retry} "
                  f"traceparent={meta.get('traceparent', '-')} "
                  f"headers=[MCP-Protocol-Version={cabecalhos.get('mcp-protocol-version', '-')} "
                  f"Mcp-Method={cabecalhos.get('mcp-method', '-')} Mcp-Name={cabecalhos.get('mcp-name', '-')}]",
                  file=sys.stderr, flush=True)
