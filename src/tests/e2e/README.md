# E2E Preview Checks

Testes E2E usados no deploy de staging antes da promocao do preview para stable.

## Objetivo

Essa suite valida duas coisas ao mesmo tempo:

- o preview sobe e responde corretamente no endpoint de health;
- o fluxo principal de divida ativa responde corretamente com um token real de staging.

## Escopo Atual

O runner [`run_preview_e2e.py`](src/tests/e2e/run_preview_e2e.py) cobre:

- `GET /health` com resposta `200` e body `OK`;
- `POST /consulta_debitos` com token valido e payload real de staging;
- `POST /consulta_debitos` com entrada invalida, esperando erro de contrato sem `500`;
- `POST /emitir_guia` e `POST /emitir_guia_regularizacao` com payload minimo, validando resposta JSON sem `500`;
- emissao real de guia quando a consulta retorna itens elegiveis.

Quando a massa de staging nao retorna itens para emissao, os happy paths de guia sao pulados e o restante da suite continua valido.

## Variaveis de Ambiente

- `PREVIEW_BASE_URL`: URL base do preview. Padrao: `http://127.0.0.1:8080`
- `VALID_TOKENS`: o runner usa o primeiro token configurado
- `PREVIEW_CONSULTA_TIPO`: tipo usado em `/consulta_debitos`
- `PREVIEW_CONSULTA_VALOR`: valor de consulta correspondente ao tipo
- `PREVIEW_CONSULTA_ANO_AUTO_INFRACAO`: obrigatorio apenas quando o tipo for `numeroAutoInfracao`
- `PREVIEW_AVISTA_PAYLOAD`: payload final real usado no happy path de `/emitir_guia`
- `PREVIEW_REGULARIZACAO_PAYLOAD`: payload final real usado no happy path de `/emitir_guia_regularizacao`

## Execucao Local

```bash
PREVIEW_BASE_URL="http://127.0.0.1:8080" \
VALID_TOKENS="token-e2e,token-2" \
PREVIEW_CONSULTA_TIPO="cpfCnpj" \
PREVIEW_CONSULTA_VALOR="12345678900" \
python3 src/tests/e2e/run_preview_e2e.py
```

## Workflow

No GitHub Actions de staging, os segredos de runtime sao buscados em runtime a partir do Infisical usando `client-id` e `client-secret`.

O workflow precisa destes GitHub Secrets para autenticar no Infisical:

- `INFISICAL_CLIENT_ID`
- `INFISICAL_CLIENT_SECRET`
- `INFISICAL_PROJECT_SLUG`
- `INFISICAL_URL`

Como o app ja usa as variaveis do `env.py`, a recomendacao para autenticacao do E2E e:

- manter `VALID_TOKENS` no Infisical;
- incluir nele um token tecnico dedicado ao E2E de staging;
- preferir esse token como primeiro item da lista, ja que o runner usa o primeiro valor disponivel.

Os parametros de consulta usados pelo teste nao precisam ficar no Infisical. No workflow, use:

- GitHub Variable `PREVIEW_CONSULTA_TIPO`
- GitHub Secret `PREVIEW_CONSULTA_VALOR`
- GitHub Secret `PREVIEW_CONSULTA_ANO_AUTO_INFRACAO` apenas se algum dia o tipo for `numeroAutoInfracao`
- GitHub Secret `PREVIEW_AVISTA_PAYLOAD`
- GitHub Secret `PREVIEW_REGULARIZACAO_PAYLOAD`

## E2E vs Quality Gate

Nao ha duplicacao real com o `pr-quality-gate`.

- o `pr-quality-gate` roda testes unitarios e internos com dependencias simuladas ou locais;
- o deploy de staging roda E2E contra o preview real no cluster, com autenticacao real e integracao real.

Os dois gates se complementam: unitario pega regressao de codigo cedo e barato; E2E protege a promocao do ambiente.

## Evolucao Recomendada

Se a gente quiser aprofundar mais, o proximo passo natural e extrair essa logica para testes `pytest`, adicionando asserts mais ricos sobre schema e respostas de negocio. O desenho atual ja entrega um gate de promocao mais forte sem aumentar muito o tempo nem as dependencias do workflow.

---

# Contrato da API da PGM

O runner [`run_pgm_contract.py`](run_pgm_contract.py) e um teste de integracao
sob demanda: nao roda em pipeline nenhuma, e existe para responder uma pergunta
so — **o que vem e vai da PGM ainda e o que o codigo espera?**

## Por que existe

