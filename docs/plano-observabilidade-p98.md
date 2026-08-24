# Plano: observabilidade para a redução do P98

**Objetivo de negócio:** hoje o P98 de resposta passa de 60s. A meta é nunca
ultrapassar esse tempo. Antes de otimizar, é preciso conseguir medir — este
documento cobre só a parte de observabilidade, que é pré-requisito das
melhorias não funcionais.

**Status:** Etapa 1 em andamento (baseline no SigNoz, feita fora do repo).
Etapas 2 em diante ainda não iniciadas.

> Este documento foi escrito para retomar o contexto mais tarde. O repo está
> sendo alterado em paralelo por outra frente — confirme as referências de
> arquivo/linha antes de aplicar as mudanças.

---

## Estado atual (o que JÁ existe)

Existe pipeline de tracing OpenTelemetry exportando OTLP/HTTP para o **SigNoz**,
ligado nos dois ambientes. Não é preciso construir observabilidade do zero.

| Camada | Onde | O que emite |
|---|---|---|
| Setup do tracer | `src/observability/tracing.py:44` | `TracerProvider` + `BatchSpanProcessor` → OTLP/HTTP |
| HTTP / ASGI | `src/main.py:32` | span da request `/mcp` |
| **Tool call** | `src/observability/tracing.py:150` | span `mcp.tool_call` + attrs `mcp.tool.name`, `mcp.tool.user_id`, `mcp.tool.success` |
| BigQuery | `src/utils/bigquery.py:302,377,474,725` | spans `bigquery.query`, `bigquery.save_*` |
| Correlação de erros | `src/utils/error_interceptor.py:54` | injeta `trace_id`/`span_id` no payload do interceptor |

Configuração de ambiente: `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` e
`OTEL_SERVICE_NAME` já setados em `k8s/prod/resources.yaml:40-43` e
`k8s/staging/resources.yaml:32-35` (`service.name=app-mcp-server`, collector
`signoz-otel-collector.signoz.svc.cluster.local:4318`).

O middleware só é registrado quando `setup_tracing()` retorna `True` **e**
`not IS_LOCAL` (`src/app.py:134`). Localmente não há span nenhum.

### O que NÃO existe

- Métricas OTel (só traces). Nenhum histograma, nenhum `/metrics`.
- Instrumentação de cliente HTTP. A única dep `opentelemetry-instrumentation-*`
  no `pyproject.toml` é `asgi`. A de `langchain` era usada apenas em `engine/`,
  removida junto com aquela subárvore em CHATR-105 — ver
  `docs/decisions/CHATR-105-debitos-tecnicos.md`.
- Spans para Gemini, Typesense, Redis, PGM, SGRC, Google Maps.
- Log padronizado de duração por tool. Existem timings ad-hoc e desconexos:
  `gemini_service.py:286,351` (só loga `elapsed_ms` quando houve retry ou
  falha — o caminho de sucesso não loga tempo), `divida_ativa.py:51`,
  `health/registry.py:169`, `poda_de_arvore/workflow.py:1007`.

### A consequência

O span `mcp.tool_call` é uma **caixa-preta**: dá para ver que uma tool levou
70s, mas não onde. Como quase toda tool sai pela rede via
`InterceptedHTTPClient` (httpx) e não há span de cliente HTTP, o P98 hoje é
descritivo, não acionável.

---

## Etapa 1 — Congelar o baseline (em andamento, sem mexer no código)

O dado do "antes" **já está sendo coletado**: duração é intrínseca ao span, não
precisa de atributo novo.

Query no SigNoz: span `mcp.tool_call`, agrupado por `mcp.tool.name`, quantis
p50 / p95 / p98 da duração + contagem de chamadas + taxa de
`mcp.tool.success=false`.

- Janela sugerida: 7 a 14 dias, para a cauda ter massa.
- Exportar como CSV e commitar em `docs/baselines/p98-antes-<AAAA-MM-DD>.csv`.
  Sem o arquivo versionado, a comparação depois vira memória.

**Antes de confiar no número, verificar duas coisas:**

1. **Amostragem.** Nenhum sampler é configurado em `tracing.py`, então o default
   é 100% das traces. Confirmar que `OTEL_TRACES_SAMPLER` não está setado no
   ambiente — se estiver, o p98 está distorcido.
2. **Perda de spans.** O `BatchSpanProcessor` usa `max_queue_size=8192`
   (`tracing.py:96`). Se estiver havendo drop sob carga, a cauda é justamente o
   que se perde primeiro. Checar as métricas de export do collector.

