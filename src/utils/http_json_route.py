"""Execução padronizada das rotas HTTP próprias (`@mcp.custom_route`) que recebem JSON.

As rotas de Dívida Ativa repetiam o mesmo bloco: ler o corpo, chamar a função
de negócio e, em qualquer exceção, responder 500 com `str(e)`. Isso fazia um
erro de entrada do cliente (corpo que não é JSON) virar erro de servidor e
repassava ao chamador o texto interno da exceção Python (DEV-240).

Aqui:

- corpo ausente, que não é JSON ou que não é um objeto JSON é entrada inválida
  do cliente: HTTP 400 com mensagem controlada;
- exceção inesperada da função de negócio é HTTP 500 com mensagem genérica, e o
  log leva só a rota e o tipo da exceção (o texto dela pode carregar dado do
  contribuinte ou de uma dependência);
- o resultado da função de negócio segue com HTTP 200, como antes.

O corpo de erro segue o padrão que os consumidores já tratam na v2
(`api_resposta_sucesso: false` com `api_descricao_erro`) e mantém a chave
`error`, que era a única até aqui, com o mesmo texto controlado.
"""

from typing import Any, Awaitable, Callable, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from src.utils.log import logger

MENSAGEM_CORPO_NAO_JSON = (
    "Corpo da requisição inválido: envie um objeto JSON "
    "(Content-Type: application/json)."
)
MENSAGEM_CORPO_NAO_OBJETO = (
    "Corpo da requisição inválido: o JSON enviado deve ser um objeto "
    "com os parâmetros da operação."
)
MENSAGEM_ERRO_INTERNO = (
    "Erro interno ao processar a requisição. Tente novamente mais tarde."
)

FuncaoDeNegocio = Callable[[Dict[str, Any]], Awaitable[Any]]


def resposta_de_erro(mensagem: str, status_code: int) -> JSONResponse:
    """Corpo de erro das rotas: padrão da v2 mais a chave `error` legada."""
    return JSONResponse(
        content={
            "api_resposta_sucesso": False,
            "api_descricao_erro": mensagem,
            "error": mensagem,
        },
        status_code=status_code,
    )


async def executar_rota_json(request: Request, funcao: FuncaoDeNegocio) -> JSONResponse:
    """Lê o corpo JSON, chama `funcao` com ele e traduz falhas em 400 ou 500."""
    rota = request.url.path

    try:
        parameters = await request.json()
    except ValueError:
        # `json.JSONDecodeError` e `UnicodeDecodeError` são `ValueError`:
        # corpo vazio, texto que não é JSON ou bytes que não são texto.
        logger.warning(
            {"event": "http_route_invalid_body", "route": rota, "motivo": "nao_json"}
        )
        return resposta_de_erro(MENSAGEM_CORPO_NAO_JSON, 400)
    except Exception as e:
        logger.error(
            {
                "event": "http_route_body_error",
                "route": rota,
                "error_type": type(e).__name__,
            }
        )
        return resposta_de_erro(MENSAGEM_ERRO_INTERNO, 500)

    if not isinstance(parameters, dict):
        logger.warning(
            {
                "event": "http_route_invalid_body",
                "route": rota,
                "motivo": "nao_objeto",
                "tipo_json": type(parameters).__name__,
            }
        )
        return resposta_de_erro(MENSAGEM_CORPO_NAO_OBJETO, 400)

    try:
        result = await funcao(parameters)
    except Exception as e:
        logger.error(
            {
                "event": "http_route_error",
                "route": rota,
                "error_type": type(e).__name__,
            }
        )
        return resposta_de_erro(MENSAGEM_ERRO_INTERNO, 500)

    return JSONResponse(content=result, status_code=200)
