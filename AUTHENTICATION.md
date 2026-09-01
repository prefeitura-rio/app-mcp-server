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

Importante: essa validação roda apenas no pipeline de mensagens do protocolo MCP (`/mcp`), aplicado via `FastMCP(auth=...)` em `src/app.py`. As `custom_route`s Starlette (`/health`, `/consulta_debitos`, `/emitir_guia`, `/emitir_guia_regularizacao`) **não são cobertas automaticamente** por essa proteção — ver seção [Protegendo outros endpoints](#-protegendo-outros-endpoints-custom_route) abaixo.

## 🔑 Variáveis de ambiente

### Já existentes (não mudam)

| Variável | Obrigatória | Descrição |
|---|---|---|
| `VALID_TOKENS` | Sim | Tokens estáticos válidos, separados por vírgula. Continua sendo o único mecanismo de auth até o Keycloak ser configurado. |
| `IS_LOCAL` | Não (default `false`) | Quando `true`, desativa toda autenticação (dev local). |
| `DANGEROUSLY_OMIT_AUTH` | Não | Escape hatch de dev local, ver `README.md`. |
| `MCP_STATELESS_HTTP` | Não (default `true` fora do local) | Controla o modo stateless do transporte Streamable HTTP em `/mcp`. Mantenha `true` em ambientes com múltiplas réplicas; use `false` apenas como rollback temporário. |
| `HTTP_AUTH_MODE` | Não (default `enforce`) | `enforce` faz cada rota usar o próprio modo; `observe` força observação em tudo (válvula de emergência). Ver a seção de rollout abaixo. |
| `HTTP_AUTH_OBSERVE_PATHS` | Não | CSV das rotas que passam sem token, apenas registrando. Não configurado = as cinco rotas legadas de Dívida Ativa. `""` = exigir token em todas. |
| `INTERNAL_TLS_VERIFY` | Não (default `false`) | Verificação de certificado TLS nas chamadas à PGM e ao IPTU via chatbot-integrations. `false` preserva o comportamento atual; vire para `true` quando o CA bundle interno estiver no container. |
| `MAX_REQUEST_BODY_BYTES` | Não (default `1048576`) | Teto do corpo de requisição, em bytes. Acima disso o servidor responde 413. |

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

## 🔒 Autenticação nas rotas HTTP fora do `/mcp`

O servidor expõe rotas HTTP "puras" fora do protocolo MCP, via
`@mcp.custom_route(...)` em `src/app.py` (`/health*`, `/consulta_debitos`,
`/emitir_guia*`, `/v2/emitir_guia*`). **O `auth=` do `FastMCP` não cobre essas
rotas**: em `fastmcp/server/http.py`, o `RequireAuthMiddleware` embrulha só o
endpoint do transporte MCP, e as rotas de `custom_route` são anexadas depois,
fora daquele wrapper. O middleware global que o provider instala
(`AuthenticationMiddleware` do Starlette) apenas *popula* `request.user` — ele
nunca rejeita ninguém.

Até agosto/2026 isso significava que `/consulta_debitos` e as quatro rotas de
emissão de guia atendiam **sem nenhuma autenticação**. Corrigido por
`src/middleware/require_auth.py`.

### Como funciona agora

`RequireAuthOnAllRoutes` é um middleware ASGI aplicado à aplicação inteira por
`build_http_middleware()` (`src/app.py`), consumido por `src/main.py`. Ele
**nega por padrão** e libera só o que está na allowlist:

| Rota | Tratamento |
|---|---|
| `/health`, `/health/ready` | Abertas. São as probes do kubelet, que não têm credencial — um 401 aqui derruba o pod. |
| `/health/detail` | **Passou a exigir token.** Desenha o mapa das dependências e não é probe. |
| `/mcp` | Isenta *deste* middleware, porque o `RequireAuthMiddleware` nativo já a protege e devolve o 401 no formato que a spec do MCP exige (com `resource_metadata` no `WWW-Authenticate`). Verificar de novo aqui quebraria a descoberta de OAuth no cliente. |
| `/.well-known/*` | Aberta por definição (RFC 9728): é o documento que um cliente lê *antes* de ter token. |
| Qualquer outra | Exige `Authorization: Bearer <token>` válido. |

O verificador é o mesmo `HybridTokenVerifier` de `/mcp`, então token estático e
JWT do Keycloak funcionam igualmente e não há uma segunda noção de "token
válido" para manter em sincronia.

**Por que middleware e não um decorator por rota.** A versão anterior deste
documento propunha um `@require_authenticated` aplicado rota a rota. O problema
é que ele exige que alguém *lembre* de aplicá-lo — que é exatamente a falha que
deixou cinco rotas abertas. Com o middleware, uma `custom_route` nova nasce
protegida, e é preciso um ato deliberado (acrescentar à allowlist) para
publicá-la. `src/tests/unit/app/test_http_auth_coverage.py` enumera a tabela de
rotas da aplicação montada e falha se alguma responder sem token.

### Rollout: grandfathering das rotas legadas

Exigir token de todo mundo de uma vez quebraria os consumidores que hoje chamam
`/consulta_debitos` e `/emitir_guia*` sem credencial. Por isso a exigência é
**por rota**, não global:

| Rota | Modo | Efeito |
|---|---|---|
| As cinco de Dívida Ativa | `observe` | Passa sem token. Registra `http_auth_would_deny` com rota, método, se veio token e o IP de origem. |
| Todas as outras, inclusive as que ainda não existem | `enforce` | 401 sem token válido. |

Essa assimetria é o ponto. Um `observe` global seria mais simples e devolveria o
problema: rota nova voltaria a nascer aberta. Aqui, o grandfathering vale para
os cinco nomes escritos em `LEGACY_OBSERVE_PATHS` — e para mais nada.

**A lista existe para encolher.** Cada entrada é um consumidor a migrar, e a
lista vazia é o estado final.

```env
# Ainda não configurado = as cinco rotas de Dívida Ativa (default do código).

# Uma rota já migrada sai da lista, sem deploy:
HTTP_AUTH_OBSERVE_PATHS="/consulta_debitos,/emitir_guia,/emitir_guia_regularizacao"

# Estado final: token em todas.
HTTP_AUTH_OBSERVE_PATHS=""
```

O ciclo de migração é: ler os `http_auth_would_deny` de uma rota; identificar o
consumidor pelo `client_ip`; fazê-lo mandar o `Authorization`; confirmar que os
eventos pararam; tirar a rota da variável. Um teste
(`test_a_lista_de_legado_nao_cresceu`) falha se alguém aumentar a lista, para
que acrescentar uma isenção seja um ato revisado e não um jeito de calar um
teste vermelho.

#### Válvula de emergência: `HTTP_AUTH_MODE`

| Valor | Comportamento |
|---|---|
| `enforce` (default) | Cada rota usa o próprio modo, conforme a tabela acima. |
| `observe` | Força observação em **tudo**, inclusive rotas novas. |

Serve para o caso de a exigência quebrar em produção algo que ninguém previu:
vira a variável, o tráfego volta, e o log continua mostrando o que seria
recusado. Não é estado de repouso.

> Nada disto afrouxa `/mcp`, que segue exigindo token pelo wrapper nativo desde
> sempre. O grandfathering só alcança rotas que já atendiam sem autenticação
> nenhuma — ele não abre nada que estivesse fechado.

### Teto de corpo de requisição

`src/middleware/body_limit.py`, aplicado pelo mesmo `build_http_middleware()`,
recusa com **413** corpo acima de `MAX_REQUEST_BODY_BYTES` (default 1 MiB).
Os handlers fazem `await request.json()`, que carrega o corpo inteiro em
memória — sem teto, um POST grande em rota sem auth vira OOMKill do pod, que em
produção roda com `replicas: 1`. São dois caminhos: `Content-Length` acima do
teto é recusado antes de ler um byte; sem `Content-Length` (chunks), a contagem
corta durante a leitura, que é o que de fato limita a memória.

### Restringir uma rota só para OAuth

Se no futuro for preciso rejeitar o token estático numa rota específica, dá para
checar `get_access_token().claims.get("auth_method")` (`"oauth"` ou `"static"`)
dentro do handler. Não está implementado — acrescente só com caso de uso
concreto. **Não crie um mecanismo de auth novo por rota; sempre reaproveite o
mesmo verifier.**

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
