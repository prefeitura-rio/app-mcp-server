"""
Emissão de guias de dívida ativa — v2.

Diferenças em relação à v1 (`src/tools/divida_ativa.py`):

- A entrada é validada por Pydantic antes de qualquer uso; não há
  `ast.literal_eval` nem `float()` sobre o payload cru.
- Falha de validação nunca resulta em chamada à PGM. A v1 retorna
  ``{"opcao_invalida": True}`` no except (divida_ativa.py:290), valor truthy
  que passa pelo guard ``if not entrada`` e deixa a PGM ser chamada com
  payload inválido.
- A saída é um modelo tipado (`EmitirGuiaResponse`), preservando o mesmo
  contrato JSON da v1.
"""

from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from src.tools.divida_ativa import log_execution_time, pgm_api
from src.tools.divida_ativa_v2.models import EmitirGuiaRequest, EmitirGuiaResponse
from src.utils.error_interceptor import interceptor, send_general_error
from src.utils.log import logger

ENDPOINT_A_VISTA = "v2/guiapagamento/emitir/avista"
ENDPOINT_REGULARIZACAO = "v2/guiapagamento/emitir/regularizacao"

CONSUMIDOR_A_VISTA = "emitir-guia-vista"
CONSUMIDOR_REGULARIZACAO = "emitir-guia-regularizacao"

SOURCE_V2 = {"source": "mcp", "tool": "divida_ativa_v2"}

MENSAGEM_SELECAO_VAZIA = (
    "Nenhum item válido selecionado. Verifique se 'itens_informados'/"
    "'apenas_um_item' correspondem a chaves de 'dicionario_itens' e se o "
    "identificador consta nas listas enviadas."
)


async def _reportar_falha_validacao(
    tipo_erro: str, tipo: str, descricao: str, parameters: Any
) -> None:
    """
    Reporta uma falha de validação ao error interceptor.

    Falha de validação não levanta exceção (o contrato é HTTP 200 com
    `api_resposta_sucesso=false`), então o decorator `@interceptor` nunca
    dispara para esses casos. Sem este report a v2 ficaria muda justamente
    para os padrões que motivaram o CHATR-120 — e é por ela que se acompanha
    se o SFMC parou de enviar placeholder.

    `send_general_error` já redige PII (CPF/telefone) do `input_body` e nunca
    propaga exceção, então é seguro passar o payload cru.
    """
    user_id = "unknown"
    if isinstance(parameters, dict):
        user_id = str(parameters.get("user_id", "unknown"))

    await send_general_error(
        user_id=user_id,
        source={**SOURCE_V2, "function": f"emitir_guia_{tipo}_v2"},
        error_type=tipo_erro,
        error_message=descricao,
        input_body=parameters,
    )


def formatar_erro_validacao(erro: ValidationError) -> str:
    """Converte um ValidationError do Pydantic em mensagem legível em PT-BR."""
    partes: List[str] = []
    for detalhe in erro.errors():
        campo = ".".join(str(item) for item in detalhe["loc"]) or "payload"
        mensagem = detalhe["msg"]
        # Pydantic prefixa erros de ValueError com "Value error, "
        mensagem = mensagem.removeprefix("Value error, ")
        partes.append(f"{campo}: {mensagem}")

    return "Parâmetros inválidos. " + " | ".join(partes)


def _resolver_sequencial(
    requisicao: EmitirGuiaRequest, sequencial: str, tipo: str
) -> Optional[Tuple[str, str]]:
    """
    Resolve um sequencial escolhido para ``(campo, identificador)``.

    Retorna ``None`` quando o sequencial não consta em `dicionario_itens` ou
    quando o identificador não aparece em nenhuma das listas do tipo pedido.
    """
    valor = requisicao.dicionario_itens.get(sequencial)
    if valor is None:
        return None

    if tipo == "a_vista":
        if valor in requisicao.lista_cdas:
            return "cdas", valor
        if valor in requisicao.lista_efs:
            return "efs", valor
        return None

    if valor in requisicao.lista_guias:
        return "guias", valor
    return None


def montar_parametros_entrada(
    requisicao: EmitirGuiaRequest, tipo: str
) -> Dict[str, Any]:
    """
    Monta o payload enviado à PGM a partir da requisição validada.

    Espelha a lógica de divida_ativa.py:263-280, mas sobre dados já validados.
    Sequenciais que não resolvem são descartados (como na v1) e registrados em
    log; cabe ao chamador recusar a emissão quando nada resolveu.
    """
    encontrados: Dict[str, List[str]] = {"cdas": [], "efs": [], "guias": []}
    descartados: List[str] = []

    for sequencial in requisicao.sequenciais_escolhidos():
        resolvido = _resolver_sequencial(requisicao, sequencial, tipo)
        if resolvido is None:
            descartados.append(sequencial)
            continue
        campo, valor = resolvido
        encontrados[campo].append(valor)

    # Descarte parcial não impede a emissão, mas precisa ser visível: é o
    # sintoma de dicionario_itens fora de sincronia com as listas enviadas.
    if descartados:
        logger.warning(
            {
                "event": "emitir_guia_v2_sequenciais_descartados",
                "tipo": tipo,
                "sequenciais": descartados,
            }
        )

    parametros: Dict[str, Any] = {"origem_solicitação": 0}
    if tipo == "a_vista":
        parametros.update({"cdas": encontrados["cdas"], "efs": encontrados["efs"]})
    else:
        parametros.update({"guias": encontrados["guias"]})

    return parametros


