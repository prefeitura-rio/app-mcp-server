# AGENTS.md

Guia para agentes (e pessoas) que alteram este repositório: o servidor MCP da
prefeitura do Rio (Python 3.11, FastMCP, `uv`). Os comandos abaixo são os que o
workflow `.github/workflows/pr-quality-gate.yaml` roda em todo PR; rode-os antes
de commitar.

## Ambiente

```bash
uv sync --frozen --all-groups
```

`--frozen` usa o `uv.lock` como está: dependência nova entra pelo
`pyproject.toml` e por `uv lock`, com o `uv.lock` no mesmo commit.

## Testes

```bash
set -a && . ./.github/ci.env && set +a
export REDIS_URL=redis://localhost:6379/0 MONGODB_URL=mongodb://localhost:27017 \
  DATA_DIR=/tmp GCP_SERVICE_ACCOUNT_CREDENTIALS=e30=
uv run pytest
```

- O ambiente é o mesmo do job `Tests + Coverage Gate`: as variáveis de
  `.github/ci.env` (valores falsos, compartilhados com o job `build`) mais as
  quatro específicas do job. Sem elas, a coleta quebra com
  `OSError: Environment variable ... is not set`; os defaults de
  `src/tests/conftest.py` não cobrem tudo. Nenhuma credencial real é
  necessária.
- `testpaths = ["src/tests"]`: a suíte inteira (`unit`, `integration`,
  `resilience`, `tools`) roda de uma vez.
- Os testes de `src/tests/integration/` exigem Redis alcançável em `REDIS_URL`
  (padrão `redis://localhost:6379/0`). **Sem Redis, o módulo inteiro é pulado e
  o pytest fica verde assim mesmo**: quem mexe nesses caminhos precisa de um
  Redis de pé para saber. No PR, o job `Tests + Coverage Gate` sobe Redis e
  MongoDB como services e eles rodam.
- `asyncio_mode = "strict"`: teste async precisa de `@pytest.mark.asyncio`.
- Estratégia da suíte e quando um teste vai para `integration/`:
  `src/tests/README.md` e `docs/decisions/CHATR-133-politica-de-testes.md`.

## Cobertura

O PR roda `uv run pytest --cov=src --cov-report=xml` e reprova se:

- a cobertura ficar abaixo de `COVERAGE_MINIMUM` (definido no topo do
  `pr-quality-gate.yaml`, fonte única do piso); ou
- cair mais de **0,1 pp** em relação à baseline versionada em
  `.github/coverage-baseline.json`.

A cobertura mede só código de produção (`src/tests/*` fica fora, ver
`pyproject.toml`). Código novo vem com teste pytest; teste Hurl não conta para
a cobertura.

## Lint e formatação

```bash
uvx ruff@0.15.5 check .
uvx ruff@0.15.5 format --check .
```

Use a mesma versão do CI (`ruff@0.15.5`). Para corrigir: `uvx ruff@0.15.5
format .` e `uvx ruff@0.15.5 check --fix .`. O job `Lint` também roda
`hadolint` no `Dockerfile`.

## Smoke HTTP (Hurl)

`src/tests/hurl/smoke.hurl` roda no job `Build + Image Scan + Smoke HTTP`
contra a imagem construída no PR (health, fronteira de autenticação do `/mcp`
e handshake MCP stateless). Para rodar localmente (exige Docker e Hurl), siga
`src/tests/hurl/README.md`; contra um servidor já de pé:

```bash
hurl --test --variable base_url=http://127.0.0.1:8080 --variable token="$TOKEN" \
  src/tests/hurl/smoke.hurl
```

`--test` é obrigatório: sem ele o Hurl sai com 0 mesmo com assert falhando.

## Dados pessoais e segredos

O servidor trata dados de contribuintes e cidadãos (LGPD):

- **CPF, CNPJ, inscrição imobiliária, CDA, número de guia, nome, endereço,
  telefone e e-mail nunca vão para log, teste, fixture, commit, PR ou issue.**
  Testes usam só dados sintéticos.
- A redação de PII nos logs é global (`src/utils/log.py` com os padrões de
  `src/utils/pii.py`; decisão em
  `docs/decisions/CHATR-167-redacao-de-pii-nos-logs.md`), mas não alcança dado
  sem formato nem rótulo (`f"Nome: {nome}"`): não logue o valor.
- Nada de `.env` nem credencial no repositório: `.github/ci.env` só tem valores
  falsos de teste. Configuração de staging e produção muda pelos manifests de
  `k8s/` e pela infraestrutura, nunca à mão.

## Deploy

O merge na `main` publica a imagem (`build-docker.yaml`) e o
`deploy-staging.yaml` atualiza `k8s/staging/resources.yaml` e promove o
Rollout `mcp` em staging. Produção sai pelo `release.yaml`.
