# CHATR-105 — Débitos técnicos e limpezas de menor prioridade

**Status:** decidido e implementado
**Data:** 2026-08-20
**Sub-tasks:** CHATR-123, CHATR-124, CHATR-127, CHATR-128

O critério de aceite do épico é ter **uma decisão registrada** para cada item
(fazer, não fazer, ou adiar com justificativa). Este documento é esse registro.

---

## CHATR-128 — Destino do código de OpenTelemetry em `engine/agent.py`

**Decisão: REMOVER.**

### Contexto

`engine/` e `src/utils/agent/` hospedavam a única instrumentação de OpenTelemetry
do repositório fora de `src/observability/`. A auditoria original precisou
descartar a hipótese de que a observabilidade do serviço já existia por ali.

O grafo de importação confirma que era código morto do ponto de vista do serviço:

```
engine/agent.py              ← importado apenas por src/utils/agent/interactive_chat.py
engine/custom_react_agent.py ← importado apenas por engine/agent.py
engine/log.py                ← importado apenas por engine/{agent,custom_react_agent}.py
src/utils/agent/*            ← importado apenas por interactive_chat.py
```

`src/utils/agent/interactive_chat.py` era um script standalone (Vertex AI Agent
Engine), executado à mão. Nenhum entrypoint de produção — `src/main.py`,
`src/app.py`, `Dockerfile` — alcançava essa subárvore.

A observabilidade real do serviço vive em `src/observability/tracing.py`, opt-in
e defensiva, acionada por `src/main.py`. Caminho totalmente separado.

### Por que remover em vez de documentar

Manter dois `TracerProvider` globais concorrentes no mesmo repositório é um risco
de confusão permanente — `engine/agent.py` chamava `trace.set_tracer_provider()`,
o mesmo global que `src/observability/tracing.py` configura. Nenhum dos dois
rodava junto do outro na prática, mas a coexistência no código é exatamente o
que fez a auditoria inicial perder tempo.

O histórico do Git preserva o código caso ele precise ser resgatado.

### O que foi removido

| Caminho | Linhas |
|---|---|
| `engine/agent.py` | 981 |
| `engine/custom_react_agent.py` | 1005 |
| `engine/log.py` | — |
| `engine/__init__.py` | — |
| `src/utils/agent/interactive_chat.py` | 350 |
| `src/utils/agent/prompt.py` | — |
| `src/utils/agent/tools.py` | — |
| `src/utils/agent/utils.py` | — |
| **Total** | **~2.971** |

### Dependências removidas

Órfãs após a remoção, verificadas uma a uma como sem nenhum outro consumidor:

- `langchain-google-cloud-sql-pg` — só `engine/agent.py` (checkpointer PostgreSQL)
- `langchain-google-vertexai` — só `engine/agent.py` (`ChatVertexAI`)
- `opentelemetry-instrumentation-langchain` — só `engine/agent.py` (`LangchainInstrumentor`)

`uv sync` desinstalou 12 pacotes contando os transitivos (`asyncpg`,
`cloud-sql-python-connector`, `pgvector`, `pyarrow`, `numexpr`, `bottleneck`,
`validators`, `opentelemetry-semantic-conventions-ai`). Menos superfície na
imagem de produção e menos CVEs para o Trivy varrer.

**Sobre `pyarrow`:** era transitivo, não dependência direta. As leituras de
BigQuery em `src/utils/bigquery.py` usam o caminho REST (`query_job.result()` +
`row.items()`); não há uso de `to_dataframe()`, `to_arrow()` nem
`bqstorage_client` em lugar nenhum do código, então Arrow nunca era exercitado.

### Mantidas

`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` e
`opentelemetry-instrumentation-asgi` continuam — são o que
`src/observability/tracing.py` e `src/main.py` usam de fato.

---

## CHATR-124 — Ciclo de vida do `_memory_cache`

**Decisão: OBSOLETO por remoção.**

O `self._memory_cache = {}` vivia em `engine/agent.py`, removido acima. O item
deixa de existir junto com o arquivo.

### Riscos registrados, caso o padrão volte

A auditoria original apontou que o cache não sobrevivia a restart de pod nem era
compartilhado entre réplicas. A leitura do código antes da remoção revelou um
problema adicional, mais sério, que não constava do ticket:

