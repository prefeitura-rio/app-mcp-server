"""Exigência de autenticação em TODA rota HTTP, não só em `/mcp`.

O `RequireAuthMiddleware` nativo do FastMCP embrulha apenas a rota do
transporte MCP. Em `fastmcp/server/http.py`, as rotas registradas por
`@mcp.custom_route` entram na aplicação depois, por
`server_routes.extend(server._get_additional_http_routes())`, fora do bloco
que aplica aquele wrapper. E o middleware global que o provider instala
(`AuthenticationMiddleware` do Starlette) apenas *popula* `request.user`: ele
nunca rejeita ninguém.

O resultado é que toda rota de `custom_route` nasce pública — inclusive as
cinco de Dívida Ativa, que consultam débito por CPF/CNPJ e emitem guia de
pagamento. Este middleware inverte o default: **nega por padrão** e libera só
o que está explicitamente na allowlist. Uma `custom_route` acrescentada amanhã
já nasce protegida, sem ninguém precisar lembrar.

O verificador é o mesmo `HybridTokenVerifier` usado em `/mcp`, então não há
uma segunda noção de "token válido" para manter em sincronia.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable, Sequence

from src.utils.log import logger


# Rotas que precisam continuar abertas. Só o liveness e o readiness: o kubelet
# os consulta sem credencial e eles não revelam nada além de "o processo
# responde". `/health/detail`, que desenha o mapa das dependências, fica de
# fora de propósito — passa a exigir token como qualquer outra rota (D-04).
PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/health/ready"})

# Prefixos cuja autenticação é responsabilidade de outra camada.
#
# `/mcp` já é embrulhado pelo `RequireAuthMiddleware` nativo, que devolve o 401
# no formato que a spec do MCP exige (com `WWW-Authenticate` carregando o
# `resource_metadata`). Duplicar a verificação aqui só trocaria essa resposta
# por uma genérica e quebraria a descoberta de OAuth no cliente. Que a rota
# continue de fato embrulhada é garantido por teste — ver
# `src/tests/unit/app/test_http_auth_coverage.py`.
#
# `/.well-known/` é metadado de OAuth (RFC 9728) e é público por definição:
# é o documento que um cliente lê *antes* de ter token. Hoje o provider não
# registra essas rotas (só passaria a registrar se `base_url` fosse
# configurado), mas a isenção fica aqui para que ligar OAuth não reabra o
# problema por um caminho lateral.
DELEGATED_PREFIXES: tuple[str, ...] = ("/mcp", "/.well-known/")

# Rotas que já existiam quando a autenticação passou a ser exigida, e cujos
# consumidores (SFMC, integrações internas) podem ainda estar chamando sem
# credencial. Ficam em OBSERVAÇÃO: a requisição passa, e o servidor registra
# `http_auth_would_deny` com a rota e o IP de origem.
#
# Isto é grandfathering, não uma isenção: a lista existe para encolher. Cada
# nome aqui é um consumidor que ainda precisa ser migrado, e a lista vazia é o
# estado final. Configurável por `HTTP_AUTH_OBSERVE_PATHS` justamente para que
# dê para remover uma rota de cada vez, conforme os consumidores forem
# passando a mandar o token — sem deploy de código.
#
# O que NÃO acontece: uma `custom_route` nova entrar nesta lista sozinha. Ela
# nasce em `enforce`, como qualquer outra. É essa assimetria que faz o gap não
# se reabrir enquanto o legado é migrado.
LEGACY_OBSERVE_PATHS: frozenset[str] = frozenset(
    {
        "/consulta_debitos",
        "/emitir_guia",
        "/emitir_guia_regularizacao",
        "/v2/emitir_guia",
        "/v2/emitir_guia_regularizacao",
    }
)

MODE_ENFORCE = "enforce"
MODE_OBSERVE = "observe"


def _bearer_token(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    """Extrai o token do header `Authorization`, ou None se não houver.

    O esquema é comparado sem diferenciar maiúsculas porque a RFC 6750 o
    define como case-insensitive — recusar `bearer` minúsculo seria rejeitar
    um cliente correto.
    """
    for nome, valor in headers:
        if nome.lower() != b"authorization":
            continue
        try:
            texto = valor.decode("latin-1")
        except UnicodeDecodeError:
            return None
        esquema, _, credencial = texto.partition(" ")
        if esquema.lower() != "bearer":
            return None
        credencial = credencial.strip()
        return credencial or None
    return None


def _client_ip(scope: dict) -> str | None:
    """IP de origem, preferindo `X-Forwarded-For` porque a chamada chega por
    ingress — sem isso, todo evento apontaria para o proxy.

    Só o primeiro salto da cadeia: os demais são informados pelo próprio
    cliente e não valem como identificação.
    """
    for nome, valor in scope.get("headers") or ():
        if nome.lower() == b"x-forwarded-for":
            primeiro = valor.decode("latin-1").split(",")[0].strip()
            if primeiro:
                return primeiro
    client = scope.get("client")
    return client[0] if client else None


async def _responder_401(
    send: Callable[[dict], Awaitable[None]], *, credencial_apresentada: bool
) -> None:
    """401 no formato da RFC 6750, sem revelar por que o token falhou.

    O desafio distingue os dois casos porque a RFC 6750 §3.1 exige: o código
    `error` só entra quando a requisição *trouxe* uma credencial e ela não
    serviu. Requisição sem credencial alguma recebe o desafio nu — anunciar
    `invalid_token` ali diria que um token foi rejeitado, quando nenhum foi
    enviado.

    Não é preciosismo de spec: é o mesmo desafio que o `/mcp` devolve pelo
    middleware nativo do FastMCP, que passou a segui-la na versão 4. Sem esta
    distinção o servidor responderia dois formatos diferentes para a mesma
    pergunta, dependendo de qual rota o cliente tentou.

    O corpo não muda entre os dois casos de propósito: distinguir ali diria a
    quem sonda o servidor se o token enviado chegou a ser avaliado.
    """
    corpo = (
        b'{"error":"invalid_token",'
        b'"error_description":"Token de autorizacao ausente ou invalido."}'
    )
    desafio = b'Bearer error="invalid_token"' if credencial_apresentada else b"Bearer"
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(corpo)).encode()),
                (b"www-authenticate", desafio),
            ],
        }
    )
    await send({"type": "http.response.body", "body": corpo})


class RequireAuthOnAllRoutes:
    """Middleware ASGI puro: exige bearer válido fora da allowlist.

    ASGI puro, e não `BaseHTTPMiddleware`, porque este middleware fica no
    caminho do streaming do transporte MCP: o `BaseHTTPMiddleware` bufferiza a
    resposta e quebraria o SSE.

    Args:
        app: aplicação ASGI encadeada.
        verifier: o mesmo `TokenVerifier` passado ao FastMCP.
        mode: modo global. `"enforce"` (default) faz cada rota usar o seu
            próprio modo; `"observe"` força observação em tudo — válvula de
            emergência, não estado de repouso.
        observe_paths: rotas legadas que passam mesmo sem token, apenas
            registrando. Ver `LEGACY_OBSERVE_PATHS`.
    """

    def __init__(
        self,
        app: Any,
        *,
        verifier: Any,
        mode: str = MODE_ENFORCE,
        observe_paths: Iterable[str] = LEGACY_OBSERVE_PATHS,
        public_paths: Iterable[str] = PUBLIC_PATHS,
        delegated_prefixes: Sequence[str] = DELEGATED_PREFIXES,
    ) -> None:
        self.app = app
        self._verifier = verifier
        self._mode = mode if mode in (MODE_ENFORCE, MODE_OBSERVE) else MODE_ENFORCE
        self._observe_paths = frozenset(observe_paths)
        self._public_paths = frozenset(public_paths)
        self._delegated_prefixes = tuple(delegated_prefixes)

    def _modo_da_rota(self, path: str) -> str:
        """Resolve o modo desta rota.

        O modo global em `observe` vale para tudo — é a válvula de emergência,
        para quando se descobre em produção que a exigência quebrou algo que
        ninguém previu. Fora dele, só as rotas legadas observam.
        """
        if self._mode == MODE_OBSERVE:
            return MODE_OBSERVE
        return MODE_OBSERVE if path in self._observe_paths else MODE_ENFORCE

    def _isento(self, path: str) -> bool:
        if path in self._public_paths:
            return True
        return any(path.startswith(prefixo) for prefixo in self._delegated_prefixes)

    async def _autenticado(self, token: str | None) -> bool:
        if token is None:
            return False
        try:
            return await self._verifier.verify_token(token) is not None
        except Exception:
            # Falha ao verificar (JWKS fora do ar, chave malformada) é negação.
            # O `JWTVerifier` nativo já engole os erros dele e devolve None, então
            # chegar aqui é anômalo e merece registro — mas nunca vira acesso
            # concedido.
            logger.exception("Falha ao verificar token; negando por precaução")
            return False

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if self._isento(path):
            return await self.app(scope, receive, send)

        token = _bearer_token(scope.get("headers") or ())
        if await self._autenticado(token):
            return await self.app(scope, receive, send)

        modo = self._modo_da_rota(path)

        # Nunca o token, nem parte dele. O que o operador precisa para migrar um
        # consumidor é qual rota foi chamada, se veio credencial e de onde veio
        # a chamada — o IP aqui é o de egresso do sistema consumidor, não de um
        # cidadão.
        evento = {
            "event": (
                "http_auth_denied" if modo == MODE_ENFORCE else "http_auth_would_deny"
            ),
            "path": path,
            "method": scope.get("method"),
            "token_presente": token is not None,
            "client_ip": _client_ip(scope),
        }
        logger.warning(evento)

        if modo == MODE_OBSERVE:
            return await self.app(scope, receive, send)
        return await _responder_401(send, credencial_apresentada=token is not None)
