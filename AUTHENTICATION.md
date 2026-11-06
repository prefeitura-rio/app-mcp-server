# Autenticação do MCP Server

Este documento descreve o modelo de autenticação do servidor MCP: como funciona hoje, quais variáveis de ambiente precisam ser configuradas, como um consumidor externo (ex: Salesforce) se conecta via OAuth 2.0, e como proteger outros endpoints do servidor caso seja necessário no futuro.

## 📌 Visão geral

O servidor aceita **dois métodos de autenticação simultâneos** no endpoint `/mcp` (protocolo MCP), implementados em `src/middleware/hybrid_verifier.py`:

1. **Token estático (legado)** — o mecanismo atual (`VALID_TOKENS`), usado pelos consumidores internos de hoje (superapp, EAI-engine, etc). Continua funcionando exatamente como sempre funcionou.
2. **JWT via OAuth 2.0 (Keycloak "Identidade Carioca")** — mecanismo novo, para consumidores externos que só falam OAuth 2.0 real, como o Salesforce.

Cada requisição tenta primeiro validar como JWT; se falhar por qualquer motivo (não é JWT, assinatura inválida, `azp` fora da allowlist, ou o Keycloak simplesmente não estiver configurado ainda), o servidor cai automaticamente para a comparação contra `VALID_TOKENS`. Isso significa que **nenhum consumidor atual precisa mudar nada**, e o suporte a OAuth só "liga" quando as variáveis de ambiente do Keycloak forem preenchidas.

```
Authorization: Bearer <token>
        │
        ▼
HybridTokenVerifier (src/middleware/hybrid_verifier.py)
        │
        ├─► KEYCLOAK_ISSUER configurado? ── não ──► pula direto pro fallback
        │           │ sim
        │           ▼
        │   Valida assinatura via JWKS do Keycloak (JWTVerifier nativo do FastMCP)
        │   + confere `iss`, `exp`
        │   + confere `azp` contra KEYCLOAK_TRUSTED_CLIENTS (AzpConstrainedJWTVerifier)
        │           │
        │      ✓ válido ──────────────────► autenticado como "oauth"
        │      ✗ falhou (qualquer motivo)
        ▼
Fallback: compara contra VALID_TOKENS (comportamento de hoje, inalterado)
        │
        ✓ está na lista ──► autenticado como "static"
        ✗ não está ──────► 401
```

