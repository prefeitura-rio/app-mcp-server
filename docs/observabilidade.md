# Observabilidade do `app-mcp-server`

Como o serviço é observado hoje, o que cada peça emite, e como usar isso para
investigar um problema. Cobre a épica **CHATR-101** (sub-tasks CHATR-110 a
CHATR-114).

> Escopo: só **tracing distribuído** (OpenTelemetry → SigNoz) e o
> `error_interceptor`. Não há métricas OTel (histogramas, `/metrics`) nem
> logging estruturado agregável — ver [Lacunas conhecidas](#lacunas-conhecidas).

---

## 1. Arquitetura em uma tela

```
                          src/main.py
                              │
              ┌───────────────┴───────────────┐
              │  setup_tracing()  (chamado    │
              │  por create_app em src/app.py)│
              └───────────────┬───────────────┘
                              │  TracerProvider global
                              │  + BatchSpanProcessor
                              ▼
   ┌──────────────────────────────────────────────────┐
   │  Camadas que emitem spans                        │
   ├──────────────────────────────────────────────────┤
   │ 1. ASGI  (OpenTelemetryMiddleware)  → POST /mcp  │
   │ 2. Tool  (ToolCallTracingMiddleware) → mcp.tool_call
   │ 3. BigQuery (spans manuais)          → bigquery.*│
   └──────────────────────────┬───────────────────────┘
                              │ OTLP/HTTP :4318/v1/traces
                              ▼
              signoz-otel-collector.signoz.svc.cluster.local
                              │
                              ▼
                    SigNoz  (service.name = app-mcp-server)

   error_interceptor  ──POST──▶  ERROR_INTERCEPTOR_URL
        (payload carrega trace_id/span_id → link de volta pro SigNoz)
```

---

## 2. Peças implementadas

### 2.1 Setup do tracer — `src/observability/tracing.py`

| Item | Onde | Comportamento |
|---|---|---|
| `setup_tracing()` | [tracing.py:75](../src/observability/tracing.py#L75) | Cria `TracerProvider` + `OTLPSpanExporter` + `BatchSpanProcessor`. **Idempotente** e **nunca levanta exceção** — falha vira log e tracing desligado. |
| `is_tracing_enabled()` | [tracing.py:44](../src/observability/tracing.py#L44) | Consulta se o setup deu certo. Usado por `main.py` para decidir se instrumenta a camada ASGI. |
| `get_tracer()` | [tracing.py:155](../src/observability/tracing.py#L155) | **Ponto de reuso**: qualquer módulo que queira um span manual chama isto. Tracer nomeado `app-mcp-server`. |
| `run_in_executor_with_context()` | [tracing.py:160](../src/observability/tracing.py#L160) | `loop.run_in_executor` que copia os `contextvars` para a thread, para o span aberto lá dentro continuar filho do `mcp.tool_call`. Ver seção 2.4. |

#### Resource attributes

Montados por `_build_resource_attributes()`
([tracing.py:49](../src/observability/tracing.py#L49)) e aplicados a **todos** os
spans do processo:

| Atributo | Origem | Para que serve |
|---|---|---|
| `service.name` | `OTEL_SERVICE_NAME` | Identifica o serviço no SigNoz. |
| `deployment.environment` | `ENVIRONMENT` | **Separa staging de prod.** Sem ele os dois publicam no mesmo stream e um alerta de taxa de erro avalia os dois juntos. |
| `k8s.pod.name` | `K8S_POD_NAME` (downward API) | Atribui um pico de latência ou uma rajada de erro a uma réplica específica. |

Atributo cujo valor chega vazio **não é publicado** — um
`deployment.environment=""` no SigNoz é pior que a ausência, porque parece um
valor legítimo na hora de filtrar. Fora do cluster não há downward API, então
`k8s.pod.name` simplesmente não existe.

> `ENVIRONMENT` **não** está nos manifests: vem do secret `mcp-secrets`. O valor
> resolvido é impresso no log de startup justamente para isso ser conferível com
> um `kubectl logs` — se o secret de prod não a define, o default de
> [env.py:21](../src/config/env.py#L21) é `staging` e os dois ambientes voltam a
> se misturar.

Parâmetros do `BatchSpanProcessor`: `max_queue_size=8192`,
`schedule_delay_millis=1000`, `export_timeout_millis=10000`,
`max_export_batch_size=256`.

Nenhum sampler é configurado → **100% das traces** são exportadas.

### 2.2 Span por chamada de tool — `ToolCallTracingMiddleware`

Registrado em [app.py:134](../src/app.py#L134), **somente quando**
`setup_tracing()` retorna `True` **e** `not IS_LOCAL`.

| Span | Atributos |
|---|---|
| `mcp.tool_call` | `mcp.tool.name`, `mcp.tool.user_id`, `mcp.tool.success` |

Em exceção: `span.record_exception(e)` + `Status(StatusCode.ERROR)` — é isso que
alimenta a aba **Exceptions** do SigNoz.

O nome do span é **fixo** (`mcp.tool_call`); a tool fica no atributo
`mcp.tool.name`. Isso é deliberado — renomear quebraria a comparação de baseline
do P98 (ver `docs/plano-observabilidade-p98.md`).

### 2.3 Span HTTP/ASGI

[main.py:30-34](../src/main.py#L30-L34) injeta o `OpenTelemetryMiddleware` do
`opentelemetry-instrumentation-asgi` no `mcp.run(...)`, só quando o tracing está
habilitado. Gera o span raiz da request (`POST /mcp`, `GET /health`, …).

### 2.4 Spans de BigQuery — `src/utils/bigquery.py`

| Span | Origem | Atributos |
|---|---|---|
| `bigquery.read` | [bigquery.py:1364](../src/utils/bigquery.py#L1364) | `bigquery.call_type`, `bigquery.timeout_budget_seconds`, `cache.enabled`, `cache.hit`, `cache.key`, `cache.written`, `cache.ttl_seconds`, `cache.coalesced`, `cache.write_skipped`, `cache.read_error` |
| `bigquery.query` | [bigquery.py:1078](../src/utils/bigquery.py#L1078) | `page_size`, `query_length`, `row_count`, `success`, `table_not_found` |
| `bigquery.save_response` | [bigquery.py:321](../src/utils/bigquery.py#L321) | `project_id`, `dataset_id`, `table_id`, `endpoint`, `row_count`, `success` |
| `bigquery.save_feedback` | [bigquery.py:397](../src/utils/bigquery.py#L397) | `project_id`, `dataset_id`, `table_id`, `row_count`, `success` |
| `bigquery.save_cor_alert` | [bigquery.py:495](../src/utils/bigquery.py#L495) | `project_id`, `dataset_id`, `table_id`, `alert_type`, `severity`, `row_count`, `success` |

### 2.5 Span do line-up do Rock in Rio — `src/tools/rock_in_rio/cache.py`

| Span | Origem | Atributos |
|---|---|---|
| `rock_in_rio.lineup_fetch` | [cache.py](../src/tools/rock_in_rio/cache.py) (`_buscar_e_guardar`) | `rock_in_rio.success`, `rock_in_rio.failure_kind` (`formato` \| `fonte`), `rock_in_rio.atracoes`, `rock_in_rio.palcos`, `rock_in_rio.dias` |

É o **único span raiz que não nasce de uma request**: quem o emite é o laço de
atualização em background, a cada 15 min. Vira uma trace própria, sem pai, e é
por isso que ele existe — quando o site de terceiro muda de estrutura de
madrugada, não há chamada de tool acontecendo para carregar o sinal.

`rock_in_rio.failure_kind` separa os dois modos de falha porque a ação é oposta:
`formato` é o site tendo mudado o HTML (exige correção de código, não passa
sozinho) e `fonte` é rede/HTTP (transitório). Um alerta que não distingue os
dois não diz o que fazer.

O `mcp.tool_call` da `rock_in_rio_lineup` ganha `rock_in_rio.degraded = true` e
`rock_in_rio.motivo` quando a tool devolve resposta indisponível. É o primeiro
caso no projeto de tool que sinaliza degradação **sem levantar**: o
`ToolCallTracingMiddleware` só marca `mcp.tool.success = False` em exceção, e
esta tool devolve dicionário de propósito. Filtrar por `rock_in_rio.degraded`
é o que encontra essas chamadas. O padrão vale para qualquer outra tool que
passe a devolver resposta degradada em vez de falhar.

A falha também alimenta `mcp.dependency.errors` e
`mcp.dependency.call.duration` com `dependency.name = rock_in_rio` (seção 4) —
é a série temporal, e não o span, que responde "quantas vezes isso caiu na
última hora".

---

`bigquery.read` cobre a chamada **inteira** (cache + fila do single-flight +
query) e é o span que mede a latência percebida pela tool; `bigquery.query` é
filho dele e existe só em cache miss, medindo o custo do BigQuery em si.

**Agrupamento:** as escritas se agrupam por `bigquery.table_id`. A leitura se
agrupa por `bigquery.call_type`, que vem do `cache_namespace` da chamada
(`equipments`, `equipments_categories`, `equipments_instructions`, …) — é a
dimensão que responde "p50/p95 por tipo de chamada" sem precisar parsear SQL.
Leitura sem namespace cai no bucket `unspecified`, de propósito: bucket
explícito aparece no painel, ausência some dele.

**Propagação de contexto:** as 5 chamadas instrumentadas rodam em thread de
executor, que **não** copia `contextvars` — e o contexto do OTel vive num
contextvar. Todas passam por `run_in_executor_with_context()`
([tracing.py:160](../src/observability/tracing.py#L160)), sem o qual os spans
`bigquery.*` nasceriam como raiz de trace própria em vez de filhos do
`mcp.tool_call`: existiriam, com duração e status, mas não daria para ir da tool
lenta até a query que a segurou.

Além desses, a própria lib `google-cloud-bigquery` emite spans automáticos
(ex.: `BigQuery.insertRowsJson`) quando o OTel está presente no processo.

### 2.5 Correlação de erros — `src/utils/error_interceptor.py`

`_get_current_trace_context()` ([error_interceptor.py:39](../src/utils/error_interceptor.py#L39))
injeta `trace_id` e `span_id` (hex, mesmo formato do SigNoz) no payload enviado
ao interceptor. Degrada em silêncio quando não há span ativo.

Também implementados na mesma frente (CHATR-113):
- **Redação de PII** antes de sair do processo: CPF, telefone e
  `customer_whatsapp_number` mascarados.
- **Correção do fire-and-forget**: as tasks do wrapper síncrono agora ficam num
  set com `add_done_callback`, em vez de `create_task` solto que podia sumir.

---

## 3. Configuração

### Variáveis de ambiente ([env.py:224-232](../src/config/env.py#L224-L232), [env.py:21-26](../src/config/env.py#L21-L26))

| Variável | Default | Efeito |
|---|---|---|
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | *(vazio)* | **Chave liga/desliga.** Sem ela, tracing desabilitado. URL base do collector (o `/v1/traces` é anexado no código). |
| `OTEL_SERVICE_NAME` | `app-mcp-server` | Vira `service.name` no SigNoz. |
| `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | *(vazio)* | Headers extras, formato `k=v,k2=v2`. |
| `ENVIRONMENT` | `staging` | Vira `deployment.environment`. Vem do secret `mcp-secrets`, não do manifest. |
| `K8S_POD_NAME` | *(vazio)* | Vira `k8s.pod.name`. Preenchido pela downward API nos manifests; vazio fora do cluster. |

### Cluster

Prod ([k8s/prod/resources.yaml:40-53](../k8s/prod/resources.yaml#L40-L53)) e
staging ([k8s/staging/resources.yaml:32-45](../k8s/staging/resources.yaml#L32-L45))
setam inline:

```yaml
env:
  - name: OTEL_SERVICE_NAME
    value: "app-mcp-server"
  - name: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
    value: "http://signoz-otel-collector.signoz.svc.cluster.local:4318"
  - name: K8S_POD_NAME
    valueFrom:
      fieldRef:
        fieldPath: metadata.name
```

`ENVIRONMENT` **não** está aqui — chega pelo `envFrom: secretRef: mcp-secrets`.

### Local

Localmente **não há span nenhum**: o middleware de tool só é registrado quando
`not IS_LOCAL`, e o de ASGI só no caminho não-local do `main.py`. Para ver spans
em dev seria preciso subir um collector e relaxar essas guardas.

---

## 4. Como usar

### 4.1 Latência por tool (o caso mais comum)

No SigNoz, serviço `app-mcp-server`:

- **Span**: `mcp.tool_call`
- **Group by**: `mcp.tool.name`
- **Métricas**: p50 / p95 / p99 da duração, contagem, taxa de `mcp.tool.success = false`

Isso responde "qual tool está lenta" e "qual tool está falhando". Para "onde
dentro da tool", ver as limitações da seção 5 — hoje o span é quase uma
caixa-preta.

### 4.2 Investigar um erro reportado por um usuário

1. Pegue o payload que chegou no `ERROR_INTERCEPTOR_URL` — ele tem `trace_id`.
2. Cole o `trace_id` na busca de traces do SigNoz.
3. A trace mostra a request HTTP, o `mcp.tool_call` e os spans de BigQuery.
4. A aba **Exceptions** do SigNoz agrupa por serviço + tipo + mensagem, e
   linka de volta para o span de origem.

### 4.3 Custo/latência de BigQuery

- **Latência percebida pela tool**: span `bigquery.read`, group by
  `bigquery.call_type`, quantis p50/p95. Inclui cache e fila do single-flight.
- **Custo do BigQuery em si**: span `bigquery.query`. Só existe em **cache
  miss** — o hit de Redis retorna antes dele.
- **Taxa de acerto de cache**: `cache.hit` no `bigquery.read`. `cache.coalesced`
  distingue quem foi atendido pela fila do single-flight.
- **Escrita**: spans `bigquery.save_*`, group by `bigquery.table_id`.
- **Filtros úteis**: `bigquery.success = false` para falhas; `bigquery.timeout =
  true` para estouro de prazo; `cache.write_skipped` para entender por que um
  resultado não foi cacheado (`redis_unavailable` ou `degraded_result`).

### 4.4 Instrumentar código novo (o padrão a reusar)

Não crie um `TracerProvider` novo nem importe `opentelemetry` direto. Use o
acessor do módulo:

```python
from opentelemetry.trace import Status, StatusCode
from src.observability.tracing import get_tracer

tracer = get_tracer()
with tracer.start_as_current_span("typesense.search") as span:
    span.set_attribute("typesense.collection", collection)
    try:
        result = do_work()
        span.set_attribute("typesense.success", True)
        span.set_status(Status(StatusCode.OK))
        return result
    except Exception as e:
        span.set_attribute("typesense.success", False)
        span.record_exception(e)
        span.set_status(Status(StatusCode.ERROR, str(e)))
        raise
```

Quando o tracing está desligado, `get_tracer()` devolve um tracer no-op — o
bloco continua funcionando sem custo relevante e sem `if` de guarda.

**Convenções em uso:** prefixo do domínio no nome (`bigquery.`, `mcp.`), sempre
um atributo `<domínio>.success`, e `record_exception` + `set_status` no `except`.

> Esse bloco de 12 linhas está hoje **copiado** em 5 lugares do
> `bigquery.py`. É o candidato número um a virar um context manager/decorator
> reusável — ver seção 5.

---

## 5. Lacunas conhecidas

Em ordem de impacto sobre a capacidade de investigar um incidente:

1. **Sem span de cliente HTTP.** `opentelemetry-instrumentation-httpx` não está
   no `pyproject.toml` (a única instrumentação presente é a `asgi`) — como
   quase toda tool sai pela rede via `InterceptedHTTPClient`, o `mcp.tool_call`
   não mostra onde o tempo foi. É a **Etapa 2** de
   `docs/plano-observabilidade-p98.md`, e o maior retorno pelo menor esforço.
2. **Sem spans para Gemini, Typesense, Redis, PGM, SGRC e Google Maps.** A
   Etapa 2 cobre por tabela tudo que passa por httpx; o que sobrar precisa de
   span manual (Etapa 4 do mesmo plano).
3. **Sem métricas OTel** (histogramas, contadores) e **sem log estruturado de
   duração por tool** — só traces. Etapa 3 do plano do P98.
4. **Duplicação do bloco de span** — o padrão da seção 4.4 está copiado ao
   longo do `bigquery.py`, cada cópia com um conjunto ligeiramente diferente de
   atributos. Candidato a virar um context manager reusável.
5. **Localmente não há span nenhum.** Ver seção 3 — é deliberado, mas custa na
   hora de validar instrumentação nova antes do deploy.

### Fechadas

| Lacuna | Onde foi resolvida |
|---|---|
| Spans de BigQuery órfãos (executor não propaga `contextvars`) | `run_in_executor_with_context()`, seção 2.4 |
| `bigquery.read` sem dimensão de agrupamento | `bigquery.call_type`, seção 2.4 |
| Sem `deployment.environment` no `Resource` (staging e prod no mesmo stream) | seção 2.1 |
| Sem `k8s.pod.name` no `Resource` (recomendação do CHATR-114) | seção 2.1 |
| `tracing.py` sem teste unitário | `src/tests/unit/observability/test_tracing.py` |
| Cache hit invisível | `cache.hit` e afins no `bigquery.read` (CHATR-115) |
| Timeout de leitura não aparecia como erro | `bigquery.timeout` no `bigquery.query` (CHATR-125) |
| Tool que devolve resposta degradada saía como sucesso no span | `rock_in_rio.degraded`, seção 2.5 — descoberto quando o site do Rock in Rio mudou (01/09/2026) e a queda não gerou erro nenhum |
| Quebra de raspagem invisível fora de uma chamada de tool | `rock_in_rio.lineup_fetch`, seção 2.5 |

---

## 6. Alertas

**Ainda não existem alertas ativos.** O estado (CHATR-112):

- Terraform escrito e commitado em `iac-superapp`
  (`modules/deployments/signoz-alerts.tf`), com dois recursos: taxa de erro de
  traces > 5% / 5min (warning) e restarts de pod > 2 / 10min (critical), ambos
  para o Discord `#alerts-signoz`.
- Provider `SigNoz/signoz` **pinado em `0.0.17`** — o recurso `signoz_alert`
  (API v1) foi removido em v0.1.0 em favor de `signoz_rule` (API v2), que está
  quebrada nessa instância.
- **Bloqueio**: `signoz_access_token` está como string vazia nos `.sops.json` de
  staging e prod. Falta alguém com acesso admin gerar a Service Account API key
  no SigNoz e rodar `apply`.

---

## 7. Referências

- `docs/plano-observabilidade-p98.md` — plano para reduzir o P98, que depende
  desta base.
- `docs/decisions/CHATR-113-sentry-vs-error-interceptor.md` — por que não
  adotamos Sentry.
- Épica: [CHATR-101](https://iplanrio-pcrj.atlassian.net/browse/CHATR-101)