A resposta de emissao da PGM nao e documentada, e ja nos surpreendeu. O
CHATR-164 teve uma implementacao inteira escrita supondo que a emissao
devolvia valor e natureza; so descobrimos que nao devolve lendo log de
producao, depois do codigo pronto. Ver
[`CHATR-164-valor-e-itens.md`](../../../docs/decisions/CHATR-164-valor-e-itens.md).

Este runner transforma aquilo que aprendemos por log em verificacao executavel.

## O que verifica

Chama a PGM pelo mesmo caminho da producao (`internal_request` ->
chatbot-integrations), nao por HTTP contra o MCP.

**Presenca de campos** — os que o codigo le em cada estrutura: consulta, CDA,
EF, guia parcelada e registro de emissao.

**Invariantes**, que e onde esta o valor real do teste:

- `valorSaldoPrincipal + valorSaldoHonorarios == valorSaldoTotal`, por CDA;
- soma dos itens nao parcelados == `saldoTotalNaoParcelado`;
- natureza de cada CDA presente em `naturezasDivida` — e o que sustenta deduzir
  a natureza da EF por eliminacao;
- na emissao, o valor extraido do PIX (campo 54) e o do codigo de barras
  (posicoes 5-15) precisam concordar: sao fontes independentes da mesma guia.

**Mudancas a favor** também são sinalizadas: se a emissao passar a trazer
`valorTotal`, ou a EF passar a trazer `naturezaDivida`, sai um aviso — deriva-los
deixa de ser necessario.

## PII

A resposta da PGM carrega nome, CPF e endereco do cidadao. O relatorio imprime
nomes de campos, contagens e valores monetarios; nunca o conteudo dos campos
identificadores, nem no diagnostico de campo faltante.

Sao dois caminhos, porque a PII chega por dois:

- campos conhecidos saem por `CAMPOS_PII` antes de qualquer diagnostico;
- texto livre da PGM (`motivos`, mensagens de erro) passa por
  `sem_identificadores`, que mascara sequencias de 6+ digitos — CPF, CNPJ,
  numero de CDA ou de EF. Valor monetario nao e afetado: o maior grupo de
  digitos em `R$26.819,86` tem 3.

Uma excecao deliberada: o numero da CDA usada em `--emitir` sai no cabecalho da
secao. Sem ele o operador nao localiza na PGM a guia que acabou de criar, e o
numero do debito nao revela nome, CPF nem endereco.

## Execucao

Credenciais em `src/config/.env` (carregado automaticamente) ou no ambiente:
`CHATBOT_INTEGRATIONS_URL`, `CHATBOT_INTEGRATIONS_KEY`, `CHATBOT_PGM_API_URL`,
`CHATBOT_PGM_ACCESS_KEY`.

```bash
# So consulta - read-only, seguro.
PGM_CONTRATO_CPF=12345678901 uv run python src/tests/e2e/run_pgm_contract.py

# Tambem emite guia. ATENCAO: gera UMA guia de verdade na PGM.
PGM_CONTRATO_CPF=12345678901 uv run python src/tests/e2e/run_pgm_contract.py --emitir
```

`--cpf` continua aceito e e equivalente, mas deixa o documento no historico do
shell e visivel em `ps`. O `--emitir` chama o endpoint de emissao **uma vez**:
a verificacao do processamento reusa os registros dessa chamada, em vez de
emitir de novo.

O CPF precisa ser de contribuinte **com debitos em aberto** — sem massa, nao ha
o que verificar. Uma massa com CDA e EF ao mesmo tempo exercita mais contrato.

## Codigos de saida

| Codigo | Significado |
| --- | --- |
| `0` | Contrato integro |
| `1` | Quebra de contrato: algo que o codigo le mudou ou sumiu |
| `2` | Nada verificado (massa vazia, credencial faltando) — o contrato segue desconhecido |

O `2` e deliberado: sem massa, dizer "integro" seria falso conforto.

## Por que nao e `test_*.py`

`testpaths` do pytest aponta para `src/tests`, entao um arquivo `test_*.py`
aqui seria coletado pelo `pr-quality-gate` e falharia no CI, que nao tem
credencial da PGM — nem deveria ter. O prefixo `run_` mantem estes runners fora
da coleta, como os demais desta pasta.

# Contrato do line-up do Rock in Rio (CHATR-187)

O runner [`run_rock_in_rio_contract.py`](src/tests/e2e/run_rock_in_rio_contract.py)
bate no site oficial do Rock in Rio e verifica se o HTML ainda tem a forma que
o parser de `src/tools/rock_in_rio/scraper.py` espera.