Importante: o `FastMCP(auth=...)` em `src/app.py` só exige o token no endpoint `/mcp`. As `custom_route`s Starlette **não são cobertas automaticamente**: as rotas de Dívida Ativa (`/consulta_debitos`, `/emitir_guia`, `/emitir_guia_regularizacao`, `/v2/emitir_guia`, `/v2/emitir_guia_regularizacao`) são protegidas explicitamente com `require_authenticated`, e, além disso, um middleware nega por padrão qualquer outra rota sem token, exceto `/health` e `/health/ready` — ver seção [Protegendo outros endpoints](#-protegendo-outros-endpoints-custom_route) abaixo.

## 🔑 Variáveis de ambiente

### Já existentes (não mudam)

| Variável | Obrigatória | Descrição |
|---|---|---|
| `VALID_TOKENS` | Sim | Tokens estáticos válidos, separados por vírgula. Continua sendo o único mecanismo de auth até o Keycloak ser configurado. |
| `IS_LOCAL` | Não (default `false`) | Quando `true`, desativa toda autenticação (dev local). |
| `DANGEROUSLY_OMIT_AUTH` | Não | Escape hatch de dev local, ver `README.md`. |
| `MCP_STATELESS_HTTP` | Não (default `true` fora do local) | Controla o modo stateless do transporte Streamable HTTP em `/mcp`. Mantenha `true` em ambientes com múltiplas réplicas; use `false` apenas como rollback temporário. |

### Transporte MCP (`/mcp`)

Em ambientes não-locais, o servidor roda o transporte Streamable HTTP em modo stateless por padrão. Isso remove a dependência de manter a sessão MCP em memória no mesmo pod entre `initialize`, `notifications/initialized` e chamadas seguintes.

Não é necessário Redis para esse modo stateless básico. O Redis plug-and-play do FastMCP via `EventStore` deve ser considerado apenas se o servidor passar a depender de resumability/eventos SSE entre reconexões.

### Novas (para habilitar OAuth 2.0 / Keycloak)

| Variável | Obrigatória | Descrição |
|---|---|---|
| `KEYCLOAK_JWKS_URI` | Só se for usar OAuth | URL do endpoint JWKS do Keycloak, usado para validar a assinatura do JWT. Formato padrão: `<issuer>/protocol/openid-connect/certs`. |
| `KEYCLOAK_ISSUER` | Só se for usar OAuth | URL do issuer (realm) do Keycloak. Deve bater exatamente com o claim `iss` dos tokens emitidos. |
| `KEYCLOAK_TRUSTED_CLIENTS` | Recomendado | Lista de `client_id` (claim `azp`) autorizados a autenticar via JWT, separados por vírgula (ex: `salesforce-mcp-client`). **Se deixado vazio, qualquer client autenticado com sucesso contra o realm é aceito** — o servidor loga um warning no startup avisando disso. Sempre preencha assim que o client do Salesforce for criado. |

**Enquanto `KEYCLOAK_JWKS_URI`/`KEYCLOAK_ISSUER` não existirem no ambiente, o comportamento do servidor é 100% idêntico ao atual** (só `VALID_TOKENS`). Não é necessário nenhum novo deploy de código para ativar o OAuth depois — só adicionar essas 3 variáveis no secret do ambiente (Infisical → K8s Secret `mcp-secrets`, mesmo mecanismo já usado por `VALID_TOKENS`/`RMI_OAUTH_*`).

### De onde vêm os valores

- `KEYCLOAK_ISSUER` e `KEYCLOAK_JWKS_URI`: dependem do client OAuth2 (`client_credentials`) que precisa ser criado no Keycloak "Identidade Carioca" pela equipe da IplanRio. O pedido é feito manualmente via Discord IplanRio, canal `#peça-permissão`, informando sistema solicitante, secretaria, escopo e justificativa (mesmo processo documentado publicamente em `mintlify-docs/barramento/auth.mdx`). Confirme com a IplanRio qual realm será usado antes de configurar (há divergência entre a URL documentada internamente e a documentada para parceiros externos — não assuma, pergunte).
- `KEYCLOAK_TRUSTED_CLIENTS`: o `client_id` que a IplanRio atribuir ao client do Salesforce.

## 🤝 Como o Salesforce vai se conectar via OAuth 2.0

Este servidor atua como **Resource Server** (só valida tokens; não emite nem gerencia login). O fluxo esperado é **Client Credentials** (M2M, sem usuário humano):

```
1. IplanRio cria um client confidencial no Keycloak para o Salesforce
   (client_id + client_secret, grant_type=client_credentials)
                    │
2. Do lado do Salesforce (Named Credential / External Credential com
   OAuth 2.0 Client Credentials Flow), configura-se:
     - Token Endpoint URL: <KEYCLOAK_ISSUER>/protocol/openid-connect/token
     - Client ID / Client Secret: os fornecidos pela IplanRio
                    │
3. O Salesforce solicita um access token:

   POST <KEYCLOAK_ISSUER>/protocol/openid-connect/token
   Content-Type: application/x-www-form-urlencoded

   grant_type=client_credentials
   client_id=<client_id_do_salesforce>
   client_secret=<client_secret_do_salesforce>

   ← resposta: { "access_token": "<jwt>", "expires_in": 300, ... }
                    │
4. O Salesforce chama o MCP server usando esse JWT como Bearer token:

   POST https://<host>/mcp
   Authorization: Bearer <jwt>
   Content-Type: application/json
   (corpo: mensagens JSON-RPC do protocolo MCP)
                    │
5. O HybridTokenVerifier deste servidor:
     - busca a chave pública no JWKS do Keycloak (cacheada por 1h)
     - valida assinatura, `iss` e `exp`
     - confere se o `azp` do token está em KEYCLOAK_TRUSTED_CLIENTS
     - se tudo ok → requisição autorizada
```

**Pontos de atenção para quem for configurar no lado do Salesforce:**
- O token expira (`expires_in`, geralmente minutos) — o Salesforce deve renovar automaticamente a cada requisição/lote, como qualquer client OAuth2 padrão faz.
- Este servidor **não implementa** descoberta automática via `.well-known/oauth-protected-resource` (RFC 9728) — foi uma decisão deliberada de manter simples, já que o cadastro no Salesforce é feito colando a URL do token endpoint e as credenciais manualmente, não via discovery automático. Se isso mudar (o conector do Salesforce passar a exigir discovery), avise para reavaliarmos.
- Este servidor **não participa do login/emissão do token** — só valida. Qualquer dúvida sobre a emissão do token (client não reconhecido, erro `invalid_client`, etc.) deve ser tratada com a equipe da IplanRio, dona do Keycloak.

## 🔒 Protegendo outros endpoints (`custom_route`)

O servidor expõe algumas rotas HTTP "puras" fora do protocolo MCP, via `@mcp.custom_route(...)` em `src/app.py`. **Essas rotas não são cobertas automaticamente** pelo `auth=` do `FastMCP`, que só exige o token no endpoint `/mcp`.

As rotas de Dívida Ativa (`/consulta_debitos`, `/emitir_guia`, `/emitir_guia_regularizacao`, `/v2/emitir_guia`, `/v2/emitir_guia_regularizacao`) exigem `Authorization: Bearer` válido desde o achado `vuln-0002` do pentest (INFRAVPIA-320). Sem token, ou com token inválido ou expirado, respondem **401** com `WWW-Authenticate: Bearer`, antes de ler o corpo e sem chamar a PGM. Só `/health` e `/health/ready` continuam públicas (o kubelet não manda token); `/health/detail`, que descreve as dependências do serviço, exige token (CHATR-311, R12).

A proteção é o decorator `require_authenticated` de `src/middleware/http_guard.py`. O provider de auth do FastMCP instala o `AuthenticationMiddleware` do Starlette no app inteiro, com o mesmo `HybridTokenVerifier` do `/mcp`, e deixa o resultado em `request.scope["user"]`; o decorator só exige que ele seja um `AuthenticatedUser`. Nenhuma lógica de token é duplicada, então vale tanto o token estático quanto o JWT do Keycloak. Fora do ambiente local, a ausência de usuário autenticado é sempre 401 (falha fechada); com `IS_LOCAL=true` a rota segue sem auth, como o `/mcp`.

Para proteger outra `custom_route`, aplique o decorator **abaixo** de `@mcp.custom_route(...)` (o decorator de rota registra a função que recebe):

```python
from src.middleware.http_guard import require_authenticated

@mcp.custom_route("/consulta_debitos", methods=["POST"])
@require_authenticated
async def da_consulta_debitos(request: Request) -> JSONResponse:
    ...
```

**Não crie um novo mecanismo de auth para cada rota — sempre reaproveite este mesmo verifier.**

### Nega por padrão: `RequireAuthenticatedByDefault`

O decorator só protege onde foi aplicado: uma `custom_route` nova sem ele nasceria pública, como nasceram as de Dívida Ativa. Por isso, fora do modo local, `src/main.py` monta também o middleware `RequireAuthenticatedByDefault` (mesmo arquivo), que exige o mesmo `AuthenticatedUser` em **toda** requisição HTTP, exceto:

| Rota | Por quê |
|---|---|
| `/health`, `/health/ready` | Probes do kubelet, sem credencial. |
| `/mcp` (e o que estiver abaixo de `/mcp/`) | Autenticado pelo próprio FastMCP, que devolve o 401 no formato da spec do MCP. `/mcp-debug` **não** herda a isenção. |

Fora dessa lista, sem usuário autenticado a resposta é o mesmo 401 do decorator, inclusive para rota inexistente (sem token, nem a existência da rota é revelada). O decorator continua nas rotas de Dívida Ativa como segunda camada; numa rota nova, aplicá-lo segue sendo recomendado para deixar a intenção explícita no código. Uma rota que precise ser pública entra em `PUBLIC_PATHS`, com justificativa — é decisão de revisão, não ajuste solto.

Se, no futuro, for necessário restringir uma rota específica só para OAuth (rejeitando o token estático legado), dá pra checar `request.user.access_token.claims.get("auth_method")` (`"oauth"` ou `"static"`) dentro do handler — mas isso não está implementado hoje; adicione somente se surgir um caso de uso concreto.

## 🧪 Testando localmente sem depender do Keycloak

Não é preciso um Keycloak rodando para testar a validação de JWT. O FastMCP expõe um helper para gerar tokens de teste assinados localmente:

```python
from fastmcp.server.auth.providers.jwt import RSAKeyPair

key_pair = RSAKeyPair.generate()

token = key_pair.create_token(
    issuer="https://fake-issuer.example.com",
    additional_claims={"azp": "salesforce-mcp-client"},
)
```

Veja `src/tests/unit/middleware/test_hybrid_verifier.py` para exemplos completos (JWT válido, expirado, `azp` não autorizado, assinatura inválida, e o fallback estático), todos rodando sem nenhuma chamada de rede real (JWKS é mockado).

## 📎 Referências

- `src/middleware/keycloak_verifier.py` — validação de assinatura + restrição por `azp`.
- `src/middleware/hybrid_verifier.py` — orquestração JWT-ou-estático.
- `src/config/env.py` — declaração das variáveis de ambiente.
- `src/app.py` — onde o `HybridTokenVerifier` é instanciado e passado ao `FastMCP(auth=...)`.
- `mintlify-docs/barramento/auth.mdx` (repo `mintlify-docs`) — documentação pública do fluxo OAuth 2.0 client_credentials e do processo de solicitação de client novo no Keycloak.
