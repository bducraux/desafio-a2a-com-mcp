"""Tasks A2A em memória e a ponte com o MCP.

A ponte:
- `iniciar` chama `reservar_sala`; quando o MCP devolve `input_required`, a Task vai para
  TASK_STATE_INPUT_REQUIRED com a linha `alternativas: …` e a `Pausa` (com o requestState)
  é guardada em `_pausas[task_id]` — fora do objeto Task que é serializado ao cliente A2A.
- `continuar` recebe `escolha=<valor>`; escolha válida ou `recusar` vira `inputResponses` e o
  agente repete o `tools/call` com id novo levando o `requestState` ecoado.

O agente não aplica regra de sala: conflito, política e alternativas vêm do servidor MCP.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
from typing import Any

from mcp_types import CallToolResult, InputRequiredResult

from . import parser
from .mcp_client import ClienteMCP, Pausa, Trace, aceitar, pausa_de, recusar

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = {COMPLETED, CANCELED, FAILED}

FORMATO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
PREFIXO_ERRO_SDK = "Error executing tool "


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


TASK_NAO_ENCONTRADA = -32001
OPERACAO_NAO_SUPORTADA = -32004


def _id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def _log(task: dict, texto: str) -> None:
    print(f"[agente] {task['id']} {texto}", file=sys.stderr, flush=True)


class Tasks:
    def __init__(self, mcp: ClienteMCP) -> None:
        self.mcp = mcp
        self._tasks: dict[str, dict[str, Any]] = {}
        self._pausas: dict[str, Pausa] = {}          # requestState fica aqui, nunca no Task serializado
        self._traces: dict[str, Trace] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ consulta

    def obter(self, task_id: str) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is None:
            raise ErroA2A(TASK_NAO_ENCONTRADA, f"Task nao encontrada: {task_id}")
        return task

    # ------------------------------------------------------------------ SendMessage

    async def receber(self, mensagem: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
        task_id = mensagem.get("taskId")
        if task_id:
            return await self._continuar(task_id, mensagem, traceparent)
        return await self._iniciar(mensagem, traceparent)

    async def _iniciar(self, mensagem: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
        task = {"id": _id("task"), "contextId": mensagem.get("contextId") or _id("ctx"),
                "status": {"state": SUBMITTED}, "history": [self._registrar_usuario(mensagem, None)],
                "artifacts": []}
        self._tasks[task["id"]] = task
        self._locks[task["id"]] = asyncio.Lock()
        self._traces[task["id"]] = Trace.do_header(traceparent)
        _log(task, f"{SUBMITTED} trace-id={self._traces[task['id']].trace_id}")

        async with self._locks[task["id"]]:
            self._mudar(task, WORKING)
            pedido = parser.ler_pedido(self._texto(mensagem))
            if pedido is None:
                self._mudar(task, FAILED, f"Pedido invalido. Use: {FORMATO_PEDIDO}")
                return task
            try:
                trace = self._traces[task["id"]]
                ferramentas = await self.mcp.descobrir_ferramentas(trace)
                if "reservar_sala" not in ferramentas:
                    self._mudar(task, FAILED, "O servidor MCP nao oferece a ferramenta reservar_sala")
                    return task
                politica = await self.mcp.versao_politica(trace)
                task["_politica"] = politica
                resultado = await self.mcp.reservar(pedido.argumentos(), trace)
                self._aplicar(task, resultado, pedido.argumentos())
            except Exception as exc:   # falha de transporte/protocolo com o servidor MCP
                self._mudar(task, FAILED, f"Falha ao falar com o servidor MCP: {exc}")
        return task

    async def _continuar(self, task_id: str, mensagem: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
        task = self.obter(task_id)
        async with self._locks[task_id]:
            estado = task["status"]["state"]
            if estado in TERMINAIS:
                raise ErroA2A(OPERACAO_NAO_SUPORTADA,
                              f"Task {task_id} esta em estado terminal ({estado}) e nao aceita novas mensagens")
            if estado != INPUT_REQUIRED:
                raise ErroA2A(OPERACAO_NAO_SUPORTADA, f"Task {task_id} nao esta aguardando resposta ({estado})")

            task["history"].append(self._registrar_usuario(mensagem, task))
            pausa = self._pausas[task_id]
            escolha = parser.ler_escolha(self._texto(mensagem))
            if escolha != parser.RECUSAR and escolha not in pausa.alternativas:
                # Fora do enum: continua pausada e repete exatamente a mesma linha.
                self._mudar(task, INPUT_REQUIRED, self._linha_alternativas(pausa))
                return task

            if traceparent:
                self._traces[task_id] = Trace.do_header(traceparent)
            self._mudar(task, WORKING)
            resposta = recusar() if escolha == parser.RECUSAR else aceitar(escolha)
            try:
                resultado = await self.mcp.retomar(pausa, resposta, self._traces[task_id])
                self._pausas.pop(task_id, None)
                self._aplicar(task, resultado, pausa.argumentos)
            except Exception as exc:
                self._mudar(task, FAILED, f"Falha ao falar com o servidor MCP: {exc}")
        return task

    # ------------------------------------------------------------------ tradução MCP → A2A

    def _aplicar(self, task: dict[str, Any], resultado: Any, argumentos: dict[str, str]) -> None:
        if isinstance(resultado, InputRequiredResult):
            # A PONTE: input_required do MCP vira pausa da Task; o requestState fica guardado
            # por Task (opaco), e a pergunta volta ao cliente A2A como a lista de alternativas.
            pausa = pausa_de(resultado, argumentos)
            self._pausas[task["id"]] = pausa
            self._mudar(task, INPUT_REQUIRED, self._linha_alternativas(pausa))
            return

        if not isinstance(resultado, CallToolResult):
            self._mudar(task, FAILED, "Resposta inesperada do servidor MCP")
            return

        texto = " ".join(getattr(c, "text", "") for c in resultado.content).strip()
        if resultado.is_error:
            if texto.startswith(PREFIXO_ERRO_SDK) and ": " in texto:
                texto = texto.split(": ", 1)[1]     # mensagem exata da tool, sem o prefixo do SDK
            self._mudar(task, FAILED, texto)
            return

        dados = resultado.structured_content or {}
        if not dados.get("reservado"):
            self._mudar(task, CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusado'}.")
            return

        reserva = {"reserva": dados["reserva"], "sala": dados["sala"], "inicio": dados["inicio"],
                   "fim": dados["fim"], "responsavel": dados["responsavel"], "politica": task.get("_politica")}
        task["artifacts"].append({"artifactId": _id("art"), "name": "reserva",
                                  "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}]})
        self._mudar(task, COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    # ------------------------------------------------------------------ utilitários

    @staticmethod
    def _linha_alternativas(pausa: Pausa) -> str:
        return "alternativas: " + ", ".join(pausa.alternativas)

    @staticmethod
    def _texto(mensagem: dict[str, Any]) -> str:
        return " ".join(p.get("text", "") for p in mensagem.get("parts") or [] if isinstance(p, dict)).strip()

    @staticmethod
    def _registrar_usuario(mensagem: dict[str, Any], task: dict[str, Any] | None) -> dict[str, Any]:
        registro = {"messageId": mensagem.get("messageId") or _id("msg"), "role": "ROLE_USER",
                    "parts": mensagem.get("parts") or []}
        if task is not None:
            registro["taskId"] = task["id"]
        return registro

    def _mudar(self, task: dict[str, Any], estado: str, texto: str | None = None) -> None:
        if task["status"]["state"] in TERMINAIS:
            raise RuntimeError("estado terminal e definitivo")
        status: dict[str, Any] = {"state": estado}
        if texto is not None:
            msg = {"messageId": _id("msg"), "role": "ROLE_AGENT", "parts": [{"text": texto}],
                   "taskId": task["id"], "contextId": task["contextId"]}
            status["message"] = msg
            task["history"].append(msg)
        task["status"] = status
        _log(task, estado + (f" | {texto}" if texto else ""))


def publico(task: dict[str, Any]) -> dict[str, Any]:
    """Visão da Task enviada ao cliente A2A (sem campos internos)."""
    return {k: v for k, v in task.items() if not k.startswith("_")}
