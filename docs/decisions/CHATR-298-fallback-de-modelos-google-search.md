# Fallback de modelos no google_search: variáveis de ambiente e operação

**Jira:** [CHATR-298](https://iplanrio-pcrj.atlassian.net/browse/CHATR-298)
**PR:** [#193](https://github.com/prefeitura-rio/app-mcp-server/pull/193)
**Data:** 22 de outubro de 2026
**Incidente de origem:** 21/10/2026, das 13h08 às 13h43. O `gemini-2.5-flash-lite` ficou sem capacidade no Google (503 "high demand" e chamadas travadas até o prazo), e 27 de 39 buscas falharam na hora das 13h.

---

## Resumo

A busca percorre uma lista de modelos em ordem de prioridade. Cada modelo tem o seu prazo dentro do prazo total. Um erro transitório (503, 429, 5xx) ganha uma nova tentativa curta no mesmo modelo; qualquer outra falha (prazo do modelo, `RECITATION`, `MAX_TOKENS`, resposta vazia, 4xx) passa direto para o próximo. Só quando todos falham a busca vira falha técnica e gera alerta.

Tudo isso é controlado por variáveis de ambiente, para que a ordem dos modelos e os prazos possam mudar **sem deploy**. Este documento descreve cada variável, o estado de cada ambiente e o procedimento para alterar.

---

## Variáveis

Definidas em [`src/config/env.py`](../../src/config/env.py). Quando ausentes no ambiente, vale o default do código.

| Variável | Default | O que controla |
| --- | --- | --- |
| `GEMINI_SEARCH_MODELS` | `gemini-2.5-flash-lite,gemini-3.5-flash-lite` | Modelos da busca, em ordem de prioridade, separados por vírgula |
| `GEMINI_SEARCH_MODEL_DEADLINE_SECONDS` | `45` | Prazo de cada modelo (todas as tentativas nele). Cobre o p99 das chamadas bem-sucedidas (27,6s em out/2026) |
| `GEMINI_SEARCH_DEADLINE_SECONDS` | `100` (era `45`) | Prazo total da busca: todos os modelos, o backoff e a resolução de URLs |
| `GEMINI_SEARCH_ATTEMPTS_PER_MODEL` | `2` | Tentativas por modelo em erro transitório (1 chamada + 1 retry) |
| `GEMINI_SEARCH_THINKING_LEVEL` | `MINIMAL` | Raciocínio dos modelos 3.x (`MINIMAL`, `LOW`, `MEDIUM`, `HIGH`) |
| `GEMINI_SEARCH_REASONING_BUDGET_TOKENS` | `8192` | Raciocínio dos modelos 2.x. Não vale para os 3.x (inalterada) |
| `GEMINI_SEARCH_MAX_OUTPUT_TOKENS` | `12288` | Teto de saída de qualquer modelo (inalterada) |
| `GEMINI_SEARCH_RETRY_BASE_SECONDS` | `2` | Base do backoff do retry: espera entre 1 e 2s (inalterada) |
| `GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS` | `16` | Teto da espera, inclusive quando a API informa `retryDelay` (inalterada) |

Variáveis que **deixaram de valer para a busca**:

| Variável | Situação |
| --- | --- |
| `GEMINI_MODEL` | Continua lida pelo `env.py`, mas a busca usa `GEMINI_SEARCH_MODELS`. Definir o modelo da busca por ela não tem mais efeito |
| `GEMINI_SEARCH_RETRY_ATTEMPTS` | Removida, substituída por `GEMINI_SEARCH_ATTEMPTS_PER_MODEL`. Se existir no Infisical, é ignorada |

A geração do modelo é deduzida pelo nome: `gemini-2.*` usa `thinking_budget`; qualquer outro usa `thinking_level`.

---

## Regras para mexer nos prazos

1. **O total precisa caber todos os modelos:** `GEMINI_SEARCH_DEADLINE_SECONDS ≥ nº de modelos × GEMINI_SEARCH_MODEL_DEADLINE_SECONDS + 10`. Os 10s são a reserva da resolução de URLs. Abaixo disso, o último modelo da lista fica com pouco ou nenhum tempo quando os anteriores travam.
2. **O total não pode passar de ~105s.** O limite real do Salesforce é o teto de 120s de callout por transação Apex. O Apex vê cerca de 6s a mais que o prazo daqui, e a transação ainda faz `initialize` e `notifications/initialized` antes do `tools/call`. O `setTimeout` de 60s do `HttpToolHelperBase` não dispara, porque os pings SSE a cada 15s zeram o timeout de leitura.
3. **Para três modelos ou mais**, reduza o prazo por modelo em vez de aumentar o total. Exemplo: 3 modelos × 30s + 10s = 100s.

---

## Estado por ambiente

Levantado em 22/10/2026 nas chaves do secret `mcp-secrets` (namespace `mcp`), que o operador do Infisical sincroniza a partir do Infisical do próprio cluster.

| Variável | Staging | Prod | Ação |
| --- | --- | --- | --- |
| `GEMINI_SEARCH_DEADLINE_SECONDS` | `45` | não definida (vale `100`) | **Staging: remover a chave ou pôr `100` antes de validar o PR.** Com `45`, o segundo modelo só ganha tempo quando o primeiro falha rápido. Prod: nada |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | `gemini-2.5-flash-lite` | Nenhuma. Ficou sem efeito na busca; pode ser removida depois, numa limpeza |
| Demais variáveis da tabela acima | não definidas | não definidas | Nenhuma: valem os defaults |

**Este PR não exige mudança de variável em prod.**

---

## Quando prod vai precisar de mudança

| Situação | O que mudar |
| --- | --- |
| Promover um 3.x a modelo principal ([CHATR-302](https://iplanrio-pcrj.atlassian.net/browse/CHATR-302)) | `GEMINI_SEARCH_MODELS`. Atenção ao custo: nos 3.x o grounding é cobrado por query de busca (5.000 grátis por mês, depois US$ 14/1.000) |
| O Google desligar ou bloquear o 2.5 | Tirar o 2.5 de `GEMINI_SEARCH_MODELS`. Sem isso a busca continua funcionando: o 4xx do 2.5 passa direto para o próximo, mas cada busca paga uma chamada perdida |
| Incidente prolongado num modelo específico | Inverter a ordem em `GEMINI_SEARCH_MODELS` até o modelo voltar. Evita que toda busca espere o modelo degradado antes do fallback |
| Adicionar um terceiro modelo | `GEMINI_SEARCH_MODELS` e `GEMINI_SEARCH_MODEL_DEADLINE_SECONDS`, respeitando as regras de prazo acima |

---

## Como alterar

1. **Editar no Infisical do cluster**, não no `infisical.iplan.dados.rio`: mudar valor lá não chega aos pods. O `InfisicalSecret mcp-secrets` sincroniza o projeto `mcp-hzj1`, path `/`, com resync de 60s. Em staging, a instância é `https://infisical-superapp-staging.squirrel-regulus.ts.net` (env `staging`). Prod segue o mesmo padrão; ver `iac-superapp/modules/deployments/infisical.tf`.
2. **Reiniciar o rollout.** O app só lê o ambiente no boot, e a anotação de auto-reload não reinicia o Rollout do Argo:

   ```bash
   kubectl argo rollouts restart mcp -n mcp --context gke_rj-superapp-staging_us-central1_application  # staging
   kubectl argo rollouts restart mcp -n mcp --context gke_rj-superapp_us-central1_application          # prod
   ```

3. **Conferir.** Cada busca grava a lista e os prazos efetivos no span da tool: `gemini.search.models`, `gemini.search.deadline_s` e `gemini.search.model_deadline_s`. Quem respondeu fica em `gemini.search.model_used` e `gemini.search.fallback_used`, e também nos campos `model`, `models_tried` e `fallback_used` do BigQuery (`rj-iplanrio.brutos_eai_logs.mcp`).

---

## Teste do fallback em staging

O QA do Salesforce chama o MCP de staging (`callout:MCPRioStaging`), então o teste ponta a ponta é feito conversando no WhatsApp de QA.

1. Em staging: `GEMINI_SEARCH_DEADLINE_SECONDS=100` (ou remover a chave) e `GEMINI_SEARCH_MODELS=gemini-inexistente,gemini-3.5-flash-lite`. Reiniciar o rollout.
2. No QA, fazer 2 ou 3 perguntas que caiam na busca web.
3. Esperado: resposta com fontes, vinda do 3.5, sem alerta no canal do interceptor; `fallback_used: true` e `models_tried` com o primeiro modelo marcado `gemini_client_error`.
4. Medir a latência e conferir que uma segunda busca no mesmo turno não estoura os 120s do Apex. No `Log_Event__c` do QA, uma falha aparece como o par `MCP_ERROR` + `ACTION_FAILED`.
5. Voltar `GEMINI_SEARCH_MODELS` ao default (remover a chave) e reiniciar o rollout.

---

## Rollback sem deploy

Para voltar a um comportamento próximo do anterior (um modelo só) sem reverter o código:

| Variável | Valor |
| --- | --- |
| `GEMINI_SEARCH_MODELS` | `gemini-2.5-flash-lite` |
| `GEMINI_SEARCH_MODEL_DEADLINE_SECONDS` | `35` |
| `GEMINI_SEARCH_DEADLINE_SECONDS` | `45` |

Diferença em relação ao código anterior: 2 tentativas por modelo em erro transitório, em vez de 4 (`GEMINI_SEARCH_ATTEMPTS_PER_MODEL` pode ir a `4` se necessário), e a resolução de URLs limitada a 10s.
