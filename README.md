# Rio de Janeiro MCP Server

Servidor MCP (Model Context Protocol) da prefeitura do Rio de Janeiro.

## Visão Geral

Este repositório hoje já cobre:

- execução local do servidor MCP;
- quality gate de PR com lint, testes, coverage e build dry-run;
- deploy de `staging` com preview, E2E antes da promoção e abort em caso de falha;
- deploy de `production` via GitHub Release com rollout canário monitorado;
- notificação no Discord para `staging` e `production`.

## Instalação Local

1. Clone o repositório:

```bash
git clone <repository-url>
cd app-mcp-server
```

2. Instale as dependências:

```bash
uv sync
```

3. Configure as variáveis locais em `src/config/.env`:

```bash
cp src/config/.env.example src/config/.env
# preencha os ___FILL_ME___ com as credenciais reais
```

Mínimo absoluto pra subir o servidor:

```env
VALID_TOKENS="token"
IS_LOCAL="true"
```

> **Nota:** não defina `ENVIRONMENT="staging"` no `.env` — o default já é
> `staging`, e setar isso quebra o conftest que sobrescreve pra `"test"` em
> `pytest`.

4. Pra usar workflows que dependem de Vertex AI / Reasoning Engine (interactive_test,
   headless_test, scripts de agente), aponte ADC pra uma Service Account com
   acesso ao projeto `rj-superapp-staging`:

```env
# em src/config/.env:
GOOGLE_APPLICATION_CREDENTIALS="/path/to/agent-engine-sa.json"
```

A SA `agent-engine@rj-superapp-staging.iam.gserviceaccount.com` é a que
normalmente tem permissão. Alternativa: `gcloud auth application-default login`.

## Uso Local

### Opção 1: Interface Web Local

Para usar a interface web local e testar o servidor:

```bash
uv run mcp dev src/app.py
```

O servidor ficará disponível em `http://localhost:627X`.

Se quiser omitir autenticação local para testes manuais, adicione:

```env
DANGEROUSLY_OMIT_AUTH="true"
```

### Opção 2: Execução Direta

```bash
uv run src/main.py
```

O servidor ficará disponível em `http://localhost:80/mcp/`.

### Opção 3: Headless test contra o agente local

Pra testar o agente local (mesmo prompt e tools de staging) sem precisar de
TTY — útil pra smoke tests em CI, debug rápido ou rodar fluxos multi-step
via script:

```bash
# one-shot, resposta limpa
uv run src/utils/agent/headless_test.py "qual horas são?"

# trace passo-a-passo (tool calls + thinking)
uv run src/utils/agent/headless_test.py --verbose "qual o IPTU da inscrição 05856711?"

# script multi-turn (workflows como poda, IPTU, reparo dependem disso pra
# manter contexto entre turns via InMemorySaver do LangGraph)
uv run src/utils/agent/headless_test.py --script ./turns.txt

# Saída JSON pro pipeline
uv run src/utils/agent/headless_test.py --json "..." | jq .
```

`interactive_test.py` ainda existe pra REPL com TTY. A diferença é que o
`headless_test.py` não instancia o `remote_agent` (não pede permissão IAM
no Reasoning Engine).

## Testes

A documentação de testes fica em:

- `src/tests/README.md`
- `src/tests/e2e/README.md`

Comandos mais comuns:

```bash
uv run pytest src/tests/unit -q
uv run pytest --cov=src --cov-report=term --cov-report=xml -q
python3 src/tests/e2e/run_preview_e2e.py
```

## CI/CD

### PR Quality Gate

O workflow de PR valida:

- lint;
- security scan;
- testes unitários;
- coverage com baseline do repositório;
- build dry-run da imagem.

O baseline atual fica versionado em:

- `.github/coverage-baseline.json`

### Staging

O workflow `deploy-staging.yaml`:

- roda em `push` para `staging`;
- aplica a nova imagem no preview;
- faz port-forward do serviço `mcp-preview`;
- executa os E2E de preview;
- promove o rollout se os E2E passarem;
- aborta e restaura o image override se os E2E falharem.

### Production

O workflow `release.yaml`:

- roda quando uma GitHub Release é publicada;
- builda a imagem com a tag da release;
- atualiza o Argo CD Application;
- monitora o rollout canário por até `45` minutos;
- restaura o override anterior se o rollout falhar.

## Redis port-forward

Quando precisar acessar o Redis do cluster:

```bash
kubectx rj-superapp
kubectl port-forward svc/mcp-redis -n mcp 6379:6379
```

ou em staging:

```bash
kubectx rj-superapp-staging
kubectl port-forward svc/mcp-redis -n mcp 6379:6379
```

## Pre-commit

Para instalar os hooks:

```bash
uv run pre-commit install
```

Para rodar manualmente:

```bash
uv run pre-commit run --all-files
```

Hooks configurados:

- `ruff-format`: formata os arquivos automaticamente
- `ruff --fix`: corrige problemas simples de lint antes do commit
