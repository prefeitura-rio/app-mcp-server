# Decisão: health checks reais e preflight de inicialização

- **Jira**: [CHATR-119](https://iplanrio-pcrj.atlassian.net/browse/CHATR-119) (a parte D6; o restante foi entregue antes do vínculo com um ticket)
- **Status**: Implementado
- **Data**: 2026-08-05 (D1–D5) · 2026-08-12 (D6)
- **Relacionados**: CHATR-110 (tracing OTel → SigNoz), CHATR-114 (cache do cliente BigQuery), [CHATR-113](CHATR-113-sentry-vs-error-interceptor.md) (error interceptor)
- **Escopo**: código, testes e manifests. 7 módulos em `src/health/`, 6 arquivos de teste (68 testes), 9 arquivos modificados.

## Problema

O `/health` retornava `PlainTextResponse("OK")` incondicionalmente — não verificava nada. Ele servia simultaneamente como `livenessProbe` **e** `readinessProbe` em produção e staging. Três consequências:

1. Um pod com credencial GCP malformada, `VALID_TOKENS` vazio ou Redis inacessível entrava em serviço respondendo 200 no probe, e só falhava na primeira chamada de tool.
2. Não havia `startupProbe`. Os imports pesados (langgraph, geopandas, pandas, crawl4ai, aiplatform) rodam no import de `src/app.py`, com apenas ~95s de folga antes do liveness matar o pod.
3. Nenhum endpoint dizia *qual* dependência estava fora.

## Decisões

### D1 — O que impede o boot: só erro de configuração

Falhas **determinísticas** (que retry não conserta) derrubam o processo com `sys.exit(1)`. Falhas de **conectividade** (transitórias) apenas logam e o processo sobe.

Sob Argo Rollouts, um pod que não sobe aborta o rollout e a versão estável continua servindo — o comportamento desejado para erro de configuração. Já derrubar o pod porque o Redis está reiniciando transformaria uma degradação parcial em indisponibilidade total.

| Verificação | Bloqueia boot | Motivo |
|---|---|---|
| 27 variáveis de ambiente obrigatórias | Sim | Determinístico |
| `GCP_SERVICE_ACCOUNT_CREDENTIALS` decodificável | Sim | Determinístico, validação local sem rede |
| `VALID_TOKENS` sem entrada vazia | Sim | Sem token válido, 100% das requisições dão 401 |
| `data/bairros.json`, `data/logradouros.json` | Sim | Ou o arquivo está na imagem ou não está |
| `REDIS_URL` parseável | Sim | O *parse* é determinístico; a *conexão* não |
| Redis respondendo (PING) | **Não** | Transitório |
| BigQuery alcançável | **Não** | Transitório |
| Keycloak JWKS alcançável | **Não** | Transitório; há fallback para token estático |

### D2 — Três endpoints com semânticas separadas

| Rota | Papel | Faz I/O? | Pode dar 503? |
|---|---|---|---|
| `/health` | liveness | não | não |
| `/health/ready` | readiness | não | sim |
| `/health/detail` | diagnóstico | sim (com cache) | **não** |

O risco que motivou a separação: enquanto os dois probes apontavam para o mesmo endpoint, qualquer verificação de dependência acrescentada ali faria o kubelet **matar** o pod quando o Redis caísse. Com `replicas: 1` em produção, isso seria outage total. Separando, o readiness pode evoluir sem esse risco.

### D3 — Dependências de runtime não gateiam tráfego

Redis fora do ar quebra **apenas** a tool `multi_step_service` (workflows IPTU, Poda, Dívida Ativa, Equipamentos), porque produção usa `StateMode.REDIS` sem fallback para JSON (`src/tools/langgraph_workflows.py:12-15`). As demais tools — `google_search`, `equipments_*`, `get_user_memory`, `web_search_surkai` — seguem funcionando.

Retornar 503 no readiness tiraria o pod do balanceador e derrubaria **todas** elas. A falha aparece em `/health/detail`, nos logs e no OTel; não no roteamento.

### D4 — `/health/detail` público, sem dados sensíveis

Só o **nome da classe** da exceção atravessa para a resposta; a exceção completa vai para o log. Mensagens de clientes de rede embutem rotineiramente a URL de conexão — no caso do Redis, com a senha dentro.

A exceção à regra é `HealthCheckError`, cuja mensagem é escrita por nós e portanto segura (ex.: `"ping sem resposta"`, `"jwks respondeu HTTP 503"`).

### D5 — APIs de negócio ficam fora dos checks

Dívida Ativa, IPTU, SGRC, Google Maps, Nominatim, Gemini, Surkai, Dharma, RMI, Typesense e GCS **não** são sondadas. Cada uma afeta um subconjunto de tools, e sondar uma dúzia de APIs a cada probe custa mais do que informa. A observabilidade delas já vem do error interceptor e do OTel.

### D6 — Tabelas externas de Sheets: sonda em background, não inline (CHATR-119)

A exceção a D5. CHATR-119 reportou `400 ... Spreadsheet not found` na tabela externa `rj-iplanrio.plus_codes.equipamentos_instrucoes`, derrubando por completo a tool `equipments_instructions`. A causa raiz — acesso da service account à planilha de origem — foi resolvida no lado do GCP; o que faltava era **detectar** a próxima ocorrência antes do cidadão.

Diferente das APIs de D5, esta dependência entra nos checks por três motivos: é uma única sonda (não uma dúzia), a quebra é silenciosa (ninguém é notificado quando alguém mexe no compartilhamento da planilha) e o modo de falha é reincidente por natureza — a fonte é um documento editável por humanos, fora do controle do deploy.

**Achado que determinou o desenho.** Medido contra o GCP real, com a credencial privada do escopo `auth/drive`:

| Sonda | Sem escopo Drive | Com escopo Drive |
|---|---|---|
| `dry_run` | **OK — falso positivo** | OK, 723ms |
| query real (`SELECT … LIMIT 1`) | `403 Permission denied while getting Drive credentials` | OK, 2,0–4,5s |

O planejamento da query não toca o Drive; só a execução toca. Logo o `check_bigquery` existente, que usa `dry_run` justamente para custar zero, **nunca** pegaria essa classe de erro. É preciso query real — e ela leva de 2 a 4,5s, acima do timeout por check (2s) e do teto global da rodada (3s).

**Decisão**: a sondagem vai para um laço de background (`src/health/external_tables.py`), iniciado no lifespan e cancelado no shutdown, com intervalo padrão de 5 min. O check registrado em `/health/detail` apenas **lê o veredito em memória** — 0,02ms, sem I/O. Isso preserva as quatro garantias do `HealthRegistry` e impede que um endpoint público martele o BigQuery (cada ciclo lê a planilha inteira, ~240 KB).

O alerta ao error interceptor dispara **só na transição disponível → indisponível**; sem isso, uma planilha fora do ar geraria um report a cada 5 min pelo tempo que durasse a falha. O log de `ERROR` por ciclo permanece.

`equipamentos_controle_categorias`, também EXTERNAL sobre a mesma planilha, ficou de fora: nenhuma tool a consulta.

**Degradação graciosa na tool.** O erro de Sheets é um `400`, não um `NotFound` — então atravessa o `except NotFound` de `get_bigquery_result` (que existe para degradar tabelas ausentes) e derrubava a tool inteira. `get_tematic_instructions_for_equipments` passou a devolver o payload de erro estruturado que `get_equipments_instructions` já usa no fallback de tema inválido, instruindo o agente a seguir para `equipments_by_address` — que funciona sem as instruções. O `except NotFound` genérico **não** foi alargado: fazê-lo silenciaria falhas de query em todos os outros call sites.

## Arquitetura

```
boot ─► run_startup_preflight()      src/health/preflight.py
        │  config inválida ──► log com TODOS os erros + sys.exit(1)
        └─ ok
           ▼
        import src.app ─► create_app()
           ├─ register_health_routes(mcp)     3 rotas
           ├─ register_default_checks(mcp)    registry
           └─ lifespan ─► run_all(force=True) snapshot inicial nos logs
                          set_ready(True)
                          run_probe_loop()   task de background (D6)
                            └─ a cada 5 min: query real nas tabelas externas
                               └─ veredito em memória ◄── check_external_tables
```

O `HealthRegistry` (`src/health/registry.py`) dá quatro garantias ao endpoint: nenhum check pode derrubar a resposta (toda exceção vira `DOWN`), pendurá-la (timeout por check de 2s + teto global de 3s), martelar uma dependência (cache TTL de 10s + single-flight por lock) ou vazar credencial (sanitização).

Checks registrados: `gcp_credentials`, `data_files`, `tool_registry` sempre; `redis`, `bigquery`, `keycloak_jwks`, `external_tables` apenas fora do ambiente local.

## Correções de defeitos encontrados no caminho

### C1 — `DATA_DIR` era `str`, usado como `Path`

`workflows/poda_de_arvore/api/api_service.py:247` faz `env.DATA_DIR / "logradouros.json"`. Como `getenv_or_action` devolve `str`, isso levantava `TypeError` — falha garantida naquele caminho. Os testes escondiam o problema porque `test_poda_support.py:79` injeta `DATA_DIR=Path("/tmp")`. Corrigido em `src/config/env.py:194` (`Path(...)`).

### C2 — Token vazio era aceito na autenticação

`"".split(",")` devolve `[""]`, e `"a,,b"` devolve `["a","","b"]`. Sem filtragem, uma string vazia entrava no set de tokens válidos do `HybridTokenVerifier`. Corrigido em `src/app.py:80` (filtro) e barrado no preflight.

### C3 — `src/__init__.py` importava a aplicação no import do pacote

`from src.app import app, mcp, create_app` fazia com que qualquer `import src.<algo>` construísse a aplicação inteira e importasse `src.config.env`. Como `python -m src.main` importa o pacote `src` **antes** de executar `main.py`, `env.py` abortava na primeira variável faltante — anulando a agregação do preflight.

Os símbolos passaram a ser resolvidos sob demanda (PEP 562, `__getattr__`), preservando a superfície pública. Nenhum consumidor no repositório usava esses nomes.

Efeito colateral: três testes em `test_wrappers_and_infisical.py` passavam por acidente, apoiados nesse import ansioso ter deixado o `http_client` real em `sys.modules`. O stub de `error_interceptor` deles não expunha `send_api_error`. Completado.

## Limitação conhecida: drain no SIGTERM não funciona

O `finally` do lifespan chama `set_ready(False)`, o que **não** acontece em SIGTERM. A uvicorn 0.38 restaura o handler original do sinal e faz `signal.raise_signal()` ao sair de `serve()` (`Server.capture_signals`); o processo morre pelo handler padrão do Python antes de o lifespan do FastMCP desenrolar. Comprovado com marcador em arquivo: apenas `set_ready(True)` é registrado.

O código foi mantido porque está correto no encerramento programático (verificado dirigindo `app.router.lifespan_context` direto: `False → True → False`). Quem tira o pod do balanceador no encerramento é o próprio Kubernetes, ao marcá-lo como `Terminating`.

Se o race entre a remoção do endpoint e a morte do processo vier a incomodar, o remédio usual é um `preStop: sleep 5` no container — **não implementado**, por estar fora do escopo.

## Manifests (`k8s/prod`, `k8s/staging`)

```yaml
startupProbe:      # NOVO — até 150s de boot
  httpGet: {path: /health, port: 80}
  periodSeconds: 5
  failureThreshold: 30
livenessProbe:     # initialDelaySeconds removido (startupProbe cobre)
  httpGet: {path: /health, port: 80}
  periodSeconds: 30
readinessProbe:    # MUDOU: /health → /health/ready (tempos inalterados)
  httpGet: {path: /health/ready, port: 80}
  periodSeconds: 30
```

> **Ordem de deploy**: o código precisa ir a produção **antes** dos manifests. Apontar o readiness para `/health/ready` antes de a rota existir causa `CrashLoopBackOff` imediato.

## Verificação executada

| O que | Resultado |
|---|---|
| Suíte completa | 195 testes passando (70 novos) |
| Preflight, 3 variáveis quebradas | exit 1, os 3 erros listados juntos |
| Preflight, ambiente vazio | exit 1, **as 27 variáveis de uma vez**, app nem construída |
| Servidor real com Redis fora | `/health` 200, `/health/ready` 200, `/health/detail` `degraded` com `redis: down` |
| Vazamento no payload | ausentes: `redis://`, porta, host, senha, `@`, e-mail da service account |
| Latência do `/health/detail` | 0,7ms com cache (probe timeout: 3s) |
| Lifespan sob uvicorn | snapshot inicial das 6 dependências nos logs |
| BigQuery `dry_run` | `up` em 1308ms contra o GCP real, custo e quota zero |
| Cache TTL | confirmado: re-sondagem só após 10s |
| **D6** — sonda real da tabela externa | `up` contra o GCP real (4478ms — daí não caber inline) |
| **D6** — check lendo o veredito | `up` em **0,024ms**, sem I/O |
| **D6** — caminho de falha (escopo Drive removido) | veredito `Forbidden`; `/health/detail` acusa `down` |
| **D6** — vazamento no payload | ausentes: ID da planilha, projeto e dataset; só `equipamentos_instrucoes` |
| **D6** — shutdown | task da sonda cancelada e aguardada: 0 tasks vivas após o lifespan |

Comandos para reproduzir:

```bash
uv run pytest src/tests/unit/health/ -v

# preflight deve abortar listando tudo
GCP_SERVICE_ACCOUNT_CREDENTIALS="x!!" DATA_DIR="/nao/existe" \
  VALID_TOKENS="a,,b" uv run python -m src.main   # espera exit 1

# degradação: subir com REDIS_URL numa porta morta e conferir que
# /health e /health/ready seguem 200 enquanto /health/detail acusa
curl -s localhost/health && curl -s localhost/health/ready
curl -s localhost/health/detail | jq

# D6: a sonda de tabelas externas, com intervalo curto para não esperar 5 min
EXTERNAL_TABLES_PROBE_INTERVAL_S=15 uv run python -m src.main
curl -s localhost/health/detail | jq '.checks[] | select(.name=="external_tables")'
```

## Variáveis de ambiente introduzidas

Todas opcionais, com default, lidas via `getenv_or_action` (não constam em `env.py` para manter `registry.py` importável isoladamente nos testes):

| Variável | Default | Efeito |
|---|---|---|
| `HEALTH_CHECK_TIMEOUT_S` | `2.0` | Timeout por check |
| `HEALTH_GLOBAL_TIMEOUT_S` | `3.0` | Teto da rodada inteira |
| `HEALTH_CACHE_TTL_S` | `10.0` | Janela de reaproveitamento do resultado |
| `EXTERNAL_TABLES_PROBE_INTERVAL_S` | `300.0` | Intervalo da sonda de tabelas externas (piso de 10s) |

## Apêndice: arquivos

**Novos** (`src/health/`):

| Arquivo | Papel |
|---|---|
| `preflight.py` | Validações de configuração, `REQUIRED_ENV_VARS`, `run_startup_preflight()` |
| `registry.py` | `HealthRegistry`, `sanitize_error()`, timeouts e cache |
| `checks.py` | Os 7 checks e `register_default_checks()` |
| `routes.py` | Os 3 handlers e `register_health_routes()` |
| `models.py` | `CheckStatus`, `CheckResult`, `HealthCheckError`, `aggregate_status()` |
| `state.py` | `set_ready()`, `is_ready()`, `uptime_seconds()` |
| `external_tables.py` | **D6** — sonda de background, veredito em memória, alerta na transição |

**Testes novos** (`src/tests/unit/health/`, 68 testes): `test_preflight.py` (16), `test_registry.py` (12), `test_checks.py` (11), `test_routes.py` (9), `test_required_env_sync.py` (2), `test_external_tables.py` (18).

`test_required_env_sync.py` merece nota: faz parse de `src/config/env.py` via `ast` e compara com `REQUIRED_ENV_VARS`, apontando exatamente qual variável saiu de sincronia. A duplicação da lista é necessária — o preflight não pode importar `env.py`, já que o objetivo é reportar todas as faltantes antes que ele aborte na primeira.

**Modificados**:

| Arquivo | Mudança |
|---|---|
| `src/main.py` | Preflight antes do import de `src.app` (ordem travada por teste) |
| `src/app.py` | `register_health_routes` + `register_default_checks` + lifespan; filtro de token vazio; start/cancel da sonda de D6 |
| `src/tools/equipments/pluscode_service.py` | **D6** — degradação graciosa de `get_tematic_instructions_for_equipments` |
| `src/tests/unit/tools/test_equipments_and_cor_alert.py` | **D6** — 2 testes da degradação |
| `src/__init__.py` | Imports preguiçosos via PEP 562 (C3) |
| `src/config/env.py` | `DATA_DIR` como `Path` (C1) |
| `k8s/prod/resources.yaml`, `k8s/staging/resources.yaml` | `startupProbe`; readiness → `/health/ready` |
| `src/tests/unit/app/test_main.py` | Stub do preflight + teste da ordem preflight/import |
| `src/tests/unit/utils/test_wrappers_and_infisical.py` | Stub de `error_interceptor` completado (C3) |
