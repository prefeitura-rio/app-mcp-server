# Redação de PII vira barreira global de log

**Status:** implementado (bloco A — causa raiz)
**Data:** 2026-08-26
**Tipo:** Bug (LGPD) — causa raiz do CHATR-167

Registra a decisão de tratar PII no log como **barreira de infraestrutura**, e
não como responsabilidade de cada `logger.*`.

---

## Contexto

O relato do CHATR-167 era uma linha: `src/tools/divida_ativa.py:143` loga a
resposta inteira da PGM, com CPF do cidadão e o registro completo da guia, e
isso está indexado no SigNoz.

A auditoria mostrou que não era um caso isolado. Varrendo por AST as **433
chamadas de `logger.*`** fora de `src/tests/`, **62** interpolam algum termo de
risco. E o motivo é o mesmo para todas:

> A redação existia em duas ilhas isoladas — `src/utils/error_interceptor.py`
> (CHATR-113) e `src/utils/http_client.py` (CHATR-176) — e ambas só sanitizavam
> o payload **enviado ao interceptor de erros**. Nenhuma tocava a saída de
> `logger.*`. Como `src/utils/log.py` não instalava sink, patcher nem filtro,
> não é que a linha 143 escapava da redação: **nenhuma linha passava por ela**.

### Dois vazamentos que não estavam na lista dos 38 pontos

1. **`diagnose=True`.** O handler default do loguru anota, em todo traceback, o
   valor das variáveis de cada frame:

   ```
   consultar(cpf_do_cidadao, chave)
   │         │               └ 'SEGREDO-OAUTH'
   │         └ '98765432100'
   ```

   Isso não é um call site: é a árvore de chamada inteira. Qualquer exceção
   dentro de `pgm_api` ou de um nó de workflow despejava CPF e credencial.

2. **`LOG_LEVEL` era lido e nunca aplicado** (`src/config/settings.py:24`). O
   handler default do loguru é DEBUG, então tudo que o ticket classificou como
   "está em `logger.debug`, tem menos alcance" — `logger.debug(dados_imovel)`
   entre eles — estava saindo em staging e produção.

## Decisão

### 1. Uma fonte de verdade: `src/utils/pii.py`

Os padrões, as chaves sensíveis e as máscaras passam a viver em um módulo só,
consumido pelos três destinos: a barreira de log, o interceptor de erros e o
cliente HTTP. Só stdlib e nenhum import de `src/` — o módulo precisa ficar
embaixo dos outros dois na ordem de import e é carregado antes do preflight.

As máscaras não são novas: `mascarar_nome`, `mascarar_cpf` e `mascarar_email`
estavam inline em `poda_de_arvore/workflow.py:694-714`, e
`mascarar_ultimos_quatro` era o `_mask_last_four_digits` do interceptor.

### 2. Duas camadas na barreira, porque uma não alcança tudo

| Camada | Onde | O que só ela pega |
| --- | --- | --- |
| `patcher` | antes de **todo** sink | mensagem ainda **estruturada** — `logger.info({...})` chega com o dict de pé, e é a única janela para redigir pela chave |
| `sink` | texto já formatado | `str(exception)` e o traceback, que não existem antes da formatação |

O sink próprio também é o que desliga `diagnose`/`backtrace` e aplica
`LOG_LEVEL`.

**Consequência a conhecer:** um sink adicionado fora de `src/utils/log.py` (um
teste, ou um exportador OTLP de logs no futuro) recebe a camada 1, não a 2 — o
traceback chegaria nele sem redigir.

Módulos que fazem `from loguru import logger` direto continuam cobertos: o
`logger` é singleton e a configuração é global. Foi por isso que **nenhum dos 18
arquivos precisou trocar de import**.

### 3. Nome e endereço não entram como regex — a chave decide

Não existe padrão confiável para nome próprio em texto solto: uma regex que
tentasse ou destruiria mensagem legítima ou não pegaria nada. A cobertura desses
vem de **olhar a chave, não o valor** — `redigir_estrutura` para dict de pé e
`redigir_chaves_sensiveis` para dict já convertido em string.

E a chave é decidida por **token**, não por igualdade, porque os payloads deste
projeto misturam convenções. Colhendo os nomes reais de campo do código:
`cpf`, `cpf_cnpj`, `cpfCnpj`, `enderecoImovel`, `endereco_imovel`,
`proprietarioPrincipal`, `nomeRequerente`, `logradouro_nome_ipp`, `phones`. Uma
lista de chaves exatas erraria em quase todas.

Duas salvaguardas contra o excesso:

- **Tokens que desarmam** (`servico`, `service`, `table`, `bairro`, `event`…):
  sem eles, `nome_servico`, `service_name` e `bairro_nome` seriam redigidos — e
  são justamente o que sobra para entender a linha.
- **Valor inócuo nunca é redigido**: `email_processed: True` e `cpf_attempts: 2`
  são o rastro do caminho do workflow. Nenhum dado pessoal cabe em 4 caracteres.

O que escapa aos dois caminhos — dado sem formato **e** sem rótulo, como
`f"Nome coletado: {nome}"` e o texto livre do cidadão — é corrigido no call
site, nos blocos B e C.

