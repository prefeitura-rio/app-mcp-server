# Smoke da imagem de produção, executado contra o container real.
#
# Por que esta suíte existe: o `pr-quality-gate` constrói a imagem, escaneia
# com Trivy e a descarta — sem nunca executá-la. Os testes unitários montam o
# app ASGI em processo (`test_app_factory.py`) e leem as rotas, mas não fazem
# request algum. O resultado é que quebrar o preflight, o `CMD`, uma env var
# obrigatória, o middleware de auth do `/mcp` ou o `stateless_http` passa o
# gate inteiro verde e só aparece em staging, depois do merge.
#
# O escopo aqui é deliberadamente "o artefato sobe e fala HTTP", não regra de
# negócio — essa continua no pytest, que é mais barato e conta para o gate de
# cobertura. Nada aqui depende de dependência externa alcançável.
#
# Variáveis: base_url, token (ver src/tests/hurl/README.md).


# ===== LIVENESS =====
# Trivial por design: nenhuma dependência é consultada. Se esta rota fizer I/O
# algum dia, o kubelet passa a matar o pod quando o Redis cair.

GET {{base_url}}/health
HTTP 200
[Asserts]
header "Content-Type" contains "text/plain"
body == "OK"


# ===== READINESS =====

GET {{base_url}}/health/ready
HTTP 200
[Asserts]
jsonpath "$.status" == "ready"


# ===== DIAGNÓSTICO DAS DEPENDÊNCIAS =====
# O invariante central do desenho de health (src/health/routes.py): uma
# dependência fora do ar aparece aqui, mas NÃO derruba /health nem
# /health/ready. As duas requisições acima já passaram com o Redis e o
# BigQuery inalcançáveis — é isso que este bloco confirma do outro lado.
#
# `degraded` é o estado esperado no CI, não uma falha do teste: o container
# roda com credenciais falsas e sem Redis, então esses checks reprovam de
# propósito. Se este assert quebrar com `ok`, significa que os checks
# pararam de detectar dependência fora do ar.
#
# Passou a exigir token: diferente de /health e /health/ready, esta rota não é
# probe do kubelet — é diagnóstico, e desenhava o mapa das dependências para
# qualquer um que alcançasse a porta.

GET {{base_url}}/health/detail
Authorization: Bearer {{token}}
HTTP 200
[Asserts]
jsonpath "$.status" == "degraded"
jsonpath "$.ready" == true
jsonpath "$.version" exists
jsonpath "$.checks" count >= 5
# `data_files` e `gcp_credentials` up provam coisas que só o container prova:
# que bairros.json/logradouros.json entraram na imagem sob o DATA_DIR certo,
# e que o preflight aceitou a credencial — ele aborta o boot se não aceitar.
jsonpath "$.checks[?(@.name=='data_files')].status" == "up"
jsonpath "$.checks[?(@.name=='gcp_credentials')].status" == "up"
jsonpath "$.checks[?(@.name=='tool_registry')].status" == "up"
jsonpath "$.checks[?(@.name=='redis')].critical" == true
# Contrato de `registry.sanitize_error`: o corpo não pode devolver URL, host
# nem credencial. Mensagens de cliente de rede embutem a URL de conexão — no
# caso do Redis, com a senha dentro. Continua valendo mesmo com a rota agora
# autenticada: quem tem token de consumidor não precisa da senha do Redis.
body not matches "[a-z][a-z0-9+.-]*://"


# ===== FRONTEIRA DE AUTENTICAÇÃO =====
# Os testes unitários exercitam o `HybridTokenVerifier` isolado. O que só se
# vê pela porta HTTP é se ele está de fato plugado no app — `auth=` do FastMCP
# protege apenas o `/mcp`, e é fácil essa ligação se perder num refactor.

# Fora do `/mcp`, quem protege é o `RequireAuthOnAllRoutes`
# (src/middleware/require_auth.py), montado por `build_http_middleware()` em
# `src/main.py`. Uma rota qualquer que não esteja na allowlist tem que recusar
# — inclusive uma que não existe, porque o middleware decide antes do
# roteamento. É este request que prova, pela rede, que o middleware está
# plugado: sem ele a resposta seria 404, e foi exatamente assim que as cinco
# rotas de Dívida Ativa passaram a atender sem credencial.
GET {{base_url}}/rota-que-nao-existe
HTTP 401
[Asserts]
header "WWW-Authenticate" contains "invalid_token"


# A mesma rota de diagnóstico que passou acima com token, agora sem ele.
GET {{base_url}}/health/detail
HTTP 401


POST {{base_url}}/mcp
Content-Type: application/json
Accept: application/json, text/event-stream
```
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hurl-smoke","version":"1"}}}
```
HTTP 401
[Asserts]
header "WWW-Authenticate" contains "invalid_token"


POST {{base_url}}/mcp
Authorization: Bearer token-que-nao-existe
Content-Type: application/json
Accept: application/json, text/event-stream
```
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hurl-smoke","version":"1"}}}
```
HTTP 401


# ===== HANDSHAKE MCP SOBRE STREAMABLE HTTP =====
# O produto é um servidor MCP; nada no CI exercitava o protocolo em si.
# As três requisições abaixo são independentes de propósito: em modo
# stateless (MCP_STATELESS_HTTP=true, padrão fora de local) não há sessão a
# carregar entre elas. Se o flag regredir para stateful, `tools/list` passa a
# exigir `Mcp-Session-Id` e este bloco falha — que é o ponto.
#
# A resposta vem como `text/event-stream`, então o corpo é
# `event: message\ndata: {...}`. A cadeia `regex ... jsonpath ...` extrai o
# JSON de dentro do envelope SSE.

POST {{base_url}}/mcp
Authorization: Bearer {{token}}
Content-Type: application/json
Accept: application/json, text/event-stream
```
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hurl-smoke","version":"1"}}}
```
HTTP 200
[Asserts]
header "Content-Type" contains "text/event-stream"
body regex "data: (.*)" jsonpath "$.result.serverInfo.name" == "Rio de Janeiro MCP Server"
body regex "data: (.*)" jsonpath "$.result.protocolVersion" exists
body regex "data: (.*)" jsonpath "$.result.capabilities.tools" exists


POST {{base_url}}/mcp
Authorization: Bearer {{token}}
Content-Type: application/json
Accept: application/json, text/event-stream
```
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```
HTTP 200
[Asserts]
# `test_app_factory.py` trava o catálogo no nível do Python. Aqui o que se
# trava é que um cliente real enxerga as tools pela rede.
body regex "data: (.*)" jsonpath "$.result.tools" count >= 10
body regex "data: (.*)" jsonpath "$.result.tools[*].name" contains "calculator_add"
body regex "data: (.*)" jsonpath "$.result.tools[*].name" contains "multi_step_service"
body regex "data: (.*)" jsonpath "$.result.tools[*].name" contains "equipments_by_address"


# Uma tool pura de ponta a ponta: sem rede, sem credencial, sem I/O. Falhar
# aqui significa que o pipeline de execução de tools quebrou, não que uma
# dependência caiu.
POST {{base_url}}/mcp
Authorization: Bearer {{token}}
Content-Type: application/json
Accept: application/json, text/event-stream
```
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"calculator_add","arguments":{"a":2,"b":3}}}
```
HTTP 200
[Asserts]
body regex "data: (.*)" jsonpath "$.result.isError" == false
body regex "data: (.*)" jsonpath "$.result.structuredContent.result" == 5.0
