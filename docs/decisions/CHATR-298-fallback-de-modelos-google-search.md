# Fallback de modelos no google_search: configuração e operação

**Jira:** [CHATR-298](https://iplanrio-pcrj.atlassian.net/browse/CHATR-298)
**PR:** [#193](https://github.com/prefeitura-rio/app-mcp-server/pull/193)
**Data:** 22 de outubro de 2026
**Atualização:** 26 de outubro de 2026, [CHATR-314](https://iplanrio-pcrj.atlassian.net/browse/CHATR-314): raciocínio de 1.024 no 2.5, 1 consulta de grounding no 3.5 e o campo `max_search_queries`.
**Incidente de origem:** 21/10/2026, das 13h08 às 13h43. O `gemini-2.5-flash-lite` ficou sem capacidade no Google (503 "high demand" e chamadas travadas até o prazo), e 27 de 39 buscas falharam na hora das 13h.

---

## Resumo

A busca percorre uma lista de modelos em ordem de prioridade. Cada modelo tem o seu prazo e os seus parâmetros de invocação. Um erro transitório (503, 429, 5xx) ganha uma nova tentativa curta no mesmo modelo; qualquer outra falha (prazo do modelo, `RECITATION`, `MAX_TOKENS`, resposta vazia, 4xx) passa direto para o próximo. Só quando todos falham a busca vira falha técnica e gera alerta.

A lista inteira fica numa variável de ambiente, `GEMINI_SEARCH_MODELS`, em JSON, para que modelos, ordem e parâmetros mudem **sem deploy**. Sem a variável, vale a configuração de produção do código.

---

## `GEMINI_SEARCH_MODELS`

Lista JSON, um objeto por modelo, na ordem em que serão tentados. Lida e validada no boot por [`src/config/gemini_search.py`](../../src/config/gemini_search.py).

```json
[
  {"model": "gemini-2.5-flash-lite", "timeout_s": 45, "thinking_budget": 1024},
  {"model": "gemini-3.5-flash-lite", "timeout_s": 20, "thinking_level": "MINIMAL", "max_search_queries": 1}
]
```

Só `model` é obrigatório. Campo omitido vale o default do campo:

| Campo | Default | Faixa aceita | O que controla |
| --- | --- | --- | --- |
| `model` | (obrigatório) | texto não vazio, sem repetir na lista | Nome do modelo na API do Gemini |
| `timeout_s` | `45` | 5 a 95 | Prazo do modelo, somando as tentativas e a espera entre elas |
| `attempts` | `2` | 1 a 4 | Chamadas no mesmo modelo em erro transitório (1 + retries) |
| `thinking_budget` | `1024` nos `gemini-2.*` (era `8192` até o CHATR-314) | ≥ 0 (`0` desliga) | Raciocínio dos 2.x, em tokens. Só nos `gemini-2.*` |
| `thinking_level` | `MINIMAL` nos demais | `MINIMAL`, `LOW`, `MEDIUM`, `HIGH` | Raciocínio dos 3.x. Não vale nos `gemini-2.*` |
| `max_output_tokens` | `12288` | ≥ 1 | Teto de saída. Nos 2.x precisa ser ≥ `thinking_budget + 2048` |
| `temperature` | `0` | 0 a 2 | Temperatura da geração |
| `max_search_queries` | ausente (sem limite) | inteiro ≥ 1 | Consultas de grounding pedidas no prompt. Ausente: "at least 5 different queries" (o texto de sempre); `1`: "exactly 1 query"; `n > 1`: "at most n different queries" |

Regras da lista:

- Ao menos um modelo, sem nomes repetidos.
- Campo desconhecido é erro (pega erro de digitação como `"timout_s"`).
- `thinking_budget` e `thinking_level` não vão juntos. Omitidos os dois, vale o default da geração do modelo.
- **Prazo total = soma dos `timeout_s` + 10s** de resolução de URLs, e não pode passar de **105s**. O limite real do Salesforce é o teto de 120s de callout por transação Apex: o Apex vê cerca de 6s a mais que o prazo daqui, e a transação ainda faz `initialize` antes do `tools/call`. O `setTimeout` de 60s do `HttpToolHelperBase` não dispara, porque os pings SSE a cada 15s zeram o timeout de leitura.

### Configuração de produção (default do código)

| Ordem | Modelo | `timeout_s` | `attempts` | Raciocínio | Consultas (`max_search_queries`) | `max_output_tokens` | `temperature` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `gemini-2.5-flash-lite` | 45 | 2 | `thinking_budget=1024` | sem limite (≥ 5) | 12288 | 0 |
| 2 | `gemini-3.5-flash-lite` | 20 | 2 | `thinking_level=MINIMAL` | `1` | 12288 | 0 |

Prazo total: 45 + 20 + 10 = **75s**. O 45s do 2.5 cobre o p99 das chamadas bem-sucedidas (27,6s em out/2026, ainda com raciocínio de 8.192). O 3.5 fica com 20s porque, na bateria do CHATR-298, teve p95 de 6,4s e máximo de 9,3s. Assim o pior caso de uma busca deixa folga para uma segunda busca no mesmo turno.

**Raciocínio e consultas ([CHATR-314](https://iplanrio-pcrj.atlassian.net/browse/CHATR-314)).** Bateria de 26/10/2026: 60 perguntas, mesmo juiz, mensagem final simulada; referência = produção até então (2.5, ≥ 5 consultas, raciocínio de 8.192).

| Configuração | p50 | p95 | Sem fonte | Mensagem final: completa / parcial / insuficiente | Custo/mês estimado |
| --- | --- | --- | --- | --- | --- |
| 2.5, raciocínio 8.192 (produção rodada de novo, ruído) | 10,6s | 18,0s | 9/60 | 20 / 38 / 2 | US$ 88 |
| 2.5, raciocínio 1.024 | 9,8s | 12,9s | 9/60 | 13 / 46 / 1 | US$ 81 |
| 2.5, raciocínio 0 | 6,9s | 9,4s | 7/60 | 11 / 39 / 10 | US$ 75 |
| 3.5, 1 consulta, `MINIMAL` | 4,6s | 6,7s | 3/60 | 16 / 41 / 3 | US$ 217 |
| 3.5, consultas livres, `MINIMAL` | 5,9s | 7,7s | 2/60 | 22 / 36 / 2 | US$ 848 |

- 2.5 com 1.024: p95 de 23,5s (produção na primeira rodada) para 12,9s, com qualidade dentro do ruído. Com `0`, as insuficientes vão a 10.
- 3.5 com 1 consulta: qualidade dentro do ruído da produção, metade da latência do 2.5 e menos "sem fonte" (3 contra 9). Com consultas livres, a qualidade é a melhor, mas o grounding dos 3.x é cobrado por consulta e o custo quase quadruplica. Como fallback, só responde quando o 2.5 falha.
- Custo/mês: como se a configuração respondesse todas as ~18 mil buscas de outubro.

O prompt de cada modelo (tudo menos a data e a pergunta) é montado uma vez, no boot. A data do dia e a pergunta entram a cada chamada.

### Comportamento no boot

| Valor da variável | Resultado |
| --- | --- |
| Ausente ou vazia | Sobe com a configuração de produção |
| JSON válido | Sobe com a lista da variável |
| Inválido (JSON quebrado, campo errado, valor fora da faixa, prazo acima do teto) | **O pod não sobe.** O preflight registra no log cada problema, com o item e o campo |

O valor inválido não cai no default de propósito: quem preencheu quis mudar algo, e seguir em silêncio com outra configuração esconderia o erro. Com o canary do Argo Rollouts (`maxUnavailable: 0`), o pod novo não fica pronto e os pods estáveis continuam atendendo.

Exemplo do log de erro:

```
Preflight de configuração falhou com 2 erro(s). A aplicação NÃO será iniciada:
  - GEMINI_SEARCH_MODELS[0] (gemini-2.5-flash-lite).timout_s: campo desconhecido (recebido: 45)
  - GEMINI_SEARCH_MODELS[1].model: campo obrigatório ausente
```

Com a configuração válida, o boot registra a configuração efetiva e a origem dela:

```
Modelos da busca web (origem: env): gemini-2.5-flash-lite(45s, 2x, budget=1024, max_output=12288, temp=0) → gemini-3.5-flash-lite(20s, 2x, level=MINIMAL, max_output=12288, temp=0, queries=1); prazo total 75s
```

---

## Outras variáveis

Continuam globais, porque são política da busca e não de um modelo:

| Variável | Default | O que controla |
| --- | --- | --- |
| `GEMINI_SEARCH_RETRY_BASE_SECONDS` | `2` | Base do backoff do retry: espera entre 1 e 2s |
| `GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS` | `16` | Teto da espera, inclusive quando a API informa `retryDelay` |

Variáveis **removidas do código**. Se ainda existirem no Infisical, são ignoradas, e o boot registra um warning para cada uma:

| Variável | Substituída por |
| --- | --- |
| `GEMINI_MODEL` | `model` do primeiro item |
| `GEMINI_SEARCH_DEADLINE_SECONDS` | Prazo total calculado (soma dos `timeout_s` + 10s) |
| `GEMINI_SEARCH_MODEL_DEADLINE_SECONDS` | `timeout_s` |
| `GEMINI_SEARCH_ATTEMPTS_PER_MODEL` / `GEMINI_SEARCH_RETRY_ATTEMPTS` | `attempts` |
| `GEMINI_SEARCH_THINKING_LEVEL` | `thinking_level` |
| `GEMINI_SEARCH_REASONING_BUDGET_TOKENS` | `thinking_budget` |
| `GEMINI_SEARCH_MAX_OUTPUT_TOKENS` | `max_output_tokens` |

Mudança de comportamento: antes, um valor inválido nessas variáveis virava o default com warning (`thinking_budget=-1`, por exemplo). Agora, um valor inválido na lista derruba o boot.

---

## Estado por ambiente

Levantado em 22/10/2026 nas chaves do secret `mcp-secrets` (namespace `mcp`), que o operador do Infisical sincroniza a partir do Infisical do próprio cluster.

| Variável | Staging | Prod | Ação |
| --- | --- | --- | --- |
| `GEMINI_SEARCH_MODELS` | não definida | não definida | Nenhuma: vale a configuração de produção |
| `GEMINI_SEARCH_DEADLINE_SECONDS` | `45` | não definida | Nenhuma. Ignorada; pode ser apagada de staging |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | `gemini-2.5-flash-lite` | Nenhuma. Ignorada; pode ser apagada depois |

**Este PR não exige mudança de variável em prod nem em staging.**

### Depois do CHATR-314

Prod já tem `GEMINI_SEARCH_MODELS` (conferido no Infisical em 26/10/2026):

```json
[{"model": "gemini-2.5-flash-lite", "timeout_s": 35}, {"model": "gemini-3.5-flash-lite", "timeout_s": 20}]
```

Com o código novo e essa variável intacta, o 2.5 herda o raciocínio de 1.024 do default do campo, mas o 3.5 **não** ganha a consulta única: o campo fica ausente e o prompt pede "ao menos 5". Depois do deploy, atualizar para:

```json
[{"model": "gemini-2.5-flash-lite", "timeout_s": 35, "thinking_budget": 1024}, {"model": "gemini-3.5-flash-lite", "timeout_s": 20, "thinking_level": "MINIMAL", "max_search_queries": 1}]
```

**Só depois do deploy:** o código anterior recusa campo desconhecido (`extra="forbid"`), e um `max_search_queries` na variável derrubaria o boot dos pods antigos.

Staging não foi conferido no CHATR-314.

Uma a duas semanas depois do deploy, medir nos spans o p99 das chamadas bem-sucedidas do 2.5 e recalibrar o `timeout_s` dele pela variável (candidato: 20 a 25s). O prazo atual foi calibrado com raciocínio de 8.192; com 1.024, o p95 na bateria foi 12,9s, mas 60 perguntas não estimam p99. Cada corte custa uma chamada ao 3.5 com 1 consulta.

---

## Quando prod vai precisar de mudança

| Situação | O que mudar em `GEMINI_SEARCH_MODELS` |
| --- | --- |
| Promover um 3.x a modelo principal ([CHATR-302](https://iplanrio-pcrj.atlassian.net/browse/CHATR-302)) | A ordem da lista. Atenção ao custo: nos 3.x o grounding é cobrado por query de busca (5.000 grátis por mês, depois US$ 14/1.000) |
| O Google desligar ou bloquear o 2.5 | Tirar o 2.5 da lista. Sem isso a busca continua funcionando: o 4xx do 2.5 passa direto para o próximo, mas cada busca paga uma chamada perdida |
| Incidente prolongado num modelo específico | Inverter a ordem até o modelo voltar. Evita que toda busca espere o modelo degradado antes do fallback |
| Adicionar um terceiro modelo | Acrescentar o item, ajustando os `timeout_s` para a soma + 10s caber em 105s. Exemplo: 45 + 20 + 20 + 10 = 95s |

---

## Como alterar

1. **Montar o JSON numa linha só**, com aspas retas (`"`). Para conferir antes, rode a validação localmente:

   ```bash
   uv run python -c 'from src.config.gemini_search import parse_search_config as p; print(p(open("/dev/stdin").read())[0].resumo())' <<< '<json>'
   ```

2. **Editar no Infisical do cluster**, não no `infisical.iplan.dados.rio`: mudar valor lá não chega aos pods. O `InfisicalSecret mcp-secrets` sincroniza o projeto `mcp-hzj1`, path `/`, com resync de 60s. Em staging, a instância é `https://infisical-superapp-staging.squirrel-regulus.ts.net` (env `staging`). Prod segue o mesmo padrão; ver `iac-superapp/modules/deployments/infisical.tf`.
3. **Reiniciar o rollout.** O app só lê o ambiente no boot, e a anotação de auto-reload não reinicia o Rollout do Argo:

   ```bash
   kubectl argo rollouts restart mcp -n mcp --context gke_rj-superapp-staging_us-central1_application  # staging
   kubectl argo rollouts restart mcp -n mcp --context gke_rj-superapp_us-central1_application          # prod
   ```

4. **Conferir.**
   - No log do boot, a linha `Modelos da busca web (origem: env)`.
   - Se o pod novo não ficar pronto, o log do preflight lista o problema. Corrigir o valor no Infisical e reiniciar de novo.
   - Em cada busca, o span da tool traz `gemini.search.models` e `gemini.search.deadline_s`. Cada chamada (`gemini.generate_content`) traz `gemini.model`, `gemini.model_deadline_s`, `gemini.max_output_tokens`, `gemini.reasoning_budget_tokens` ou `gemini.thinking_level` e, quando o modelo tem limite, `gemini.search.max_queries`.
   - Quem respondeu fica em `gemini.search.model_used` e `gemini.search.fallback_used`, e também nos campos `model`, `models_tried`, `fallback_used` e `temperature` do BigQuery (`rj-iplanrio.brutos_eai_logs.mcp`).

---

## Teste do fallback em staging

O QA do Salesforce chama o MCP de staging (`callout:MCPRioStaging`), então o teste ponta a ponta é feito conversando no WhatsApp de QA.

1. Em staging, definir:

   ```json
   [{"model": "gemini-inexistente", "timeout_s": 20}, {"model": "gemini-3.5-flash-lite", "timeout_s": 20}]
   ```

   Reiniciar o rollout.
2. No QA, fazer 2 ou 3 perguntas que caiam na busca web, e ao menos um turno com duas buscas.
3. Esperado: resposta com fontes, vinda do 3.5, sem alerta no canal do interceptor; `fallback_used: true` e `models_tried` com o primeiro modelo marcado `gemini_client_error`.
4. Medir a latência e conferir que a soma das duas buscas do turno fica abaixo dos 120s do Apex. No `Log_Event__c` do QA, uma falha aparece como o par `MCP_ERROR` + `ACTION_FAILED`.
5. Apagar a chave `GEMINI_SEARCH_MODELS` e reiniciar o rollout.

Para forçar a falha técnica de toda busca (teste do caminho de erro no Salesforce), use `[{"model": "gemini-inexistente"}]`.

---

## Rollback sem deploy

Para voltar ao comportamento anterior ao CHATR-298 (um modelo só, prazo total de 45s, 4 tentativas) sem reverter o código:

```json
[{"model": "gemini-2.5-flash-lite", "timeout_s": 35, "attempts": 4, "thinking_budget": 8192}]
```

Diferença em relação ao código anterior: a resolução de URLs fica limitada a 10s.

Para desfazer só o CHATR-314 (raciocínio de 8.192 no 2.5 e consultas livres no 3.5):

```json
[{"model": "gemini-2.5-flash-lite", "timeout_s": 35, "thinking_budget": 8192}, {"model": "gemini-3.5-flash-lite", "timeout_s": 20}]
```
