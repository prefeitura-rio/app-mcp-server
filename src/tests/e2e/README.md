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

## Execucao

Credenciais em `src/config/.env` (carregado automaticamente) ou no ambiente:
`CHATBOT_INTEGRATIONS_URL`, `CHATBOT_INTEGRATIONS_KEY`, `CHATBOT_PGM_API_URL`,
`CHATBOT_PGM_ACCESS_KEY`.

```bash
# So consulta - read-only, seguro.
uv run python src/tests/e2e/run_pgm_contract.py --cpf 12345678901

# Tambem emite guia. ATENCAO: gera uma guia de verdade na PGM.
uv run python src/tests/e2e/run_pgm_contract.py --cpf 12345678901 --emitir
```

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