### 4. Padrão numérico estrito, mas tolerante à pontuação

O padrão herdado do interceptor era `\+?\d{10,13}` — qualquer inteiro de 10 a 13
dígitos. Aplicado ao log, ele apagaria epoch em segundos (10 dígitos), epoch em
milissegundos (13) e boa parte dos protocolos. Um log redigido a ponto de não
dizer nada não serve para diagnosticar.

O padrão de telefone agora exige DDD válido (nenhum termina em 0) e primeiro
dígito coerente do assinante (9 no móvel, 2-5 no fixo). Efeito colateral bom: o
CPF sem pontuação deixou de ser rotulado `[REDACTED-PHONE]`, observação que o
próprio ticket registrou.

**Estrito no formato, tolerante no separador.** O validador do projeto
(`divida_ativa/core/models.py:13`) aceita `^\d{3}\.?\d{3}\.?\d{3}-?\d{2}$` — cada
separador é opcional **e independente do outro**. Ou seja, `123.456.78901` e
`123456789-01` são entradas válidas que chegam ao log. Cobrir só os dois extremos
(tudo pontuado / tudo colado) deixava o meio de fora; o padrão agora aceita
separador opcional em cada posição. Vale o mesmo para o CNPJ.

**Telefone fora de qualquer formato** — o que vem do cadastro do RMI em
`state.data["phone"]`, cujo formato é de terceiro, ou o que o cidadão digita sem
DDD — é coberto por uma regra de **palavra ao lado**: `Telefone:`, `whatsapp`,
`contato:` autorizam a redação do número mesmo sem forma reconhecível.

**Não são redigidos, deliberadamente:** inscrição imobiliária, protocolo,
`trace_id` e timestamp. Nenhum identifica uma pessoa sozinho, e são o que resta
para amarrar uma linha de log a um atendimento depois que o resto foi redigido.
O `bairro` fica fora pelo mesmo motivo — é grosso demais para identificar alguém
e é o que permite diagnosticar a geolocalização da poda de árvore.

Uma exceção conhecida: um protocolo de **10 dígitos** que comece com DDD válido
(`1234567890`) é redigido como telefone, porque tem exatamente a forma de um
fixo. Não há como separar os dois sem contexto.

### 4b. O regex de e-mail é possessivo por segurança, não por estilo

`[\w.+-]+@[\w-]+(?:\.[\w-]+)+` tem backtracking quadrático em texto longo sem
`@` — e este padrão roda em **toda linha de log**, sobre texto que o cidadão
escreveu. Com lookbehind e quantificadores possessivos (`++`, Python 3.11+),
8 KB de entrada adversarial caem de 0,30s para 0,0001s.

### 5. Sem chave para desligar

Não há `LOG_REDACTION_ENABLED`. Uma flag dessas é o tipo de coisa que alguém
liga para depurar em staging e esquece ligada em produção.

O que existe é uma garantia oposta: `_redigir_record` **nunca levanta**. Um
patcher que estoura derruba o `logger.*` de quem chamou (verificado), e a linha
de log vale menos do que a chamada que a produziu.

### 6. O interceptor continua vendo o nome

`_redact_pii_in_text` usa `redigir_padroes_pii` (só formato), não `redigir_texto`
(formato + chave). O interceptor é um destino diferente do log indexado, e a
decisão do CHATR-113 de preservar contexto de debug lá continua valendo — o que
mudou é que agora ele também cobre e-mail, CNPJ e blob em base64.

## O que muda de comportamento

- **Formato da linha de log:** igual ao default do loguru, sem cor. Quem
  consome o SigNoz por parse de texto não precisa mudar nada.
- **`logger.debug` para de sair** em staging e produção (`LOG_LEVEL` default
  `INFO`). Defesa em profundidade — o controle é a redação, não o nível.
- **Traceback perde o dump de locais** (`diagnose=False`) e o encadeamento
  estendido (`backtrace=False`).

## Em aberto

- **Blocos B, C e D** — os 38 call sites. A barreira cobre o que tem formato
  reconhecível ou chave rotulada; nome em f-string, endereço e texto livre do
  cidadão continuam dependendo da correção pontual.
- **CHATR-175 · retenção.** A correção no código não remove o que já está
  indexado. Recomendação: definir com quem opera o SigNoz uma janela de expurgo
  para o período afetado (do CHATR-113 até este deploy), tratando o índice de
  logs e o de traces — `mcp.tool.user_id` põe o WhatsApp em atributo de span,
  que é outro caminho até o mesmo dado.
- **Sink de terceiro não recebe a camada 2.** Se um exportador OTLP de logs for
  adicionado, o traceback precisa passar por `redigir_texto` na entrada dele.

## Onde ficou

| Camada | Arquivo |
| --- | --- |
| Padrões, chaves sensíveis e máscaras | `src/utils/pii.py` |
| Barreira global (patcher + sink) | `src/utils/log.py` |
| Payload do interceptor de erros | `src/utils/error_interceptor.py` (delega) |
| Credencial em URL/body antes do interceptor | `src/utils/http_client.py` (delega) |
| Testes | `src/tests/unit/utils/test_{pii,log_redaction}.py` |
