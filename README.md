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

O servidor estará disponível em `http://localhost:80/mcp`

### Configuração do transporte MCP

Em ambientes não-locais, o endpoint `/mcp` roda com Streamable HTTP stateless por padrão (`MCP_STATELESS_HTTP=true`). Isso evita que o handshake MCP dependa de cair sempre no mesmo pod quando há mais de uma réplica.

Variáveis úteis:

```env
MCP_STATELESS_HTTP=true
MCP_JSON_RESPONSE=false
```

Use `MCP_STATELESS_HTTP=false` apenas como rollback operacional se algum cliente depender explicitamente de sessão MCP stateful. Redis não é necessário para o modo stateless básico; ele só deve ser considerado para resumability/eventos SSE se o servidor passar a usar esse tipo de recurso.


### REDIS port-forward

`kubectx rj-superapp or rj-superapp-staging`
`kubectl port-forward svc/mcp-redis -n mcp 6379:6379`