Existe por um motivo so: **avisar quando o site mudar**. A tool depende de
raspar HTML de terceiro, que pode trocar de tema a qualquer momento e sem
aviso — inclusive na vespera do festival. Os testes unitarios rodam sobre
fixtures salvas e continuariam verdes nesse cenario.

```bash
uv run python src/tests/e2e/run_rock_in_rio_contract.py
```

Nao precisa de credencial nenhuma, so de rede. Valida, para cada um dos sete
dias, que o numero de atracoes esta numa faixa plausivel e que os palcos
pertencem ao catalogo conhecido. As faixas sao propositalmente largas: uma
atracao a mais ou a menos e mudanca legitima de line-up, e reprovar por isso
transformaria o runner em ruido. O que precisa disparar e a mudanca de ordem de
grandeza — tipicamente o parser tendo parado de casar com o HTML.

| Codigo | Significado |
| --- | --- |
| `0` | Contrato integro |
| `1` | O site mudou, ou nao respondeu |

Fica fora do CI pelo mesmo motivo dos demais runners desta pasta: uma
indisponibilidade momentanea do `rockinrio.com` nao pode reprovar um PR que nao
tem nada a ver com isso.

---

# Chat local do Rock in Rio (CHATR-187)

O runner [`run_chat_rock_in_rio.py`](run_chat_rock_in_rio.py) sobe uma página com
cara de WhatsApp em `http://127.0.0.1:8100/` onde dá para perguntar o que o
cidadão perguntaria e ver a resposta montada a partir do retorno real da tool
`rock_in_rio_lineup`.

```bash
# a tool no próprio processo: sem servidor, sem Redis, sem token
uv run python src/tests/e2e/run_chat_rock_in_rio.py

# a tool publicada pelo servidor MCP (nome registrado, auth, serializacao)
uv run python src/tests/e2e/run_chat_rock_in_rio.py --mcp
```

## Por que existe

O `run_rock_in_rio_contract.py` responde se o parser ainda casa com o HTML. Ele
não responde a outra metade da pergunta: **o que o cidadão recebe.** É uma tool
de resposta única e sem argumentos, então o Inspector mostra só um JSON de 156
atrações — nada ali diz se "quem toca hoje" acerta o dia, ou se quem pergunta
por horário sai com a informação certa.

Não tem LLM, e é de propósito: o roteamento da pergunta é determinístico e mora
em Python, então duas execuções iguais dão a mesma resposta e dá para apontar o
dedo para o que quebrou. Quem responde "o modelo escolhe a tool certa?" é o chat
do VS Code em agent mode, não este runner.

## O que dá para ver aqui

- **A lógica de "hoje"** do `_situacao_temporal`: o festival ainda não começado,
  o intervalo de 08 a 10 de setembro, a jornada que avança pela madrugada. A
  página não recalcula nada disso — consome o campo `situacao` que a tool monta.
- **Dia e palco de cada atração**, conferidos contra o que o site publica.
- **A ausência de horários**, que é o risco número um da tool. Pergunte "que
  horas começa" e veja a resposta que o cidadão recebe.
- **A tela de indisponibilidade**: rode com a rede cortada (ou com `--mcp` sem
  servidor no ar) e confirme que ela não vaza line-up nenhum.
- **Nome errado ou com erro de digitação** — "avenged sevenfould" acha
  `AVENGED SEVENFOLD`, e uma banda que não está na edição recebe um "não
  encontrei" explícito em vez de silêncio.

O painel do rodapé (`</>`) mostra a fatia crua do retorno usada em cada resposta,
as `instrucoes_de_resposta` que a LLM receberia e a resposta completa da tool.

## Flags

| Flag | Padrão | Para que |
| --- | --- | --- |
| `--porta` | `8100` | porta do chat |
| `--bind` | `127.0.0.1` | prefira loopback + `tailscale serve` |
| `--mcp` | — | chama a tool pelo servidor MCP em vez de no processo |
| `--url` | `http://127.0.0.1:80/mcp` | servidor MCP alvo (com `--mcp`) |
| `--token` | 1º de `VALID_TOKENS` | Bearer do servidor (com `--mcp`) |
| `--sem-navegador` | — | não abre o navegador sozinho |

O botão `⟳` do cabeçalho refaz a busca na fonte sem reiniciar o processo — sem
ele, o cache de processo devolveria o mesmo dado por até uma hora.

Para mostrar a alguém, `tailscale serve --bg 8100`: a página só fala com dois
endpoints que consultam uma tool de leitura, então não dá execução na sua
máquina — diferente do Inspector. Não use `--bind 0.0.0.0`, que abre para a rede
local inteira.
