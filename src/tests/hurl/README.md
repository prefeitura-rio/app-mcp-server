# Smoke HTTP com Hurl

Suíte que exercita **a imagem de produção rodando**, pela porta HTTP.

## Por que existe

O `pr-quality-gate` já construía a imagem e a escaneava com Trivy — e a
descartava sem nunca executá-la. Os testes unitários montam o app ASGI em
processo (`src/tests/unit/app/test_app_factory.py`) e leem as rotas, mas não
fazem request algum.

Sobrava um vão entre as duas camadas existentes:

| Camada | O que valida | Quando roda |
| --- | --- | --- |
| `unit` (pytest) | lógica, com dependências controladas | todo PR |
| **`hurl`** | **o container sobe e fala HTTP** | **todo PR** |
| `e2e` (`run_preview_e2e.py`) | preview real, PGM real | após merge em `main` |

No vão moravam falhas que passavam o gate inteiro verde e só apareciam em
staging, depois do merge: preflight reprovando, `CMD` quebrado, env var
obrigatória faltando, middleware de auth desligado do `/mcp`,
`MCP_STATELESS_HTTP` regredindo para stateful.

## O que a suíte cobre

- **Trio de health** — `/health` (liveness trivial), `/health/ready`,
  `/health/detail`. Inclui o invariante de desenho: dependência fora do ar
  aparece no `detail` sem derrubar as outras duas.
- **Fronteira de autenticação** — `/mcp` sem token e com token inválido
  devolvem 401; com token válido, 200. Os unitários testam o
  `HybridTokenVerifier` isolado; aqui se verifica que ele está **plugado**.
- **Handshake MCP sobre Streamable HTTP** — `initialize`, `tools/list` e
  `tools/call`, em requisições independentes (modo stateless).
- **Sanitização do `/health/detail`** — a rota é pública e não pode devolver
  URL, host ou credencial.

Fora de escopo, de propósito: regra de negócio (fica no pytest, que é mais
barato e conta para o gate de cobertura) e qualquer coisa que dependa de
dependência externa alcançável.

## Rodar localmente

Contra a imagem, que é o cenário real:

```bash
docker build -t app-mcp-server:local .

# O preflight recusa credencial de service account malformada e aborta o
# boot, então um placeholder como `e30=` não serve: precisa de uma SA
# parseável. Esta é falsa e descartável — não dá acesso a nada.
openssl genrsa -out /tmp/fake-sa.pem 2048
GCP_SA=$(python3 -c '
import base64, json
key = open("/tmp/fake-sa.pem").read()
print(base64.b64encode(json.dumps({
    # sem o campo "type": ver comentario no pr-quality-gate.yaml
    "project_id": "local-smoke",
    "private_key_id": "local", "private_key": key,
    "client_email": "local@example.invalid", "client_id": "0",
    "token_uri": "https://oauth2.googleapis.com/token",
}).encode()).decode())')

# As flags de runtime reproduzem o `securityContext` dos manifestos: raiz
# somente leitura, sem capability, uid não-root (vem do `USER` da imagem) e o
# `ip_unprivileged_port_start` do kernel, que o Docker zera e o Kubernetes não.
# Rodar sem elas é a prova local que dá verde e o pod que morre no deploy.
docker run -d --name mcp-smoke -p 8080:8080 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --sysctl net.ipv4.ip_unprivileged_port_start=1024 \
  --tmpfs /tmp:mode=1777 \
  --tmpfs /app/data/bq_dlq:mode=1777 \
  --env-file .github/ci.env \
  -e GCP_SERVICE_ACCOUNT_CREDENTIALS="$GCP_SA" \
  -e DATA_DIR=/app/data \
  -e REDIS_URL=redis://127.0.0.1:6379/0 \
  app-mcp-server:local

hurl --test --variable base_url=http://127.0.0.1:8080 --variable token=test-token \
  src/tests/hurl/smoke.hurl

docker rm -f mcp-smoke
```

O Redis aponta para uma porta inalcançável de dentro do container **de
propósito**: o check correspondente reprova, o agregado vira `degraded` e
a suíte confirma que `/health` e `/health/ready` continuam 200 assim mesmo.
Nenhuma infra precisa estar de pé.

Contra um servidor já rodando, basta apontar o `base_url`:

```bash
hurl --test --variable base_url=http://127.0.0.1:8080 --variable token="$TOKEN" \
  src/tests/hurl/smoke.hurl
```

## Convenções

- `--test` é obrigatório: sem ele o Hurl imprime o corpo e **sai com 0 mesmo
  com assert falhando**.
- Relatórios (`--report-html`) saem em `src/tests/hurl/store/`, que é
  ignorado pelo git — só os `.hurl` são versionados (CHATR-133).
- Assert sobre resposta do `/mcp` passa pela cadeia `body regex "data: (.*)"
  jsonpath "$..."`: o corpo é um envelope SSE (`event: message\ndata: {...}`),
  não JSON puro.
