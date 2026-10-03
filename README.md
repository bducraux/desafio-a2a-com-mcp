# A Ponte: agente A2A com MCP por dentro

Dois processos independentes para a Central de Salas da Hill Valley Tech:

- **`servidor-mcp/`**: servidor MCP (SDK oficial `mcp==2.3.0`, revisão `2026-07-28`) em Streamable HTTP na porta `7301`, endpoint `/mcp`. Tem 3 tools, o resource `politica://uso` e o ciclo de MRTR em `reservar_sala`.
- **`agente/`**: agente que é **cliente MCP por dentro**, usando o `Client` do mesmo SDK por HTTP, e **servidor A2A v1.0 por fora**, em JSON-RPC na porta `7300`, com `/a2a` e `/.well-known/agent-card.json`. Não usa LLM: interpreta o pedido em formato fixo e decide por regra.

```
servidor-mcp/src/salas_mcp/
├── server.py       # MCPServer: tools, resource, resolver de MRTR, RequestStateSecurity, app ASGI
├── dominio.py      # regras de sala (validação, conflito, alternativas) e reservas em memória
└── logging_mw.py   # log no stderr de cada request: método, id, traceparent e headers
agente/src/agente/
├── app.py          # servidor A2A (Starlette): agent card, SendMessage, GetTask
├── tasks.py        # Tasks em memória e a ponte input_required ⇄ TASK_STATE_INPUT_REQUIRED
├── mcp_client.py   # cliente MCP (SDK v2) com allow_input_required, retry e traceparent
└── parser.py       # pedidos "reservar sala=… inicio=… fim=… responsavel=…" e "escolha=…"
```

## Como rodar

