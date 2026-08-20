# Decisão: agrupamento de escritas e dead-letter recuperável no BigQuery

- **Jira**: [CHATR-103](https://iplanrio-pcrj.atlassian.net/browse/CHATR-103) · sub-tasks [CHATR-118](https://iplanrio-pcrj.atlassian.net/browse/CHATR-118) (batch) e [CHATR-126](https://iplanrio-pcrj.atlassian.net/browse/CHATR-126) (dead-letter)
- **Status**: Implementado
- **Data**: 2026-08-20
- **Relacionados**: CHATR-102 (orçamento de tempo das leituras), CHATR-114 (cache do client), [CHATR-119](health-checks-e-preflight.md) (health checks)
- **Escopo**: `src/utils/bigquery.py`, `src/utils/bq_dlq_replay.py` (novo), `src/utils/background.py` (novo), `src/config/env.py`, `src/app.py`, `src/health/checks.py`, `src/health/routes.py`, `src/tools/{search,feedback_tools,equipments_tools}.py`, `k8s/{prod,staging}/resources.yaml`, arquivos de teste.

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

### D10 — Os contadores saem em `/health/detail`

O critério de aceite do CHATR-118 é que o volume de inserts caia "de forma
mensurável". Os contadores existiam desde o batching, mas nada os lia: conferir
a taxa de agrupamento exigia abrir um REPL dentro do pod. Sem saída, o critério
não era verificável — e, pior, um buffer que parou de escoar também reduz
inserts, então "menos chamadas" sozinho não distingue ganho de regressão.

`bigquery_write` entra no corpo de `/health/detail`, ao lado dos checks:
`taxa_agrupamento` (`rows_written / insert_calls`) é o número que responde ao
critério, e `rows_buffered` diz o que está em memória naquele instante.

Fica fora do `health_registry` de propósito — o registry existe para check com
I/O e timeout próprio, e aqui não há chamada de rede nenhuma. Pelo mesmo motivo
o bloco nunca propaga exceção: ele é informativo e não pode ser a razão de a
rota de diagnóstico falhar.

A leitura roda na event loop, e é isso que obriga o lock do buffer a ter prazo
próprio (`_METRICS_LOCK_TIMEOUT_SECONDS`, 250ms, contra os 5s do resto do
módulo). O buffer nunca é segurado durante I/O, então a seção crítica é só CPU;
o teto curto garante que a rota não segure a event loop nem no pior caso. Sem o
lock, `rows_buffered` sai como `null` — e não `0`, que faria concluir que não há
linha parada.

### D11 — Alerta do COR de severidade alta ou crítica não passa pelo lote

É o trade-off que o CHATR-118 pede explicitamente. Agrupar é a escolha certa
para o volume, não para a emergência: um alerta de enchente podia ficar até
`BIGQUERY_FLUSH_INTERVAL_SECONDS` no buffer antes de existir em qualquer lugar
consultável — justamente o registro que alguém procura durante a ocorrência.

| Severidade | `cor_alerts` | `cor_alerts_queue` (despacho) |
|---|---|---|
| alta, crítica | direto | direto (já era) |
| baixa, média | em lote | não é enfileirado |

O volume está em baixa/média, e é lá que o batching entrega a redução de custo.
A comparação usa a severidade normalizada (sem acento, minúscula), porque o
valor chega da tool como texto livre.

### D12 — O fallback em arquivo ganha as mesmas proteções do Redis

O caminho em arquivo roda exatamente quando o Redis — a proteção principal —
está fora. Ele tinha três furos que o caminho no Redis já não tinha, e todos no
momento de maior fragilidade:

| Furo | Consequência | Fechado com |
|---|---|---|
| Item recusado por schema era pulado e sumia no rewrite final | Perda definitiva e silenciosa — o defeito que o CHATR-126 existe para eliminar | `dlq_<tabela>.poison.jsonl`, par do `bq_dlq_poison:` do Redis |
| Arquivo sem teto | Enche o disco do container durante indisponibilidade longa | `BIGQUERY_DLQ_MAX_ITEMS`, com `logger.critical` no corte |
| Nada expirava | Disco e, sobretudo, retenção indefinida de dado pessoal | `BIGQUERY_DLQ_TTL_SECONDS` sobre o `mtime` |

Sem variável de ambiente nova: são as mesmas duas do Redis, para não haver dois
modelos mentais para a mesma fila. O relógio do TTL é o `mtime`, que avança a
cada append — um arquivo que ainda recebe escrita não expira, igual à chave do
Redis, cujo `EXPIRE` é renovado a cada gravação.

O teto é conferido por bytes e aplicado por linhas. É custo: contar linhas exige
ler o arquivo inteiro, e este código roda no caminho de falha de escrita, que
pode estar sendo exercitado a cada requisição. Um `stat()` por append é barato;
a leitura completa só acontece quando o arquivo passa de
`BIGQUERY_DLQ_MAX_ITEMS * _DLQ_LINHA_MEDIA_BYTES`. Com linhas bem menores que a
média suposta o arquivo pode passar do teto em número de itens antes da primeira
conferência — mas aí ele é pequeno em bytes, que é o que ameaça o disco.

O arquivo de poison é ignorado pelo reprocessamento (`dlq_*.jsonl` casaria com o
nome dele): são as linhas que o BigQuery já recusou em definitivo, e cada
passagem as devolveria a ele para serem recusadas de novo.

### D13 — Escrita em background com referência forte

`asyncio.create_task` devolve a task, mas a event loop guarda dela apenas
referência **fraca**. Uma task criada e não guardada em lugar nenhum pode ser
coletada antes de terminar — sem erro, sem log e sem rastro.

Os seis call sites de log/feedback/alerta nos tools faziam exatamente isso.
Nenhuma das redes construídas aqui alcança essa perda: agrupamento, retry,
dead-letter e flush no encerramento só entram em ação depois que a corrotina
começa a rodar. Era a mesma perda de registro, um passo antes de toda a
proteção.

`src/utils/background.py` mantém a referência viva até o fim e a solta no
done-callback (segurar para sempre trocaria perda de dado por vazamento de
memória). Consumir o resultado ali serve a um segundo propósito: sem isso, uma
exceção só apareceria no destrutor, como "Task exception was never retrieved" —
sem contexto e fora de ordem.

O padrão já estava em `src/app.py`, nas tasks do lifespan; este módulo é a
versão reutilizável, para call sites que não têm um escopo longo onde segurar a
referência.

### D14 — `terminationGracePeriodSeconds` explícito

O flush de encerramento (D1, D2) depende desse prazo, mas ele estava no default
implícito de 30s. Estourá-lo significa SIGKILL no meio do flush — e aí não sobra
nem a DLQ, que é a rede que este trabalho inteiro construiu.

Agora são 60s declarados em `k8s/prod` e `k8s/staging`: folga sobre o pior caso
observável (4 tabelas × 5s de `BIGQUERY_SHUTDOWN_TIMEOUT_SECONDS`) sem atrasar
rollout, já que o valor é teto e não espera — o pod sai assim que termina.

### D15 — O poison também ganha volta

O poison era write-only. `_mover_para_poison` e `_mover_linha_para_poison`
escreviam; nada lia de volta — nem o worker de drain, nem o CLI. A única saída
era o TTL expirando, e no Redis isso acontece **sem deixar rastro**.

É o mesmo defeito que a D3 corrigiu para a DLQ principal ("persistir o payload
sem caminho de retorno só troca 'dado perdido' por 'dado parado numa lista que
ninguém lê"), reproduzido um nível abaixo — e com o agravante de que o critério
de aceite do CHATR-126, *payload fica disponível para reprocessamento manual ou
automático*, não era cumprido para esses itens.

São três operações, todas manuais e explícitas, porque as três exigem um
julgamento que nenhum worker pode fazer sozinho:

| Operação | Para quê | Cuidado |
|---|---|---|
| `--poison` | ler o erro do BigQuery e os campos do payload para diagnosticar | não consome, não altera TTL |
| `--requeue-poison` | devolver à DLQ normal depois de corrigir a causa | não valida a correção — se não foi feita, o drain recusa de novo |
| `--purge-poison` | descartar o que se concluiu ser irrecuperável | exige `--confirmar`; sai em `logger.critical` |

O reprocesso não valida se a causa foi de fato corrigida porque não tem como. Se
não foi, o drain recusa o item mais uma vez e ele volta ao poison: o laço é
finito, cada passagem fica no log, e o custo de errar é uma tentativa perdida —
muito menor que o de não haver caminho de volta nenhum.

**O conteúdo do payload não sai por padrão.** A saída do CLI vai para o terminal
do operador e, com frequência, para o scrollback ou para o log de um job;
despejar `user_id` (telefone), endereço e coordenada ali seria tirar o dado
justamente dos lugares onde ele é controlado — Redis com TTL, tabela do
BigQuery. O que sai é o que resolve um erro de schema: a mensagem do BigQuery,
que nomeia o campo recusado, e a lista de campos presentes. Nome de campo é
estrutura, não dado da pessoa. O conteúdo exige `--mostrar-payload`.

### D16 — Poison degrada a partir do primeiro item, agora que há saída

A degradação a partir de 1 item só passou a ser defensável depois da D15. Antes,
"degradado" significava esperar sete dias de TTL, e um único payload malformado
mascarava toda outra degradação no agregado por uma semana — um check
permanentemente vermelho é um check que ninguém lê.

Agora significa "rode `bq_dlq_replay --poison`", e some quando alguém reprocessa
ou descarta. Para que agir não exija investigação prévia, a mensagem carrega os
três dados que o operador teria de descobrir sozinho: a tabela afetada, o prazo
até o TTL apagar o payload e o comando que resolve.

O prazo tem um segundo papel: a expiração no Redis não deixa rastro nenhum.
Sem ele exposto em `get_dlq_depth`, o operador só descobriria o TTL depois de
ele ter vencido — ou seja, depois de o dado já ter sumido.

Alternativas descartadas: **limiar configurável**, que só move o número em que o
mascaramento começa e silencia o caso de um payload isolado — que é exatamente o
que se quer ver; e **tirar o poison do agregado**, que elimina o mascaramento
mas devolve o poison à condição de "aparece só numa linha de log que ninguém
revisita", o problema que o check existe para resolver.

## Limitação conhecida

O fallback em arquivo da DLQ (`DATA_DIR/bq_dlq/*.jsonl`) grava numa camada efêmera: não há volume montado no `k8s/`. No cenário exato em que ele é acionado — Redis fora —, um restart do pod leva o arquivo junto. Ele segue valendo como rede de segurança dentro da vida do pod (o worker de drain o reprocessa), mas **não** é armazenamento durável. Tornar durável exige um PVC, que é decisão de infraestrutura e ficou fora deste escopo.

O TTL de D12 limita por quanto tempo o payload fica ali, o que resolve a retenção de dado pessoal mesmo sem volume durável — mas não torna o arquivo sobrevivente a restart.

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

Quando o check apontar item em **poison** — payload que o BigQuery recusou em
definitivo, e que o drain automático nunca reprocessa porque repeti-lo não muda
o desfecho:

```bash
# 1. diagnosticar: erro do BigQuery e campos do payload (conteúdo omitido)
kubectl exec -it deploy/mcp -- uv run python -m src.utils.bq_dlq_replay --poison

# 2a. corrigida a causa (schema ajustado, coluna criada), devolver à DLQ
kubectl exec -it deploy/mcp -- uv run python -m src.utils.bq_dlq_replay \
  --requeue-poison --table rj-iplanrio.brutos_eai_logs.feedback

# 2b. ou, se for irrecuperável, descartar (irreversível, exige --confirmar)
kubectl exec -it deploy/mcp -- uv run python -m src.utils.bq_dlq_replay \
  --purge-poison --table rj-iplanrio.brutos_eai_logs.feedback --confirmar
```

`--mostrar-payload` inclui o conteúdo na listagem. É opt-in porque o payload
carrega dado pessoal; use só quando o erro do BigQuery não bastar.

`check_bigquery_dlq` degrada `/health/detail` quando há item pendente, e não é `critical`: item na DLQ significa log atrasado, não servidor incapaz de atender — derrubar o pod por isso trocaria perda de log por indisponibilidade da aplicação. Item em poison degrada a partir do primeiro e só sai com uma das ações acima — ver D16.
