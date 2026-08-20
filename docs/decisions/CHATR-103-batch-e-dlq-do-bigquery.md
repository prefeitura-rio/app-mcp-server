# Decisão: agrupamento de escritas e dead-letter recuperável no BigQuery

- **Jira**: [CHATR-103](https://iplanrio-pcrj.atlassian.net/browse/CHATR-103) · sub-tasks [CHATR-118](https://iplanrio-pcrj.atlassian.net/browse/CHATR-118) (batch) e [CHATR-126](https://iplanrio-pcrj.atlassian.net/browse/CHATR-126) (dead-letter)
- **Status**: Implementado
- **Data**: 2026-08-20
- **Relacionados**: CHATR-102 (orçamento de tempo das leituras), CHATR-114 (cache do client), [CHATR-119](health-checks-e-preflight.md) (health checks)
- **Escopo**: `src/utils/bigquery.py`, `src/utils/bq_dlq_replay.py` (novo), `src/config/env.py`, `src/app.py`, `src/health/checks.py`, 3 arquivos de teste.

## Problema

A primeira metade do CHATR-103 já estava em `main` (commit `e8cf883`): buffer por tabela, flush por tamanho e por tempo, client do BigQuery como singleton, retry com backoff e persistência em DLQ. O que sobrou não eram detalhes — eram os dois pontos em que o objetivo da história ("parar de perder registro em silêncio") continuava não sendo atingido.

1. **O buffer se perdia a cada deploy.** A única rede era `atexit`. Mas `src/app.py` já documentava que a uvicorn restaura o handler original do sinal e faz `signal.raise_signal()` ao sair de `serve()` — com o handler default, o processo morre ali, sem passar por `atexit`. Cada rollout levava junto até um lote inteiro por tabela mais o que tivesse acumulado desde o último flush periódico. A perda tinha apenas mudado de lugar.
2. **A DLQ não tinha volta.** Nada lia o que era gravado nela. O critério de aceite do CHATR-126 é "payload fica disponível para reprocessamento manual ou automático"; sem leitor, o dado saía de "perdido em silêncio" para "parado numa lista que ninguém sabe que existe".

Somavam-se três riscos menores: o buffer podia crescer sem teto, a DLQ no Redis podia crescer sem teto (na mesma instância do cache de queries), e as escritas ainda usavam o executor default — justamente o problema que motivou o pool dedicado de leitura no CHATR-102.

## Decisões

### D1 — Flush em handler de sinal, instalado antes da uvicorn

O handler de SIGTERM/SIGINT é instalado no import de `src/utils/bigquery.py`, que acontece antes de `mcp.run()`. A uvicorn o captura como "handler anterior", o restaura ao sair de `serve()` e o re-levanta — e é aí que o flush roda. Depois, delegamos ao handler que estava instalado antes de nós, preservando o código de saída original.

Alternativa descartada: **mover o buffer para o Redis**. Daria durabilidade também contra SIGKILL, mas colocaria um round-trip de rede por linha no caminho de escrita e faria o Redis virar dependência dele — perdendo boa parte do ganho que o batching existe para obter. Com `replicas: 1` e rollout via Argo, SIGTERM é o caso real; SIGKILL só acontece se o pod estourar o `terminationGracePeriod`, e para isso o flush de encerramento usa `max_retries=1`.

**O que continua exposto**: SIGKILL e OOMKill perdem o que estiver em memória (no máximo `BIGQUERY_BATCH_SIZE - 1` linhas por tabela). É aceito conscientemente.

### D2 — O flush de encerramento troca insistência por prazo

`max_retries=1`, `initial_delay=0` e `timeout=BIGQUERY_SHUTDOWN_TIMEOUT_SECONDS`. O orçamento no shutdown não é "conseguir escrever", é "não estourar o `terminationGracePeriod`": uma tentativa que falha cai direto na DLQ, que é recuperável. Insistir com backoff arriscaria o SIGKILL no meio do caminho — e aí não sobraria nem a DLQ.

O teto de tempo por chamada fecha o outro lado do mesmo risco: sem ele, `insert_rows_json` herda o default do transporte e pode não voltar — pendurando o handler de sinal até o Kubernetes mandar SIGKILL, que é justamente a perda que a DLQ existiria para evitar.

Pelo mesmo motivo, o lock do buffer é adquirido com prazo (`_BUFFER_LOCK_TIMEOUT_SECONDS`), nunca indefinidamente: o flush final roda na thread principal, dentro do handler de sinal, e um `acquire()` sem teto ali converteria contenção momentânea em pod que não termina.

### D3 — Reprocessamento automático **e** manual

| | Worker `drain_bigquery_dlq_loop` | CLI `python -m src.utils.bq_dlq_replay` |
|---|---|---|
| Quando | task do lifespan, a cada `BIGQUERY_DLQ_DRAIN_INTERVAL_SECONDS` | sob demanda, no pod |
| Para que | recuperar sozinho quando o BigQuery volta | conferir antes de mexer (`--dry-run`), reprocessar após corrigir a causa |
| Diante de erro transitório | para e tenta na próxima varredura | reporta e sai com código 1 |

O worker é conservador de propósito: reprocessar de minuto em minuto enquanto o BigQuery está fora só empilharia falha sobre falha. Isso resolve o caso comum sem intervenção, mas deixa dois casos para gente — e é o que o CLI cobre.

Todo o trabalho bloqueante (Redis síncrono, insert, leitura de arquivo) vai para o pool de escrita. A event loop nunca fica presa numa varredura, mesmo com a DLQ cheia.

### D4 — Entrega ao menos uma vez, nunca no máximo uma vez

O item só sai da DLQ **depois** de o BigQuery confirmar a escrita (`LRANGE` → insert → `LPOP`, não `LPOP` → insert). Uma queda entre a confirmação e a remoção reprocessa o item, ou seja, duplica.

É a troca deliberada: para log, feedback e alerta, registro duplicado é um incômodo de análise; registro perdido é o defeito que esta história inteira existe para eliminar.

Um lock no Redis (`SET NX EX`) serializa o drain entre réplicas — dois drenos simultâneos duplicariam sem necessidade. O lock é best-effort: Redis que não responde ao `SET NX` faz o drain pular a varredura, nunca travar.

### D5 — Payload recusado para sempre sai da frente

Erro 4xx do BigQuery (fora 429) e linha recusada por schema não melhoram com o tempo. Sem tratar isso, um único payload malformado na cabeça da fila bloquearia para sempre todos os que chegaram depois dele.

Esses itens vão para uma chave `bq_dlq_poison:<tabela>` — separados, não descartados —, com o mesmo teto e a mesma validade. `check_bigquery_dlq` distingue os dois casos: pendência comum tende a zerar sozinha; poison exige correção manual, e a mensagem diz isso.

A distinção usa `BigQueryRowRejectedError` (linha recusada por conteúdo) e `ClientError`/`TooManyRequests` do `google.api_core`. Ela também poupa retry inútil no caminho de escrita corrente.

### D6 — Teto e validade em toda fila, com o descarte visível

| Fila | Teto | Validade |
|---|---|---|
| Buffer em memória | `BIGQUERY_BATCH_MAX_BUFFERED_ROWS` (10000) — excedente vai para a DLQ | — |
| DLQ e poison no Redis | `BIGQUERY_DLQ_MAX_ITEMS` (1000), via `LTRIM` | `BIGQUERY_DLQ_TTL_SECONDS` (7 dias) |

O teto do buffer protege o pod: o limite de memória do container é 1536Mi, e um buffer sem teto transformaria "log parado" em OOMKill do servidor inteiro. O excedente não é descartado — vai para a DLQ.

O teto da DLQ protege o **cache**: a DLQ divide instância de Redis com o cache de queries do BigQuery, e sem teto uma indisponibilidade longa viraria falha de escrita derrubando a leitura junto.

Quando o `LTRIM` de fato descarta, sai um `logger.critical` dizendo quantos itens se perderam. Descarte de DLQ é perda definitiva de dado e não pode passar como aviso de rotina.

A validade tem um segundo papel: o payload carrega dado pessoal (`user_id` é telefone; alerta do COR tem endereço e coordenada). O TTL é o que limita essa retenção — o relógio conta a partir da última gravação na chave, não por item.

### D7 — Pool de escrita dedicado

Espelha o `bq-read` do CHATR-102, pelo motivo inverso: o retry dorme entre as tentativas, então uma indisponibilidade do BigQuery segurava cada thread por até ~1,5s **no executor default**, que é o mesmo de qualquer outra chamada bloqueante do app. `BIGQUERY_WRITE_MAX_WORKERS` (4) é pequeno de propósito — escrita de log é assíncrona ao usuário e não precisa de paralelismo alto.

### D8 — O span deixa de mentir sobre durabilidade

Antes, `save_response_in_bq` marcava `success = True` e `row_count = 1` no instante do **enfileiramento**: o trace afirmava escrita bem-sucedida de uma linha que ainda podia terminar na DLQ. Agora cada span carrega `bigquery.write_mode` (`batched`/`direct`) e `bigquery.durable` — em modo `batched`, retornar sem erro significa "aceita no buffer", não "gravada".

`get_bigquery_write_metrics()` expõe os contadores que faltavam para o critério de aceite "o volume de inserts cai de forma mensurável": `rows_enqueued / insert_calls` é a taxa de agrupamento efetiva, e a diferença entre `rows_enqueued` e `rows_written + rows_to_dlq` é o que está em memória naquele instante.

### D9 — Nome de tabela sanitizado antes de virar caminho ou chave

Nomes de tabela vêm de constantes internas, nunca de entrada de usuário. Mas o nome vira nome de arquivo e chave de Redis, e uma travessia de diretório ali seria escrita arbitrária no container. A allowlist (`[A-Za-z0-9_.\-]`) custa nada e fecha a categoria inteira, sem depender de a origem continuar confiável no futuro.

## Limitação conhecida

O fallback em arquivo da DLQ (`DATA_DIR/bq_dlq/*.jsonl`) grava numa camada efêmera: não há volume montado no `k8s/`. No cenário exato em que ele é acionado — Redis fora —, um restart do pod leva o arquivo junto. Ele segue valendo como rede de segurança dentro da vida do pod (o worker de drain o reprocessa), mas **não** é armazenamento durável. Tornar durável exige um PVC, que é decisão de infraestrutura e ficou fora deste escopo.

Para reduzir a janela, o `replay_dlq_arquivos` renomeia o arquivo para `.processing` antes de lê-lo: assim `_persist_to_dlq` segue acrescentando ao nome original e o reprocessamento não apaga o que chegou durante a varredura. Sobras de uma execução interrompida são adotadas na varredura seguinte.

## Configuração

Todas as variáveis têm default e `action="ignore"` — nenhuma entra em `REQUIRED_ENV_VARS`, e o comportamento sem configurá-las é o desejado em produção.

| Variável | Default | Papel |
|---|---|---|
| `BIGQUERY_BATCH_SIZE` | 50 | linhas por lote |
| `BIGQUERY_FLUSH_INTERVAL_SECONDS` | 30 | flush periódico (antes era constante no código) |
| `BIGQUERY_BATCH_MAX_BUFFERED_ROWS` | 10000 | teto do buffer em memória |
| `BIGQUERY_WRITE_MAX_WORKERS` | 4 | tamanho do pool `bq-write` |
| `BIGQUERY_WRITE_TIMEOUT_SECONDS` | 10.0 | teto por chamada de insert |
| `BIGQUERY_SHUTDOWN_TIMEOUT_SECONDS` | 5.0 | teto por chamada no flush de encerramento |
| `BIGQUERY_DLQ_MAX_ITEMS` | 1000 | teto por chave da DLQ |
| `BIGQUERY_DLQ_TTL_SECONDS` | 604800 | validade das chaves da DLQ |
| `BIGQUERY_DLQ_DRAIN_INTERVAL_SECONDS` | 300 | intervalo do worker de drain |
| `BIGQUERY_DLQ_DRAIN_BATCH` | 100 | itens por varredura |
| `BIGQUERY_DLQ_DRAIN_ENABLED` | true | desliga o drain automático |

## Operação

```bash
# o que está parado, sem consumir a fila
kubectl exec -it deploy/mcp -- uv run python -m src.utils.bq_dlq_replay --dry-run

# só a profundidade (também aparece em /health/detail, check `bigquery_dlq`)
kubectl exec -it deploy/mcp -- uv run python -m src.utils.bq_dlq_replay --depth-only

# reprocessar após corrigir a causa
kubectl exec -it deploy/mcp -- uv run python -m src.utils.bq_dlq_replay --limit 500
```

`check_bigquery_dlq` degrada `/health/detail` quando há item pendente, e não é `critical`: item na DLQ significa log atrasado, não servidor incapaz de atender — derrubar o pod por isso trocaria perda de log por indisponibilidade da aplicação.