Pré-requisitos: Python 3.12 e [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/bducraux/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
uv sync
```

**Terminal 1, servidor MCP.** A chave de integridade do `requestState` vem só do ambiente, com no mínimo 32 bytes. Gere uma chave sua e exporte-a no mesmo terminal. Nunca a versione.

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
uv run salas-mcp          # http://127.0.0.1:7301/mcp — o stderr mostra cada request
```

Se você reiniciar o servidor e quiser que os `requestState` já emitidos continuem válidos, reinicie no **mesmo terminal**, mantendo a mesma variável. A validade de cada token é de 15 minutos.

**Terminal 2, agente** (suba depois do MCP):

```bash
uv run agente-salas       # A2A em http://127.0.0.1:7300/a2a, card em /.well-known/agent-card.json
```

**Terminal 3, validador** (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais, com os padrões do desafio:

| Variável | Padrão |
| --- | --- |
| `MCP_PORT` / `MCP_HOST` | `7301` / `127.0.0.1` |
| `AGENTE_PORT` / `AGENTE_HOST` | `7300` / `127.0.0.1` |
| `MCP_URL` | `http://127.0.0.1:7301/mcp` |
| `AGENTE_URL` | `http://127.0.0.1:7300` (URL pública que vai no card) |

Exemplo manual de pausa e continuação:

```bash
curl -s localhost:7300/a2a -H 'Content-Type: application/json' -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
  -d @<(python3 -c "import json;print(json.dumps(json.load(open('exemplos/wire/08-a2a-send-message.json'))['request']['body']))")
# → TASK_STATE_INPUT_REQUIRED, mensagem "alternativas: sala-fusca, sala-mirante"
curl -s localhost:7300/a2a -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{"messageId":"m2","role":"ROLE_USER","parts":[{"text":"escolha=sala-mirante"}],"taskId":"<id da task>"}}}'
```

## Onde a ponte acontece

**MCP → A2A (pausa).** Em `agente/src/agente/tasks.py`, no método `Tasks._aplicar`, o resultado do `tools/call` é testado com `isinstance(resultado, InputRequiredResult)`. Quando o servidor MCP respondeu `input_required`, a função `pausa_de` (em `agente/src/agente/mcp_client.py`) extrai a chave atribuída pelo servidor e o `enum` de salas. A `Pausa`, que contém o `requestState` opaco, é guardada em `Tasks._pausas[task_id]`, e a Task vai para `TASK_STATE_INPUT_REQUIRED` com a mensagem exata `alternativas: <enum na ordem recebida>`. A `Pausa` fica num dicionário separado do objeto Task que é serializado. Por isso o `requestState` nunca aparece no card, no artifact ou em mensagens A2A: o agente só guarda e ecoa, nunca abre o token.

**A2A → MCP (retomada).** Em `Tasks._continuar`, um `SendMessage` com `taskId` e `escolha=<valor>` é validado contra o enum guardado. Um valor fora do enum mantém a Task pausada e repete a linha. Uma escolha válida vira `action: accept`, e `recusar` vira `action: decline`. Em seguida `ClienteMCP.retomar` (`agente/src/agente/mcp_client.py`) repete o `tools/call` original com os mesmos argumentos, `input_responses={<mesma chave>: …}` e `request_state=<o mesmo token, sem modificação>`. O SDK emite esse retry com um **id JSON-RPC novo**. O resultado volta ao `_aplicar`: `COMPLETED` com o artifact `reserva`, `CANCELED` na recusa ou `FAILED` em erro da tool.

**Do lado do servidor**, em `servidor-mcp/src/salas_mcp/server.py`, o parâmetro `escolha` de `reservar_sala` é anotado com `Resolve(escolha_de_sala)`. O resolver valida o pedido e calcula conflito e alternativas. Quando há conflito com alternativa, devolve `Elicit(...)` com um schema plano (`sala` restrita às alternativas), e o SDK encerra a resposta com `resultType: input_required`, sem canal de volta. No retry, o SDK verifica o `requestState`, roda o resolver de novo e entrega à tool a resposta do usuário.

Evidência no stderr do servidor MCP, durante o validador (trace-id `e76da00f96b5234e406bc51d26daa7e9`, impresso pelo validador). Mostra o `tools/list` antes do primeiro `tools/call`, o mesmo trace-id com span-id novo por request, e o par pausa (id 7) e retry (id 8) com ids diferentes:

```
[mcp] 20:47:30.971 method=tools/list id=2 name=- traceparent=00-e76da00f96b5234e406bc51d26daa7e9-9e5c0f963d8cda4e-01
[mcp] 20:47:30.976 method=resources/read id=3 name=politica://uso traceparent=00-e76da00f96b5234e406bc51d26daa7e9-c60a51ed9785fcee-01
[mcp] 20:47:30.981 method=tools/call id=4 name=reservar_sala traceparent=00-e76da00f96b5234e406bc51d26daa7e9-dbf402c32c122df9-01
[mcp] 20:47:31.084 method=tools/call id=7 name=reservar_sala traceparent=00-e76da00f96b5234e406bc51d26daa7e9-9e6807d5970e174b-01
[mcp] 20:47:31.093 method=tools/call id=8 name=reservar_sala retry traceparent=00-e76da00f96b5234e406bc51d26daa7e9-e9c1dded26f73b71-01
```

Cada linha do log também registra os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` recebidos.

## Decisões técnicas

**Proteção do `requestState`.** Uso o utilitário do próprio SDK, `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=900)` (`server.py`, função `_seguranca_request_state`):
- **Formato:** o token é **AES-GCM**, AEAD, portanto cifrado e autenticado.
- **Vínculos:** o token fica amarrado ao método, à tool e a um digest dos argumentos.
- **Expiração:** carrega `iat`/`exp`.
- **Rejeição:** qualquer adulteração, expiração ou divergência é recusada pelo SDK com `-32602` ("Invalid or expired requestState").
- **Teste de adulteração:** troquei, um a um, cada um dos 387 caracteres de um token real, e todas as variações foram rejeitadas com `-32602`.
- **Argumentos adulterados no retry:** são rejeitados, porque o vínculo com o digest não confere.
- **Chave:** vem só de `REQUEST_STATE_SECRET`, e o servidor se recusa a subir sem ela ou com menos de 32 bytes. Não há `.ephemeral()` nem segredo no código. Como o estado viaja no token e a chave vem do ambiente, **um retry após reiniciar o servidor funciona**. Testei o fluxo de pegar o `requestState`, reiniciar o processo e reenviar, e a reserva foi concluída.

**Validade.** 15 minutos (`VALIDADE_REQUEST_STATE_S = 15 * 60`), dentro da faixa de 5 a 30 minutos pedida.

**Servidor sem sessão.** `streamable_http_app(json_response=True, stateless_http=True)`. Os campos obrigatórios do `_meta` (`-32602` + HTTP 400), os headers divergentes (`-32020`) e a capability de elicitation em form mode ausente (`-32021` + `data.requiredCapabilities` + HTTP 400) são tratados pelo próprio SDK, por request, sem inferir nada de requests anteriores.

**Estado das Tasks.** Em memória no processo do agente (`Tasks._tasks`), com um `asyncio.Lock` por Task. As pausas ficam em `Tasks._pausas[task_id]` e os trace contexts em `Tasks._traces[task_id]`. Como o estado é por Task, duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`. Estados terminais (`COMPLETED`, `CANCELED`, `FAILED`) são definitivos, e um `SendMessage` para eles recebe erro JSON-RPC `-32004`.

**Cliente MCP do agente: SDK oficial, sem resolução automática.** O `Client` do SDK v2 responde sozinho a um `InputRequiredResult` quando recebe um `elicitation_callback`, e isso fecharia o ciclo sem pausar a Task. O agente usa o mesmo `Client` com `allow_input_required=True` em todos os `tools/call`, o que devolve o `input_required` cru. O callback existe só para declarar a capability `{"elicitation": {"form": {}}}` e lança erro se for chamado. O SDK monta o `_meta` obrigatório e os headers espelhados. O agente acrescenta o `traceparent` no `_meta`: o trace-id vem do header `traceparent` da chamada A2A (ou é gerado se ele faltar), com span-id novo por request. A conexão fica aberta e é reaproveitada entre chamadas, aberta e fechada pela mesma task dona.

**Descoberta e política.** Para cada Task, o agente faz `tools/list` (sem lista fixa no código), lê `politica://uso`, extrai a versão da primeira linha e usa esse valor no campo `politica` do artifact.

**Regras de negócio só no servidor.** Conflito (intervalos meio-abertos), janela de 08:00 a 20:00 em `-03:00`, duração máxima e alternativas (capacidade ≥ a pedida, ordem por capacidade e depois id, no máximo 3) ficam em `servidor-mcp/src/salas_mcp/dominio.py`. O agente só traduz protocolo.

**Log.** O middleware ASGI (`logging_mw.py`) registra no stderr cada request JSON-RPC antes de o SDK processá-lo, inclusive os rejeitados. O agente também registra as transições de estado de cada Task no próprio stderr.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: e76da00f96b5234e406bc51d26daa7e9
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