**Critério de pronto:** CSV commitado, com janela e data anotadas.

---

## Etapa 2 — Span por chamada externa (maior retorno pelo menor esforço)

Sem isso, as etapas de otimização viram tentativa e erro.

1. Adicionar `opentelemetry-instrumentation-httpx>=0.57b0` ao `pyproject.toml`
   (mesma faixa de versão da instrumentação `asgi` já presente).
2. Em `setup_tracing()`, logo após `trace.set_tracer_provider(provider)`
   (`src/observability/tracing.py:102`):

   ```python
   from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
   HTTPXClientInstrumentor().instrument()
   ```

   Manter dentro do `try/except` existente: a premissa do módulo é que nada
   daqui derruba a aplicação.

Cobre de uma vez todo o `InterceptedHTTPClient` e, por tabela, as chamadas do
`google-genai` (que usa httpx internamente — **confirmar na prática**, olhando se
aparecem spans de rede dentro do `mcp.tool_call` do `google_search`).

**Fica de fora:** `equipments_workflow.py:39` usa `requests`, não httpx. Se esse
caminho importar para o P98, ou migra para httpx ou adiciona
`opentelemetry-instrumentation-requests`.

**Critério de pronto:** abrir uma trace de `google_search` no SigNoz e enxergar
os spans filhos de rede com suas durações.

---

## Etapa 3 — Log estruturado de duração por tool call

Baseline paralelo ao SigNoz, agregável com `kubectl logs | jq`, útil quando não
se quer depender do dashboard.

Em `ToolCallTracingMiddleware.on_call_tool`
(`src/observability/tracing.py:150`), medir com `time.monotonic()` e emitir uma
linha por chamada:

```
tool_call tool=<nome> duration_ms=<int> success=<bool>
```

**Atenção ao desenho:** hoje o middleware faz early-return quando
`_tracing_enabled` é `False` (`tracing.py:155-156`). Para o log existir
independente do tracing, a medição precisa sair de dentro dessa guarda — e a
guarda passa a valer só para a criação do span. Vale revisar junto se o
middleware deve ser registrado mesmo sem tracing habilitado (`src/app.py:134`),
o que daria medição local também.

**Critério de pronto:** uma linha por tool call no log, com o mesmo p98 do
SigNoz quando agregada.

---

## Etapa 4 — Spans manuais nos trechos internos que não são rede

Só depois da Etapa 2, e só onde ela mostrar buraco. Trechos que consomem tempo
sem sair pela rede não aparecem na instrumentação de httpx — o candidato óbvio é
`resolve_urls()` (`src/tools/google_search/gemini_service.py:625`), que orquestra
dezenas de requests e cujo custo agregado só fica visível com um span próprio
envolvendo a chamada inteira.

Usar `get_tracer()` de `src/observability/tracing.py`, no mesmo padrão dos spans
de BigQuery.

---

## Não fazer antes de a comparação fechar

Renomear o span `mcp.tool_call` (hoje o nome é igual para toda tool; o nome da
tool é só atributo). Colocar o nome da tool no span deixaria a leitura no SigNoz
melhor, mas mudar isso **entre** a medição do antes e a do depois quebra a
comparação e invalida as queries do baseline. Fica para depois de o ganho estar
demonstrado.

---

## Anexo: suspeitos de cauda já mapeados

Levantados por leitura de código, ainda **não confirmados por medição** — servem
de hipótese para a fase de otimização, não de conclusão.

- **`google_search`** é o candidato mais forte. `asyncio.timeout(180)` é por
  tentativa (`gemini_service.py:172`), com até 4 tentativas
  (`GEMINI_SEARCH_RETRY_ATTEMPTS`, default 4) e budget de retry de 60s. Depois
  que o Gemini responde, `resolve_urls()` ainda valida cada link com HEAD+GET,
  3 tentativas e 5s de timeout cada (`gemini_service.py:479-484`), com semáforo
  de 20 — custo puramente aditivo antes de devolver a resposta.
- **`internal_request`** com `timeout=600.0` (`src/tools/utils.py:58`).
- **`web_search_surkai`** (`:31`), **`dharma_search`** (`:29`) e **`memory`**
  (`:71,126`) com timeout de 120s — acima da meta de 60s por si só.
- Não existe teto de tempo global por tool call. Nenhum dos timeouts acima
  conversa com o alvo de 60s.