O hook síncrono de injeção de memória chamava `loop.run_until_complete()` /
`asyncio.run()` para executar `_fetch_long_term_memory`. Em contexto assíncrono
isso **bloquearia o event loop**; o código contornava com um
`if loop.is_running(): ... skipping` que simplesmente desistia de buscar a
memória e logava um warning — ou seja, em produção o cache silenciosamente não
funcionaria.

Se esse padrão for reaproveitado no futuro:

- Usar **Redis** (já disponível no cluster) para qualquer estado que precise
  sobreviver ao ciclo de vida de um pod ou ser compartilhado entre réplicas.
- **Nunca** usar `run_until_complete()` ou `asyncio.run()` dentro de código que
  possa rodar sob um event loop ativo. O hook deve nascer `async`.

---

## CHATR-127 — Consolidar `CustomJSONEncoder`

**Decisão: FEITO** (já estava concluído antes desta mudança; registrado aqui
para fechar o épico).

`CustomJSONEncoder` vive em `src/utils/json_utils.py` e é referenciado por todos
os pontos que precisam dele:

- `src/utils/bigquery.py` — DLQ, `save_response_in_bq`, cache
- `src/utils/error_interceptor.py` — serialização do corpo da requisição

`src/tools/equipments/utils.py` não tem mais definição própria.

---

## CHATR-123 — Piso de cobertura do CI e testes de regressão de datetime

**Decisão: FAZER** (a metade que faltava).

### Testes de regressão — já existiam

`src/tests/unit/utils/test_bigquery_json_serialization.py` cobre exatamente o
cenário pedido:

- `save_response_in_bq` com payload contendo `datetime.time` / `date` /
  `datetime` (colunas `TIME`/`DATE`/`DATETIME` do BigQuery)
- `get_bigquery_result` convertendo `TIME` para string ISO
- o encoder isolado

### Piso do CI — corrigido

O gate em `.github/workflows/pr-quality-gate.yaml` estava com
`minimum_coverage = 50.0` e `.github/coverage-baseline.json` em `61.5`, enquanto
a cobertura real era **78,53%**. O piso estava ~28pp abaixo da realidade: um PR
podia derrubar um terço da suíte sem o CI reclamar.

| Parâmetro | Antes | Depois |
|---|---|---|
| `minimum_coverage` | 50,0% | **75,0%** |
| `coverage-baseline.json` | 61,5 | **80,0** |
| `tolerance` (ratchet) | 0,1pp | 0,1pp (inalterado) |

Cobertura medida após a remoção do código morto: **80,04%** (522 testes,
zero skips, determinístico em duas execuções). A subida de 78,53% → 80,04% vem
dos ~309 statements a 0% que saíram do denominador.

**Por que 75% e não 78%:** ~5pp de folga absorvem a flutuação natural entre PRs
sem transformar o gate num obstáculo que induz teste escrito só para passar. O
ratchet de 0,1pp continua sendo o mecanismo que barra regressão incremental; o
`minimum` é a rede de segurança absoluta.

**Por que baseline 80,0 e não 80,04:** deixa 0,04pp de folga somados à tolerância
de 0,1pp, absorvendo qualquer diferença mínima entre o runner do CI e o ambiente
local.

---

## Follow-ups não incluídos nesta mudança

Fora do escopo acordado para CHATR-105, registrados para não se perderem:

- **Env vars órfãs em `src/config/env.py`.** `EAI_AGENT_URL`, `EAI_AGENT_TOKEN`,
  `PROJECT_NUMBER`, `REASONING_ENGINE_ID`, `INSTANCE`, `DATABASE`,
  `DATABASE_USER`, `DATABASE_PASSWORD` e `LOCATION` eram consumidas apenas pelo
  código removido. Todas são declaradas com `action="ignore"` e **não** constam
  de `REQUIRED_ENV_VARS`, então a remoção do código não afeta o preflight nem o
  teste `test_required_env_sync.py`. `env.py` foi deixado intocado de propósito.
- **`langchain-google-genai` e `google-cloud-bigquery-storage`** não têm nenhum
  consumidor no código — condição anterior a esta mudança, não causada por ela.
- **`src/utils/test_agent.py`** tem nome com prefixo `test_` mas é código de
  aplicação, não suíte de teste. Confunde a coleta do pytest e aparece com 21%
  de cobertura.
