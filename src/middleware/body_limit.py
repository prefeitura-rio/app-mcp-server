"""Teto para o tamanho do corpo de requisição.

Os handlers HTTP fazem `await request.json()` direto, e o Starlette carrega o
corpo inteiro em memória antes de devolvê-lo. Sem teto, um POST de algumas
centenas de MB vira memória do pod — e o limite do container é 1536Mi, com
`replicas: 1` em produção. Poucas requisições concorrentes bastam para o
OOMKill.

São dois caminhos, porque um cliente hostil não é obrigado a declarar o
tamanho:

1. `Content-Length` presente e acima do teto: recusa antes de ler um byte.
2. Sem `Content-Length` (transferência em chunks): conta enquanto lê e aborta
   ao ultrapassar. É o caminho que de fato limita a memória, já que o header
   é só uma promessa do cliente.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from src.utils.log import logger


class _CorpoGrandeDemais(Exception):
    """Sinaliza estouro do teto de dentro do `receive` embrulhado."""


async def _responder_413(send: Callable[[dict], Awaitable[None]], limite: int) -> None:
    corpo = (
        b'{"error":"payload_too_large",'
        b'"error_description":"Corpo da requisicao acima do limite de '
        + str(limite).encode()
        + b' bytes."}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(corpo)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": corpo})


class LimitRequestBodyMiddleware:
    """Middleware ASGI puro que recusa corpo acima de `max_bytes`.

    ASGI puro pelo mesmo motivo de `RequireAuthOnAllRoutes`: fica no caminho do
    streaming do transporte MCP, que o `BaseHTTPMiddleware` quebraria.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self._max_bytes = max_bytes

    def _declarado_acima_do_teto(self, headers: Any) -> bool:
        for nome, valor in headers or ():
            if nome.lower() != b"content-length":
                continue
            try:
                return int(valor) > self._max_bytes
            except (TypeError, ValueError):
                # `Content-Length` ilegível não é motivo para recusar aqui: a
                # contagem no `receive` cobre o caso, e o servidor HTTP à frente
                # já rejeita header malformado.
                return False
        return False

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        if self._declarado_acima_do_teto(scope.get("headers")):
            logger.warning(
                {
                    "event": "request_body_too_large",
                    "path": scope.get("path"),
                    "method": scope.get("method"),
                    "motivo": "content_length",
                    "limite_bytes": self._max_bytes,
                }
            )
            return await _responder_413(send, self._max_bytes)

        lidos = 0
        resposta_iniciada = False

        async def receive_contando() -> dict:
            nonlocal lidos
            mensagem = await receive()
            if mensagem.get("type") == "http.request":
                lidos += len(mensagem.get("body") or b"")
                if lidos > self._max_bytes:
                    raise _CorpoGrandeDemais
            return mensagem

        async def send_marcando(mensagem: dict) -> None:
            nonlocal resposta_iniciada
            if mensagem.get("type") == "http.response.start":
                resposta_iniciada = True
            await send(mensagem)

        try:
            return await self.app(scope, receive_contando, send_marcando)
        except _CorpoGrandeDemais:
            logger.warning(
                {
                    "event": "request_body_too_large",
                    "path": scope.get("path"),
                    "method": scope.get("method"),
                    "motivo": "streaming",
                    "limite_bytes": self._max_bytes,
                }
            )
            if resposta_iniciada:
                # O handler já começou a responder; não dá para trocar o status.
                # A leitura parou, que é a propriedade que importa: a memória
                # está limitada de qualquer forma.
                return None
            return await _responder_413(send, self._max_bytes)
