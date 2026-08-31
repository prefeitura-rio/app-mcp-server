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
| `patcher` | antes de **todo** sink | `record["extra"]`, que não passa pela formatação e chega inteiro a um sink que serializa o record |
| `sink` | texto já formatado | `str(exception)` e o traceback, que não existem antes da formatação |

**O patcher não vê estrutura.** O loguru monta o record com `str(message)`
(`_logger.py:2024`) e só então chama o patcher (`_logger.py:2060`), então
`logger.info({...})` chega já stringificado — não há janela para redigir o dict
de pé. Quem cobre esse caso é `redigir_chaves_sensiveis`, que reconhece a chave
dentro do texto. A consequência prática é que **essa cobertura por chave é a
única que existe para nome e endereço em `logger.*`**: tirá-la de
`redigir_texto` reabre o vazamento. `redigir_estrutura` continua valendo para
`extra` e para os payloads do interceptor e do cliente HTTP, onde a estrutura
de fato sobrevive.

O sink próprio também é o que desliga `diagnose`/`backtrace` e aplica
`LOG_LEVEL`.

**A segunda passada é condicional.** O que ela acrescenta é exatamente
`str(exception)` e o traceback; sem exceção no record, tudo que varia na linha é
`{message}` — já redigido pelo patcher — mais timestamp, nível e origem, que não
são PII. Redigir de novo dobrava o custo do caminho quente, e esse custo roda no
thread do event loop: 106 µs → 46 µs por linha típica.

**Consequência a conhecer:** um sink adicionado fora de `src/utils/log.py` (um
teste, ou um exportador OTLP de logs no futuro) recebe a camada 1, não a 2 — o
traceback chegaria nele sem redigir.

Módulos que fazem `from loguru import logger` direto continuam cobertos: o
`logger` é singleton e a configuração é global. Foi por isso que **nenhum dos 18
arquivos precisou trocar de import**.

### 3. Nome e endereço não entram como regex — a chave decide

Não existe padrão confiável para nome próprio em texto solto: uma regex que
tentasse ou destruiria mensagem legítima ou não pegaria nada. A cobertura desses
vem de **olhar a chave, não o valor** — `redigir_chaves_sensiveis` para o texto
que chega ao logger (que é sempre texto, ver acima) e `redigir_estrutura` onde a
estrutura sobrevive: `record["extra"]`, o payload do interceptor e o do cliente
HTTP.

E a chave é decidida por **token**, não por igualdade, porque os payloads deste
projeto misturam convenções. Colhendo os nomes reais de campo do código:
`cpf`, `cpf_cnpj`, `cpfCnpj`, `enderecoImovel`, `endereco_imovel`,
`proprietarioPrincipal`, `nomeRequerente`, `logradouro_nome_ipp`, `phones`. Uma
lista de chaves exatas erraria em quase todas.

Duas salvaguardas contra o excesso:

- **Tokens que desarmam** (`servico`, `service`, `table`, `bairro`, `event`…):
  sem eles, `nome_servico`, `service_name` e `bairro_nome` seriam redigidos — e
  são justamente o que sobra para entender a linha. O veto vale **só sobre token
  genérico** (`nome`, `name`), que rotula qualquer coisa. Desarmando a chave
  inteira, bastava um `servico` em qualquer posição para
  `nome_do_cliente_do_servico` sair em claro; `cliente`, `cpf` e `endereco`
  ganham do veto.
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

### 4b. Regex que roda em toda linha de log precisa ser linear

Vale para dois padrões, pela mesma razão: eles processam texto que o cidadão
escreveu, no thread do event loop, duas vezes por linha (patcher e sink).

**E-mail.** `[\w.+-]+@[\w-]+(?:\.[\w-]+)+` tem backtracking quadrático em texto
longo sem `@`. Com lookbehind e quantificadores possessivos (`++`, Python
3.11+), 8 KB de entrada adversarial caem de 0,30s para 0,0001s.

**Chave/valor.** `[A-Za-z0-9_]*(?:alternação de 35 tokens)[A-Za-z0-9_]*` tem o
mesmo problema, e pior: sem uma guarda de ancoragem o motor tenta casar em cada
posição de um run de `[A-Za-z0-9_]` e, em cada uma, refaz o backtracking sobre a
alternação inteira — O(n² · k). Medido: um `logger.info` com 4 KB adversariais
custava **2,4 s de CPU no event loop**, o que faz de uma mensagem de WhatsApp um
DoS. Com `(?<![A-Za-z0-9_])`, 1,9 ms.

