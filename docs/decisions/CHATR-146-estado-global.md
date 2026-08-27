# Decisão: eliminar estado global e introduzir contexto de aplicação e de request

- **Jira**: [CHATR-146](https://iplanrio-pcrj.atlassian.net/browse/CHATR-146)
- **Status**: Decidido, implementação pendente
- **Data**: 2026-08-21
- **Sub-tasks**: CHATR-147, CHATR-148, CHATR-149, CHATR-150, CHATR-151, CHATR-152, CHATR-153
- **Relacionados**: [CHATR-105](CHATR-105-debitos-tecnicos.md) (débitos técnicos — registrou `mcp = create_app()` no import como follow-up), [CHATR-119](health-checks-e-preflight.md) (health checks e preflight — origem de `REQUIRED_ENV_VARS`), CHATR-125 (single-flight do cache BigQuery — origem de `_inflight_locks`)
- **Escopo**: levantamento de `src/` inteiro (exceto testes) e registro das sete direções de correção.

> **Revisão de 2026-08-26.** Ao planejar a execução, cinco pontos deste ADR não sobreviveram ao contato com o código — ver [Correções](#correções-2026-08-26) no fim do documento. As decisões D1, D2, D3 e D7 tiveram o escopo alterado.

## Problema

Uma varredura de `src/` atrás de cinco padrões — palavra-chave `global`, atribuições mutáveis em nível de módulo, singletons instanciados no import, caches de processo (`lru_cache`) e configuração global — encontrou **~35 pontos de estado global**.

O dado que organiza o diagnóstico não é a contagem, é o que está ausente: **o repositório não usa `contextvars` em lugar nenhum** (zero ocorrências). Não existe nenhuma noção de contexto de aplicação nem de request/tool call. Sem um lugar onde pendurar "o que vale para este processo" e "o que vale para esta chamada", cada recurso compartilhado acaba virando uma variável de módulo por falta de alternativa — e é isso que explica a distribuição dos achados.

O custo não é hipotético. Já existe, em código versionado, uma camada inteira de contorno: três funções `reset_*()` de produção que só servem aos testes, ~30 `monkeypatch` de atributo privado de módulo, dois `__getattr__` PEP 562 para desarmar ciclos de import, e um teste que faz parse AST de `src/config/env.py` para policiar uma lista duplicada à mão.

## Levantamento

### Categoria A — estado mutável de módulo com `global` (15)

| Variável | Local | O que faz |
|---|---|---|
| `_ready`, `_started_at` | [state.py:21-22](../../src/health/state.py#L21-L22) | Flag de readiness do processo, lida por `/health/ready` |
| `_redis_backend` | [checks.py:26](../../src/health/checks.py#L26) | Lazy singleton do `RedisBackend` do health check |
| `_last_result`, `_last_probe_at` | [external_tables.py:72-73](../../src/health/external_tables.py#L72-L73) | Cache do veredito da última sondagem das tabelas externas |
| `_gcp_credentials_verdict` | [preflight.py:78](../../src/health/preflight.py#L78) | Memoiza o parse da chave RSA (~40 ms de CPU) |
| `_tracing_enabled`, `_setup_attempted` | [tracing.py:35-36](../../src/observability/tracing.py#L35-L36) | Idempotência do `setup_tracing()` e sinalização para o middleware |
| `_token_manager` | [rmi_oauth2.py:98](../../src/utils/rmi_oauth2.py#L98) | Singleton do `OAuth2TokenManager` (token + expiry + lock) |
| `_env_cache` | [infisical.py:8](../../src/utils/infisical.py#L8) | Cache do `.env` parseado |
| `_batch_buffer` + lock | [bigquery.py:74-75](../../src/utils/bigquery.py#L74-L75) | Buffer de linhas pendentes de insert em lote |
| `_flush_thread`, `_flush_stop_event` | [bigquery.py:84-85](../../src/utils/bigquery.py#L84-L85) | Thread daemon que drena o buffer a cada 30 s |
| `_sync_redis_client` + lock | [bigquery.py:120-121](../../src/utils/bigquery.py#L120-L121) | Cliente Redis síncrono da DLQ |
| `_async_redis_client` | [bigquery.py:690](../../src/utils/bigquery.py#L690) | Cliente Redis async do cache de queries |
| `_inflight_locks`, `_inflight_refs` | [bigquery.py:845-846](../../src/utils/bigquery.py#L845-L846) | Single-flight por chave de cache |
| `_read_executor` + lock | [bigquery.py:1013-1014](../../src/utils/bigquery.py#L1013-L1014) | `ThreadPoolExecutor` das leituras do BigQuery |
| `_tools_description_cache` | [core/__init__.py:71](../../src/tools/multi_step_service/core/__init__.py#L71) | Descrição das tools, resolvida sob demanda via PEP 562 |
| `_pending_interceptor_tasks` | [error_interceptor.py:447](../../src/utils/error_interceptor.py#L447) | Referências fortes às tasks fire-and-forget, contra coleta pelo GC |

### Categoria B — singletons instanciados no import (5)

| Símbolo | Local | Observação |
|---|---|---|
| `mcp = create_app()` / `app = mcp` | [app.py:675-678](../../src/app.py#L675-L678) | Constrói a aplicação inteira no import: ~20 tools, auth, `setup_tracing()`, `set_ready(True)` |
| `health_registry = HealthRegistry()` | [registry.py:205](../../src/health/registry.py#L205) | Populado por `register_default_checks()` |
| `gemini_service = GeminiService()` | [gemini_service.py:476](../../src/tools/google_search/gemini_service.py#L476) | Instancia `genai.Client` no import, lendo `env.GEMINI_API_KEY` |
| `get_bigquery_client()` `@lru_cache(maxsize=1)` | [bigquery.py:26](../../src/utils/bigquery.py#L26) | Singleton via cache em vez de `global` — o padrão que os demais deveriam seguir |
| `workflows = [...]` | [workflows/__init__.py:23](../../src/tools/multi_step_service/workflows/__init__.py#L23) | Registry hardcoded, importado de dentro do `Orchestrator` para quebrar ciclo |

`Orchestrator()` é instanciado **4 vezes** ([tool.py:22,39,53](../../src/tools/multi_step_service/tool.py#L22) e [core/__init__.py:56](../../src/tools/multi_step_service/core/__init__.py#L56)), reconstruindo o dict de workflows a cada chamada de tool.

### Categoria C — configuração global

[`src/config/env.py`](../../src/config/env.py) tem **81 constantes** lidas no import, consumidas por **30 módulos**. [`src/config/settings.py`](../../src/config/settings.py) acrescenta `Settings` (class attributes lendo `os.getenv` no import), `DISTRICTS_DATA` e `FEATURES_CONFIG`.

### Categoria D — constantes de módulo mutáveis (~13)

Regexes compilados, `frozenset`s, tuplas e sentinelas (`_CPF_PATTERN`, `RETRYABLE_CLIENT_CODES`, `_ESCAPES_DA_CHAVE`, `_CACHE_MISS`/`_CACHE_UNAVAILABLE`) são imutáveis e não entram na conta. O que resta é um grupo declarado como `list`/`dict`/`set`:

| Símbolo | Local |
|---|---|
| `ALLOWED_NEIGHBORHOODS_PONTOS_APOIO` | [equipments_tools.py:13](../../src/tools/equipments_tools.py#L13) **e** [equipments_workflow.py:19](../../src/tools/multi_step_service/workflows/equipments/equipments_workflow.py#L19) |
| `VALID_ALERT_TYPES`, `VALID_SEVERITIES`, `NEIGHBORHOOD_ALIASES` | [cor_alert_tools.py:21-24](../../src/tools/cor_alert_tools.py#L21-L24) |
| `DEFAULT_ERROR_STATUS_CODES`, `SENSITIVE_KEYS` | [http_client.py:38-41](../../src/utils/http_client.py#L38-L41) |
| `DISTRICTS_DATA`, `FEATURES_CONFIG` | [settings.py:38,66](../../src/config/settings.py#L38) |
| `_OPTION_REGISTRY` | [divida_ativa/core/models.py:19](../../src/tools/multi_step_service/workflows/divida_ativa/core/models.py#L19) |
| ~~`PAIR_RESOLUTIONS_`~~ | ~~[openlocationcode.py:104](../../src/tools/equipments/openlocationcode.py#L104)~~ — ver C5 |

## Evidência do custo já pago

Três sintomas já versionados, todos consequência direta do padrão:

1. **Três `reset_*()` de produção que só servem aos testes** — `checks.reset_redis_backend()`, `external_tables.reset_state()`, `preflight.reset_gcp_credentials_cache()`.
2. **Testes manipulando privados de módulo** — `module._async_redis_client = None`, `module._sync_redis_client = None`, `get_bigquery_client.cache_clear()`, e ~30 `monkeypatch.setattr(module, "get_bigquery_client", ...)` distribuídos por 10 arquivos.
3. **Dois `__getattr__` PEP 562** ([src/__init__.py:25](../../src/__init__.py#L25), [core/__init__.py:74](../../src/tools/multi_step_service/core/__init__.py#L74)) escritos para contornar ciclos de import e inicialização ansiosa, cada um com docstring longa explicando o ciclo que desarma.

## Decisões

Sete direções, ordenadas por relação custo/benefício e não por gravidade teórica.

### D1 — `AppContext` com escopo de lifespan (CHATR-147)

**Cobre 8 dos 15 globais da categoria A**: `_redis_backend`, `_token_manager`, `_batch_buffer`, `_flush_thread`, `_sync_redis_client`, `_async_redis_client`, `_inflight_locks`/`_inflight_refs`, `_read_executor`.

Esses oito são todos a mesma coisa: recurso caro que precisa nascer uma vez e morrer no shutdown. Variável de módulo é a forma errada de expressar isso porque atrela o ciclo de vida do recurso ao ciclo de vida do `import`, que ninguém controla — nem para ordenar a criação, nem para garantir a destruição.

```python
@dataclass
class AppContext:
    bq: bigquery.Client
    redis_async: Redis | None
    redis_sync: Redis | None
    read_executor: ThreadPoolExecutor
    token_manager: OAuth2TokenManager
```

**Ganho lateral:** `_shutdown_read_executor()` já existe, mas depende de alguém lembrar de chamá-lo. Num lifespan passa a ser o caminho normal de saída.

> **Corrigido (C1).** A redação original incluía `_stop_batch_flush_thread()` nesta frase e os globais do caminho de escrita na tabela acima. Está errado: o teardown do lifespan não roda em SIGTERM, e o caminho de escrita já tem encerramento determinístico por outro mecanismo. `_batch_buffer`, `_flush_thread`, `_flush_stop_event`, `_write_executor` e os signal handlers **saem do escopo do D1**.

### D2 — `ContextVar` para escopo de request (CHATR-148)

É o "contexto" que falta. Hoje `user_id` aparece em **22 assinaturas de função** e em 210 linhas, passado à mão camada por camada; e [tracing.py:125](../../src/observability/tracing.py#L125) tem um `_extract_user_id()` que faz *best-effort* fuçando os argumentos da tool porque não tem de onde ler. Um `ContextVar` setado no `ToolCallTracingMiddleware` / `CheckTokenMiddleware` cobre `user_id`, `request_id` e correlação de traces, e apaga os dois problemas.

**Ressalva que precisa ser tratada explicitamente.** `ContextVar` propaga por `await`, mas **não atravessa `loop.run_in_executor()`** sem `contextvars.copy_context()`. Sem tratar essas fronteiras, o contexto some justamente no caminho onde seria mais útil: correlacionar uma query lenta com o usuário que a disparou.

> **Corrigido (C3, C4).** Metade da ressalva já está resolvida: `run_in_executor_with_context()` ([tracing.py:160](../../src/observability/tracing.py#L160)) já faz o `copy_context()`, e as leituras do BigQuery já passam por ele. Falta rotear os sites que ainda usam `loop.run_in_executor` cru — `checks.py:82`, `external_tables.py:124`, `pluscode_service.py:45`, `llms.py:51` e quatro pontos de `bigquery.py`.
>
> Em compensação, dois outros pontos do D2 estavam errados: **não existe middleware sempre-ligado** onde pendurar o `ContextVar` (o de tracing é condicional; `CheckTokenMiddleware` é código morto), e **`user_id` não sai das assinaturas das tools** — é argumento de entrada do cliente MCP, não da autenticação.

### D3 — `Settings` validado no lugar das 81 constantes de env (CHATR-149) — **superseded**

> **Corrigido (C6).** Esta decisão foi re-planejada com muito mais profundidade em [CHATR-154](CHATR-154-config-pydantic-settings.md), que tem ADR próprio, 8 sub-tasks e um destino registrado para cada uma das 89 variáveis do serviço. **CHATR-149 é fechada como superseded** e o épico passa a ter seis sub-tasks. O texto abaixo fica como registro do diagnóstico que originou o CHATR-154.

Mudança de maior impacto e de maior raio. Não é cosmética — resolve três coisas de uma vez:

| Problema atual | Como o `BaseSettings` resolve |
|---|---|
| `env.py` aborta na **primeira** variável faltante, o que obrigou a duplicar 27 nomes em `preflight.REQUIRED_ENV_VARS` | Pydantic reporta todas de uma vez — exatamente o que o preflight foi escrito à mão para fazer |
| `test_required_env_sync.py` faz parse AST de `env.py` só para policiar a duplicação | A duplicação deixa de existir, e o teste junto |
| Coerção espalhada: `int(...)`, `float(...)`, `getenv_bool`, `Path(...)` | Validação de tipo na borda, uma vez |

**Cuidado de ordem de boot:** [`src/main.py`](../../src/main.py) hoje roda `run_startup_preflight()` **antes** de importar `src.app`, justamente porque `env.py` aborta na primeira faltante. Essa ordenação precisa ser repensada junto, não depois.

### D4 — módulos-com-estado viram instâncias (CHATR-150)

Para [state.py](../../src/health/state.py), [external_tables.py](../../src/health/external_tables.py) e [tracing.py](../../src/observability/tracing.py), o estado é pequeno mas a forma é a que mais atrapalha o teste. Viram `ReadinessState`, `ExternalTablesProbe` e um objeto `Tracing` com `.enabled`, guardados no `AppContext` ou no `health_registry`.

**Bug embutido a corrigir de passagem:** `_started_at = time.monotonic()` em [state.py:22](../../src/health/state.py#L22) é avaliado no import, então `uptime_seconds()` mede o tempo desde o *import do módulo*, não desde o start do servidor. A diferença é pequena hoje, mas os imports pesados deste projeto (langgraph, geopandas, pandas, crawl4ai) não são instantâneos — e CHATR-119 já registrou que eles são o motivo do `startupProbe`.

### D5 — health checks por closure (CHATR-151)

`HealthRegistry.register()` já aceita um callable. Registrar closures que capturam o contexto elimina `_redis_backend` e `reset_redis_backend()` sem tocar na arquitetura do registry:

```python
health_registry.register("redis", lambda: check_redis(ctx.redis_backend))
```

### D6 — `@lru_cache` no lugar de `global` para memoização pura (CHATR-152)

`_gcp_credentials_verdict`, `_env_cache` e `_tools_description_cache` são memoização pura: mesma entrada, mesma saída. `@lru_cache` expressa isso melhor, é thread-safe e já traz `.cache_clear()` para os testes. O padrão já existe no repositório em `get_bigquery_client()`; é uniformizar.

### D7 — congelar constantes e desduplicar (CHATR-153)

`tuple`/`frozenset`/`MappingProxyType` para o que é constante. O item que também corrige risco real de bug é `ALLOWED_NEIGHBORHOODS_PONTOS_APOIO`: hoje é o mesmo literal em dois arquivos, e a divergência (quando alguém editar só um) seria silenciosa — a tool e o workflow passariam a aceitar listas de bairros diferentes.

**Entregue em 2026-08-26.** `ALLOWED_NEIGHBORHOODS_PONTOS_APOIO` passou a ter origem única em [equipments_tools.py:17](../../src/tools/equipments_tools.py#L17) (`frozenset`), importada pelo workflow — que já importava três outros símbolos do mesmo módulo, então não houve aresta de import nova. `VALID_ALERT_TYPES`/`VALID_SEVERITIES` viraram `tuple` e não `frozenset` porque a ordem chega ao cidadão via `", ".join(...)` na mensagem de erro. `NEIGHBORHOOD_ALIASES` e `_OPTION_REGISTRY` viraram `MappingProxyType` (congelamento raso: os dicts internos do registry seguem mutáveis). `DEFAULT_ERROR_STATUS_CODES` e `SENSITIVE_KEYS` viraram `frozenset`, com as anotações dos parâmetros que os recebem relaxadas para `AbstractSet`.

Dois desvios do escopo original, ambos deliberados:

- **`PAIR_RESOLUTIONS_` ficou de fora** — ver C5.
- **`DISTRICTS_DATA` e `FEATURES_CONFIG` ficaram de fora.** A fase 7 do CHATR-154 (CHATR-161) dissolve `settings.py` inteiro; congelá-los agora só geraria conflito. `FEATURES_CONFIG`, aliás, é uma das decisões em aberto daquele ADR (tem uso real ou é resíduo do template?).

Correção de passagem, na mesma linha do problema que o D7 ataca: `_SENSITIVE_QUERY_RE` era compilado a partir de `'|'.join(SENSITIVE_KEYS)` sobre um `set`, cuja ordem de iteração de strings varia entre processos (hash randomization) — o padrão compilado mudava a cada boot. O resultado da redação é o mesmo, mas o `sorted()` torna o comportamento reproduzível entre um pod e outro.

## O que fica como está

Decisão explícita, para não ser reaberta a cada leitura do código:

**`_pending_interceptor_tasks`** ([error_interceptor.py:447](../../src/utils/error_interceptor.py#L447)) permanece. Um `set` de módulo guardando referência forte a tasks fire-and-forget é o idiom recomendado para o problema conhecido do asyncio — a event loop só guarda referência fraca, e sem isso o GC pode coletar a task no meio da execução. Movê-lo para o `AppContext` é aceitável se o resto for junto, mas não é dívida por si só.

**`mcp = create_app()`** ([app.py:675](../../src/app.py#L675)) permanece como símbolo global: `uvicorn`/`fastmcp` precisam de algo importável. CHATR-105 já havia registrado isso como follow-up. O que dá para melhorar não é o símbolo, são os efeitos colaterais dentro do factory — `setup_tracing()` e `set_ready(True)` acontecendo em tempo de import é o que obriga `test_main.py` a stubar o módulo inteiro.

## Ordem de execução

| # | Card | Entrega | Depende de |
|---|---|---|---|
| 1 | CHATR-153 (D7) | Constantes congeladas e desduplicadas | — |
| 2 | CHATR-152 (D6) | `@lru_cache` nas memoizações puras | — |
| 3 | CHATR-147 (D1) | `AppContext` + holder + lifespan | — |
| 4 | CHATR-151 (D5) | Health checks por closure | 3 |
| 5 | CHATR-150 (D4) | Módulos-com-estado viram instâncias | 3 |
| 6 | CHATR-148 (D2) | `ContextVar` de request | 3 |
| — | CHATR-149 (D3) | Superseded → [CHATR-154](CHATR-154-config-pydantic-settings.md) | — |

Cada PR deixa a suíte verde por si só. 1 e 2 podem sair em paralelo; 4, 5 e 6 também, depois do 3.

---

## Como o `AppContext` chega aos call sites

Decisão que este ADR tinha deixado em aberto, fechada em 2026-08-26.

O contexto é alcançado por um **holder único** (`src/context.py`) preenchido pelo lifespan. Os acessores que já existem — `_get_read_executor()`, `_get_redis_backend()`, … — mantêm nome e assinatura e passam a ler dele; muda só o corpo.

O holder é uma variável de módulo **com dono**, não um `ContextVar`: o escopo é o processo inteiro, e `ContextVar.set()` dentro de um `@asynccontextmanager` tem propagação sutil demais para o ganho. A troca é oito globais sem ciclo de vida por **um** com ciclo de vida explícito, criado num ponto e destruído noutro — e com ponto de substituição único nos testes.

`get_app_context()` constrói um contexto de processo se ninguém setou. Não é preguiça: `mcp.run()` local, testes e uso embarcado não entram no lifespan, e é o mesmo motivo do `set_ready(True)` de [app.py:698](../../src/app.py#L698).

O caminho canônico do FastMCP (`ctx.request_context.lifespan_context`) fica **aberto** — o lifespan devolve `{"app": ctx}` — mas não é o mecanismo de acesso: exigiria mudar ~20 assinaturas de tool e propagar contexto por todas as 3.075 linhas de `bigquery.py`.

---

## Correções (2026-08-26)

Cinco pontos do levantamento original não sobreviveram ao contato com o código na hora de planejar a execução.

### C1 — mover o flush do BigQuery para o lifespan seria uma regressão

O D1 justificava-se dizendo que `_stop_batch_flush_thread()` "passa a ser o caminho normal de saída" num lifespan. Não passa: [app.py:203-215](../../src/app.py#L203-L215) documenta que **o teardown do lifespan NÃO roda em SIGTERM** — a uvicorn restaura o handler original e faz `signal.raise_signal()` ao sair de `serve()`, matando o processo antes do desenrolar. Por isso `bigquery.py` instala o **próprio** handler ([bigquery.py:401-425](../../src/utils/bigquery.py#L401-L425)), deliberadamente, para ser o "handler anterior" que a uvicorn re-levanta.

Todo o caminho de escrita do BigQuery sai do escopo do D1. Já tem encerramento determinístico, por um caminho que o lifespan não alcança.

### C2 — o inventário da categoria A está incompleto e com linhas defasadas

`bigquery.py` cresceu desde 2026-08-21 e **todas** as linhas citadas para esse arquivo estão erradas (`_read_executor` está em 1675, não 1013; `_sync_redis_client` em 428, não 120; `_async_redis_client` em 1352, não 690; `_inflight_locks` em 1507, não 845). Faltam ainda no inventário:

| Variável | Local |
|---|---|
| `_write_executor` + lock | [bigquery.py:179-180](../../src/utils/bigquery.py#L179-L180) |
| `_previous_signal_handlers`, `_signal_handlers_installed` | [bigquery.py:369-370](../../src/utils/bigquery.py#L369-L370) |
| `_write_metrics` + `_metrics_lock` | [bigquery.py:97-98](../../src/utils/bigquery.py#L97-L98) |

O último traz uma **quarta** função `reset_*()` de produção que só serve aos testes — `reset_bigquery_write_metrics()` ([bigquery.py:157](../../src/utils/bigquery.py#L157)) — que o "Evidência do custo já pago" acima não contabilizou.

### C3 — o D2 não tem middleware onde pendurar o `ContextVar`

O ADR mandava setar o contexto no `ToolCallTracingMiddleware` / `CheckTokenMiddleware`. Nenhum dos dois serve: o de tracing só é registrado se `setup_tracing()` retornar True **e** `not IS_LOCAL` ([app.py:134](../../src/app.py#L134)); e `CheckTokenMiddleware` é **código morto** — definido em [check_token.py:7](../../src/middleware/check_token.py#L7) e não referenciado fora dos testes (`app.py` usa `HybridTokenVerifier` como `auth` provider, não como middleware). O D2 precisa de um middleware novo, incondicional.

### C4 — `user_id` não sai das assinaturas das tools

O critério "`user_id` deixa de ser parâmetro obrigatório nas assinaturas" não é atingível: `user_id` é **argumento de entrada da tool**, vindo do cliente MCP (`store_user_feedback(user_id: str, feedback: str)`), não da autenticação. O que o `ContextVar` elimina é o repasse manual **abaixo** da fronteira da tool, e o `_extract_user_id()` de best-effort do tracing.

### C5 — `PAIR_RESOLUTIONS_` não deve ser tocado

[openlocationcode.py](../../src/tools/equipments/openlocationcode.py) é a implementação de referência do Google (Apache-2.0) vendorizada. Congelar `PAIR_RESOLUTIONS_` diverge do upstream e complica qualquer atualização futura, em troca de nada. Sai da lista do D7.

### C6 — D3 vira CHATR-154

Ver a nota na própria seção D3.
