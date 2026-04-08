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

## E2E vs Quality Gate

Nao ha duplicacao real com o `pr-quality-gate`.

- o `pr-quality-gate` roda testes unitarios e internos com dependencias simuladas ou locais;
- o deploy de staging roda E2E contra o preview real no cluster, com autenticacao real e integracao real.

Os dois gates se complementam: unitario pega regressao de codigo cedo e barato; E2E protege a promocao do ambiente.

## Evolucao Recomendada

Se a gente quiser aprofundar mais, o proximo passo natural e extrair essa logica para testes `pytest`, adicionando asserts mais ricos sobre schema e respostas de negocio. O desenho atual ja entrega um gate de promocao mais forte sem aumentar muito o tempo nem as dependencias do workflow.