O `_BLOB_BASE64` encurta runs de 64+ caracteres antes, mas não protege: basta um
`_` no meio para o run deixar de ser base64 e continuar sendo um run para o
padrão de chave/valor.

### 4c. Blob se distingue de caminho pelo formato, não pelo alfabeto

`[A-Za-z0-9+/]{64,}` também casa com caminho de arquivo e rota de API, que são
runs longos dos mesmos caracteres — e apagava do log justamente o que localiza o
erro. Tentar separar pelo alfabeto não funciona (`QUJDRA...` é base64 legítimo
sem um dígito sequer); o que separa é a forma. Em base64 o `/` aparece com
probabilidade 1/64 por caractere, então os pedaços entre barras são longos; em
caminho são curtos e numerosos.

Errar para o lado do blob não redigido é aceitável: o conteúdo já está
codificado, e a chave que o carrega (`pdf`, `base64`) continua sendo redigida
pela chave.

### 4d. Credencial não usa a isenção de valor curto

`_valor_e_inocuo` deixa passar valor numérico de até 4 caracteres, para preservar
`cpf_attempts: 2` e `email_processed: True`. O argumento — nenhum dado pessoal
cabe em 4 caracteres — vale para PII e **não** vale para segredo: PIN, OTP e
código de acesso moram exatamente nessa faixa, e `senha: 1234` estava saindo em
claro. Chave de credencial desarma a isenção.

### 4e. `LOG_LEVEL` inválido não pode impedir a subida

Passar a **aplicar** o `LOG_LEVEL` cria um modo de falha que não existia enquanto
ele era lido e ignorado: o loguru exige o nome em maiúsculas e levanta
`ValueError` no resto, e `LOG_LEVEL=info` ou vazio é o que um ConfigMap produz
sem esforço. Como `src/utils/log.py` é importado na primeira linha de
`src/main.py`, o estouro viria **antes do preflight** — trocando o relatório
consolidado de variáveis faltantes por um traceback de dentro do loguru, em
CrashLoopBackOff. O valor é normalizado e cai para INFO com aviso. Nível de log
errado é motivo para logar mais, nunca para não subir.

### 5. Sem chave para desligar

Não há `LOG_REDACTION_ENABLED`. Uma flag dessas é o tipo de coisa que alguém
liga para depurar em staging e esquece ligada em produção.

O que existe são duas garantias opostas, e são independentes uma da outra.

**Nunca levanta.** Um patcher que estoura derruba o `logger.*` de quem chamou
(verificado), e a linha de log vale menos do que a chamada que a produziu.

**Falha fechado.** Não levantar não diz nada sobre *qual valor fica*, e devolver
o texto original quando a redação quebra — uma regex nova mal formada, um objeto
cujo `__str__` levanta — é pior do que não ter barreira: o vazamento é silencioso
e ninguém vai procurar por ele. A linha vira
`[REDACAO-FALHOU:<TipoDaExcecao>]`, que é o termo a alertar no SigNoz. O tipo da
exceção entra porque localiza a causa; a mensagem dela, não — costuma repetir o
valor que estourou a redação. Mensagem e `extra` têm `try` separados, para que
uma falha em um não devolva o outro em claro.

Perder a linha custa um diagnóstico; deixá-la passar custa o CPF.

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
- **A barreira cobre só o loguru.** Não há ponte `logging` → loguru, então o que
  uvicorn, starlette, opentelemetry e google-cloud escrevem pela stdlib não passa
  por redação nenhuma — `src/utils/log.py` só ajusta o nível de `httpx` e
  `httpcore`. Hoje o risco é baixo, porque o que sai por lá é log de acesso e de
  infraestrutura, mas qualquer dependência nova que logue payload cai fora da
  barreira sem aviso. A correção é um `InterceptHandler` roteando a raiz do
  `logging` para o loguru, em ticket próprio: muda o roteamento de log de todas
  as dependências de uma vez.

## Onde ficou

| Camada | Arquivo |
| --- | --- |
| Padrões, chaves sensíveis e máscaras | `src/utils/pii.py` |
| Barreira global (patcher + sink) | `src/utils/log.py` |
| Payload do interceptor de erros | `src/utils/error_interceptor.py` (delega) |
| Credencial em URL/body antes do interceptor | `src/utils/http_client.py` (delega) |
| Testes | `src/tests/unit/utils/test_{pii,log_redaction}.py` |
