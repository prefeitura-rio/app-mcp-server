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

```env
VALID_TOKENS="token"
IS_LOCAL="true"
```

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
