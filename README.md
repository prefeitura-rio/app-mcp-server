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