def _extrair_dados_guia(registros: Any) -> Dict[str, str]:
    """
    Extrai os campos da guia emitida a partir da resposta da PGM.

    O contrato de resposta comporta uma única guia, então o último registro
    vence — mesma semântica da v1 (divida_ativa.py:320-324). Se vier mais de
    um, os anteriores são perdidos, e isso é registrado em log.
    """
    dados: Dict[str, str] = {}
    encontrados = 0

    for item in registros:
        if not isinstance(item, dict):
            continue
        encontrados += 1
        dados["codigo_de_barras"] = item.get("codigoDeBarras") or ""
        dados["link"] = item.get("pdf") or ""
        dados["data_vencimento"] = item.get("dataVencimento") or ""
        dados["pix"] = item.get("codigoQrEMVPix") or ""

    if encontrados > 1:
        logger.warning(
            {
                "event": "emitir_guia_v2_multiplos_registros",
                "registros": encontrados,
                "detalhe": "apenas o último registro é retornado ao consumidor",
            }
        )

    return dados


def _identificadores_selecionados(
    parametros_entrada: Dict[str, Any], tipo: str
) -> List[str]:
    """Identificadores que serão efetivamente enviados à PGM."""
    if tipo == "a_vista":
        return parametros_entrada.get("cdas", []) + parametros_entrada.get("efs", [])
    return parametros_entrada.get("guias", [])


async def _emitir(parameters: Dict[str, Any], tipo: str) -> EmitirGuiaResponse:
    """Valida a entrada, chama a PGM e monta a resposta tipada."""
    endpoint, consumidor = _rota_da_pgm(tipo)

    try:
        requisicao = EmitirGuiaRequest.model_validate(parameters or {})
    except ValidationError as erro:
        descricao = formatar_erro_validacao(erro)
        logger.error(
            {
                "event": "emitir_guia_v2_validation_error",
                "tipo": tipo,
                "erro": descricao,
            }
        )
        await _reportar_falha_validacao("ValidationError", tipo, descricao, parameters)
        return EmitirGuiaResponse.de_erro(descricao)

    parametros_entrada = montar_parametros_entrada(requisicao, tipo)

    # Um payload sintaticamente válido ainda pode não selecionar nada (chave
    # ausente em dicionario_itens, identificador fora das listas, ou campo
    # renomeado absorvido por extra="ignore"). Chamar a PGM com listas vazias
    # só transforma um erro de entrada em erro remoto.
    if not _identificadores_selecionados(parametros_entrada, tipo):
        logger.error(
            {
                "event": "emitir_guia_v2_selecao_vazia",
                "tipo": tipo,
                "parametros_entrada": parametros_entrada,
            }
        )
        await _reportar_falha_validacao(
            "SelecaoVaziaError", tipo, MENSAGEM_SELECAO_VAZIA, parameters
        )
        return EmitirGuiaResponse.de_erro(MENSAGEM_SELECAO_VAZIA)

    logger.info(
        {
            "event": "emitir_guia_v2_started",
            "tipo": tipo,
            "parametros_entrada": parametros_entrada,
        }
    )

    registros = await pgm_api(
        endpoint=endpoint, consumidor=consumidor, data=parametros_entrada
    )

    if isinstance(registros, dict) and "erro" in registros:
        return EmitirGuiaResponse.de_erro(registros.get("motivos") or "Erro na PGM")

    # Campos passados explicitamente: `**parametros_entrada` contra o
    # extra="forbid" de EmitirGuiaResponse transformaria uma guia emitida com
    # sucesso em erro caso o payload da PGM ganhasse qualquer chave nova.
    dados_guia = _extrair_dados_guia(registros)
    return EmitirGuiaResponse(
        api_resposta_sucesso=True,
        origem_solicitação=parametros_entrada["origem_solicitação"],
        cdas=parametros_entrada.get("cdas"),
        efs=parametros_entrada.get("efs"),
        guias=parametros_entrada.get("guias"),
        codigo_de_barras=dados_guia.get("codigo_de_barras"),
        link=dados_guia.get("link"),
        data_vencimento=dados_guia.get("data_vencimento"),
        pix=dados_guia.get("pix"),
    )


def _rota_da_pgm(tipo: str) -> Tuple[str, str]:
    """Endpoint e consumidor da PGM para o tipo de emissão."""
    if tipo == "a_vista":
        return ENDPOINT_A_VISTA, CONSUMIDOR_A_VISTA
    return ENDPOINT_REGULARIZACAO, CONSUMIDOR_REGULARIZACAO


@interceptor(source={"source": "mcp", "tool": "divida_ativa_v2"})
@log_execution_time
async def emitir_guia_a_vista_v2(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Emite guia de pagamento à vista com validação Pydantic da entrada."""
    try:
        resposta = await _emitir(parameters, tipo="a_vista")
    except Exception as e:
        logger.error(
            {
                "event": "emitir_guia_a_vista_v2_error",
                "error": str(e),
            }
        )
        resposta = EmitirGuiaResponse.de_erro(f"Erro ao emitir guia à vista: {str(e)}")

    return resposta.para_dict()


@interceptor(source={"source": "mcp", "tool": "divida_ativa_v2"})
@log_execution_time
async def emitir_guia_regularizacao_v2(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Emite guia de regularização com validação Pydantic da entrada."""
    try:
        resposta = await _emitir(parameters, tipo="regularizacao")
    except Exception as e:
        logger.error(
            {
                "event": "emitir_guia_regularizacao_v2_error",
                "error": str(e),
            }
        )
        resposta = EmitirGuiaResponse.de_erro(
            f"Erro ao emitir guia de regularização: {str(e)}"
        )

    return resposta.para_dict()
