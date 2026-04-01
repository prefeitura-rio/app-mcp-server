# Rio de Janeiro MCP Server

Servidor MCP (Model Context Protocol) da prefeitura do Rio de Janeiro.

### Instalação Local

1. Clone o repositório:
```bash
git clone <repository-url>
cd app-mcp-server
```

2. Instale as dependências:
```bash
uv sync
```

3. Crie o arquivo `src/config/.env`:
```env
VALID_TOKENS="token"
IS_LOCAL="true"
```

## 🛠️ Uso Local

### Opção 1: Interface Web Local

Para usar a interface web local e testar todas as funcionalidades:

**Importante**: Certifique-se de que `IS_LOCAL=true` no arquivo `.env`. Para desativar a autenticação local adicione `DANGEROUSLY_OMIT_AUTH=true` ao `.env` e escolha `STDIO` como Transport Type na UI.

```bash
uv run mcp dev src/app.py
```

O servidor estará disponível em `http://localhost:627X`


### Opção 2: Execução Direta

Para executar o servidor diretamente:

```bash
uv run src/main.py
```

O servidor estará disponível em `http://localhost:80/mcp/`


### REDIS port-forward

`kubectx rj-superapp or rj-superapp-staging`
`kubectl port-forward svc/mcp-redis -n mcp 6379:6379`

### Pre-commit

Para rodar formatação e lint automaticamente antes de cada commit:

```bash
uv add --dev pre-commit
uv run pre-commit install
```

Depois da instalação, o hook roda automaticamente sempre que você executar:

```bash
git commit
```

Se você quiser rodar manualmente antes do commit, em todo o repositório:

```bash
uv run pre-commit run --all-files
```

Resumo do fluxo:
- `uv run pre-commit run --all-files`: roda manualmente em todos os arquivos
- `git commit`: roda automaticamente nos arquivos incluídos no commit

Hooks configurados:
- `ruff-format`: formata os arquivos automaticamente
- `ruff --fix`: corrige problemas simples de lint antes do commit

### Cobertura De Testes Local

Para ver a cobertura localmente sem depender do PR:

```bash
uv add --dev pytest-cov coverage
uv run pytest --cov=src --cov-report=term-missing --cov-report=xml
```

Esse comando:
- instala o plugin de cobertura no ambiente local
- roda a suíte em `src/tests`
- mostra a cobertura no terminal
- gera o arquivo `coverage.xml`

Se você quiser comparar com a baseline usada no CI, confira:

- `.github/coverage-baseline.json`

Se quiser reproduzir mais perto do ambiente do GitHub Actions, use as mesmas variáveis dummy do workflow `pr-quality-gate.yaml`, porque este projeto depende bastante de variáveis de ambiente já no import dos módulos.
