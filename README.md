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
```

Use `MCP_STATELESS_HTTP=false` apenas como rollback operacional se algum cliente depender explicitamente de sessão MCP stateful. Redis não é necessário para o modo stateless básico; ele só deve ser considerado para resumability/eventos SSE se o servidor passar a usar esse tipo de recurso.


### REDIS port-forward

`kubectx rj-superapp or rj-superapp-staging`
`kubectl port-forward svc/mcp-redis -n mcp 6379:6379`

## 💰 Dívida Ativa — endpoints HTTP

| Endpoint | Status |
|---|---|
| `POST /consulta_debitos` | ativo |
| `POST /emitir_guia` | **deprecated** — migrar para `/v2/emitir_guia` |
| `POST /emitir_guia_regularizacao` | **deprecated** — migrar para `/v2/emitir_guia_regularizacao` |
| `POST /v2/emitir_guia` | ativo, com validação |
| `POST /v2/emitir_guia_regularizacao` | ativo, com validação |

### v2: validação com Pydantic

A v2 (`src/tools/divida_ativa_v2/`) valida entrada e saída com Pydantic antes de
chamar a PGM. A v1 continua funcionando sem alterações enquanto os consumidores migram.

O que a v2 garante:

- **Aceita os dois formatos.** Tipos nativos (`dict`/`list`/`bool`) e o formato legado
  do SFMC, em que todo campo chega como string JSON escapada. String vazia vira
  coleção vazia; `apenas_um_item` aceita `"1"`, `1`, `1.0` ou `true`.
- **Rejeita placeholder de template não renderizado.** Qualquer campo contendo
  `{{...}}` (ex.: `{{Event.DEAudience-...}}`) é recusado com mensagem nomeando o
  campo, em vez de estourar `could not convert string to float`.
- **Nunca chama a PGM com entrada inválida.** Falha de validação encerra a
  requisição antes da chamada externa.
- **Recusa seleção vazia.** Se nenhum sequencial resolver para um identificador
  conhecido — chave ausente em `dicionario_itens`, identificador fora das listas, ou
  campo renomeado pelo consumidor — a requisição é recusada em vez de chegar à PGM
  com `cdas`/`efs`/`guias` vazios. Resolução parcial (alguns itens válidos) segue
  emitindo a guia, com os descartados registrados em log.
- **Normalização simétrica de sequencial.** As chaves de `dicionario_itens` passam
  pela mesma normalização de `itens_informados`/`apenas_um_item`, então `"01"` casa
  com `"1"`. Chaves que colidem após normalização são rejeitadas.
- **Resposta tipada** (`EmitirGuiaResponse`), com o mesmo contrato JSON da v1.

Erros de validação retornam **HTTP 200 com `api_resposta_sucesso: false`** e
`api_descricao_erro` preenchido — o mesmo contrato que a v1 já usa, para não quebrar
os consumidores (SFMC/LLM). Toda falha de validação é reportada ao error interceptor
(`ValidationError` para erros de schema, `SelecaoVaziaError` para seleção vazia), que é
como se acompanha se o SFMC parou de enviar placeholders.

```bash
# payload legado (strings JSON escapadas) — aceito
curl -X POST http://localhost:80/v2/emitir_guia \
  -H "Content-Type: application/json" \
  -d '{
    "dicionario_itens": "{\"1\": \"01/225716/2024-00\"}",
    "lista_cdas": "[\"01/225716/2024-00\"]",
    "lista_efs": "",
    "lista_guias": "",
    "apenas_um_item": "1"
  }'

# payload nativo — também aceito
curl -X POST http://localhost:80/v2/emitir_guia \
  -H "Content-Type: application/json" \
  -d '{
    "dicionario_itens": {"1": "01/225716/2024-00"},
    "lista_cdas": ["01/225716/2024-00"],
    "itens_informados": ["1"]
  }'
```

Testes: `uv run pytest src/tests/unit/tools/test_divida_ativa_v2.py -v`
