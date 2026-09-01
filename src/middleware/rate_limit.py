"""Teto de taxa nas chamadas MCP, por identidade.

Usa o `RateLimitingMiddleware` do FastMCP — token bucket, documentado em
https://gofastmcp.com/servers/middleware. Não há implementação própria de
algoritmo aqui: o que este módulo acrescenta é a **chave** e a **telemetria**.

**Por que a chave importa mais que o número.** O modo de uso previsto é uma
rajada de cidadãos distintos perguntando a mesma coisa — 500 por segundo, todos
servidos do cache a partir do primeiro. Um limite global cortaria exatamente
esse cenário: seria o pico legítimo batendo no teto e derrubando quem chegasse
depois. Um limite por identidade não vê rajada nenhuma, porque cada um tem o
próprio balde. É por isso que `global_limit` fica em `False` e a chave é
obrigatória.

A chave é, em ordem:

    user:<user_id>    o argumento `user_id` da tool, quando a chamada tem um
    cred:<subject>    o `sub` (ou `client_id`) do token autenticado
    ip:<origem>       o IP de origem, para o que não tem nem um nem outro
    anonimo           último recurso; só alcançável fora do caminho HTTP

`user_id` vem do cliente e não é verificado contra o token (é achado conhecido
deste servidor). Isso limita o que este teto promete: ele contém o consumidor
que entra em laço, não o adversário que rotaciona `user_id` de propósito. Quem
fecha esse buraco é o vínculo entre `user_id` e a identidade do token, que é
outro trabalho — o teto por credencial abaixo dele existiria para isso, e fica
de fora enquanto não houver número real de produção para dimensioná-lo sem
cortar o pico legítimo.

**Cobertura.** Middleware de FastMCP roda no caminho do protocolo MCP, ou seja,
em `/mcp`. As rotas de `@mcp.custom_route` (as cinco de Dívida Ativa) entram na
aplicação fora dessa cadeia — o mesmo motivo pelo qual `RequireAuthOnAllRoutes`
precisou ser ASGI puro. Elas seguem sem teto de taxa.
"""

from __future__ import annotations

from typing import Any

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware.rate_limiting import (
    RateLimitError,
    RateLimitingMiddleware,
)

from src.utils.log import logger


def _do_token() -> str | None:
    """Identidade do token autenticado, sem deixar o token vazar para a chave.

    `subject` é o `sub` do JWT do Keycloak; `client_id` cobre o token estático
    de dev/homologação, onde não há `sub`.
    """
    try:
        token = get_access_token()
    except Exception:
        # Fora de requisição HTTP não há token a resolver. Não é erro: só
        # significa que a chave tem de vir de outro lugar.
        return None
    if token is None:
        return None
    return token.subject or token.client_id or None


def _do_argumento(context: Any) -> str | None:
    """`user_id` dos argumentos da chamada, quando a chamada os tem.

    Vale para `tools/call` e `prompts/get`, que são os que carregam
    `arguments`. Os demais métodos (`initialize`, `tools/list`, ...) caem na
    credencial, que é o certo: não pertencem a cidadão nenhum.
    """
    argumentos = getattr(getattr(context, "message", None), "arguments", None)
    if not isinstance(argumentos, dict):
        return None
    user_id = argumentos.get("user_id")
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()
    return None


def _do_ip(context: Any) -> str | None:
    """IP de origem, pelo mesmo critério de `require_auth._client_ip`."""
    contexto_http = getattr(context, "fastmcp_context", None)
    if contexto_http is None:
        return None
    try:
        request = contexto_http.get_http_request()
    except Exception:
        return None
    encaminhado = request.headers.get("x-forwarded-for")
    if encaminhado:
        primeiro = encaminhado.split(",")[0].strip()
        if primeiro:
            return primeiro
    return getattr(getattr(request, "client", None), "host", None)


def chave_da_requisicao(context: Any) -> str:
    """Identidade que ganha um balde próprio. Ver o cabeçalho do módulo."""
    user_id = _do_argumento(context)
    if user_id:
        return f"user:{user_id}"
    subject = _do_token()
    if subject:
        return f"cred:{subject}"
    ip = _do_ip(context)
    if ip:
        return f"ip:{ip}"
    return "anonimo"


class RateLimitPorIdentidade(RateLimitingMiddleware):
    """`RateLimitingMiddleware` que registra o estouro antes de recusar.

    O `on_request` é reescrito em vez de embrulhado porque embrulhar exigiria
    capturar `RateLimitError` em volta de `call_next`, e aí um erro vindo de
    baixo viraria log de estouro deste teto. São as mesmas quatro linhas da
    classe base, usando os mesmos `limiters` dela.

    Sem o evento não há como operar o teto: a diferença entre "ninguém bate no
    limite" e "o limite está cortando gente" fica invisível, e a única
    evidência seria o cliente reclamando.
    """

    async def on_request(self, context: Any, call_next: Any) -> Any:
        chave = await self._get_client_identifier(context)
        if not await self.limiters[chave].consume():
            logger.warning(
                {
                    "event": "mcp_rate_limited",
                    "chave": chave,
                    "method": getattr(context, "method", None),
                    "limite_rps": self.max_requests_per_second,
                    "burst": self.burst_capacity,
                    "baldes_ativos": len(self.limiters),
                }
            )
            raise RateLimitError(f"Rate limit exceeded for client: {chave}")
        return await call_next(context)


def montar_rate_limit(
    *, rps: float, burst: int | None = None
) -> RateLimitPorIdentidade | None:
    """Instância pronta, ou `None` quando o teto está desligado (`rps <= 0`).

    Desligar é uma posição legítima: é o estado de hoje, e ligar um teto sem
    número de produção para calibrá-lo troca um risco por outro.
    """
    if rps <= 0:
        return None
    return RateLimitPorIdentidade(
        max_requests_per_second=rps,
        burst_capacity=burst or None,
        get_client_id=chave_da_requisicao,
        global_limit=False,
    )
