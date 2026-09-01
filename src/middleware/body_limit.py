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

O caminho 2 não pode depender do handler. Os cinco handlers de Dívida Ativa
embrulham o `await request.json()` em `except Exception` e devolvem 500 —
engoliam o sentinel deste módulo e o 413 nunca saía, nem o log. Por isso a
proteção está em dois pontos independentes: o sentinel herda de
`BaseException`, fora do alcance de um `except Exception`, e o `send`
embrulhado ainda troca por 413 qualquer resposta que um handler tente emitir
depois de o teto estourar. Um handler novo nasce com teto sem precisar saber
que este middleware existe.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from src.utils.log import logger


class _CorpoGrandeDemais(BaseException):
    """Sinaliza estouro do teto de dentro do `receive` embrulhado.

    Herda de `BaseException` de propósito: `except Exception` — o que todo
    handler deste servidor usa em volta do `await request.json()` — não o pega,
    então o sentinel chega até aqui em vez de virar 500 no handler.
    """


def _achar_sentinel(erro: BaseException) -> bool:
    """O sentinel pode chegar embrulhado em `BaseExceptionGroup`.

    Quem atravessa um task group do anyio — o transporte MCP em streaming, por
    exemplo — entrega as exceções das tarefas filhas dentro de um grupo. Um
    `except _CorpoGrandeDemais` seco não pegaria, e a exceção vazaria para o
    servidor ASGI com a requisição sem resposta.
    """
    if isinstance(erro, _CorpoGrandeDemais):
        return True
    if isinstance(erro, BaseExceptionGroup):
        return any(_achar_sentinel(sub) for sub in erro.exceptions)
    return False


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

    def _logar_estouro(self, scope: dict, motivo: str) -> None:
        logger.warning(
            {
                "event": "request_body_too_large",
                "path": scope.get("path"),
                "method": scope.get("method"),
                "motivo": motivo,
                "limite_bytes": self._max_bytes,
            }
        )

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        if self._declarado_acima_do_teto(scope.get("headers")):
            self._logar_estouro(scope, "content_length")
            return await _responder_413(send, self._max_bytes)

        lidos = 0
        estourou = False
        handler_respondeu = False
        trocado_por_413 = False

        async def receive_contando() -> dict:
            nonlocal lidos, estourou
            if estourou:
                # O handler engoliu o sentinel e voltou a pedir corpo. Não
                # entregamos mais nada: parar de ler é a propriedade que este
                # middleware existe para garantir.
                raise _CorpoGrandeDemais
            mensagem = await receive()
            if mensagem.get("type") == "http.request":
                lidos += len(mensagem.get("body") or b"")
                if lidos > self._max_bytes:
                    estourou = True
                    # Logado aqui, e não no `except`, para o evento sair mesmo
                    # que alguém no meio do caminho capture o sentinel.
                    self._logar_estouro(scope, "streaming")
                    raise _CorpoGrandeDemais
            return mensagem

        async def send_filtrando(mensagem: dict) -> None:
            nonlocal handler_respondeu, trocado_por_413
            if trocado_por_413:
                # Resto da resposta do handler, depois de já termos respondido
                # 413 no lugar dela.
                return
            if estourou and not handler_respondeu:
                # O handler tenta responder algo sobre um corpo que nunca
                # terminou de chegar — 500, 200, tanto faz. Troca por 413.
                trocado_por_413 = True
                await _responder_413(send, self._max_bytes)
                return
            if mensagem.get("type") == "http.response.start":
                handler_respondeu = True
            await send(mensagem)

        try:
            await self.app(scope, receive_contando, send_filtrando)
        except BaseException as erro:  # noqa: BLE001
            # Só o estouro de corpo para aqui; todo o resto segue subindo.
            if not _achar_sentinel(erro):
                raise
            if isinstance(erro, BaseExceptionGroup):
                _, resto = erro.split(_CorpoGrandeDemais)
                if resto is not None:
                    # A requisição já vai ser respondida com 413; o que veio
                    # junto no grupo é consequência do aborto da leitura, mas
                    # some do traceback se não for registrado aqui.
                    logger.warning(
                        {
                            "event": "request_body_too_large_com_erro_junto",
                            "path": scope.get("path"),
                            "erros": [repr(sub) for sub in resto.exceptions],
                        }
                    )

        if estourou and not trocado_por_413 and not handler_respondeu:
            await _responder_413(send, self._max_bytes)
        # `handler_respondeu` sem `trocado_por_413` é o caso em que a resposta
        # começou a sair antes do estouro: o status já foi para o cliente e não
        # dá para trocar. A leitura parou de qualquer forma, que é o que limita
        # a memória.
