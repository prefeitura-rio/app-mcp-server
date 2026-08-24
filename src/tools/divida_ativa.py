import ast
import time
import asyncio
from functools import wraps
from typing import Dict, Any, List, Optional, Tuple


from src.tools.utils import internal_request
from src.utils.log import logger
from src.utils.error_interceptor import interceptor
from src.config import env


def log_execution_time(func):
    """Decorator to log function execution time."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        parameters = args[0] if args else kwargs.get("parameters", {})

        if isinstance(parameters, dict) and "_request_start_time" in parameters:
            start_time = parameters.pop("_request_start_time")
            logger.info(
                {
                    "event": "function_processing_started",
                    "function": func.__name__,
                    "http_request_start": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(start_time)
                    ),
                    "timestamp": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(time.time())
                    ),
                }
            )
        else:
            start_time = time.time()
            logger.info(
                {
                    "event": "request_started",
                    "function": func.__name__,
                    "timestamp": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(start_time)
                    ),
                    "timestamp_epoch": start_time,
                }
            )

        try:
            result = await func(*args, **kwargs)
            end_time = time.time()
            elapsed = round(end_time - start_time, 3)

            logger.info(
                {
                    "event": "request_completed",
                    "function": func.__name__,
                    "timestamp": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(end_time)
                    ),
                    "timestamp_epoch": end_time,
                    "duration_seconds": elapsed,
                    "status": "success",
                }
            )

            return result

        except Exception as e:
            end_time = time.time()
            elapsed = round(end_time - start_time, 3)

            logger.error(
                {
                    "event": "request_failed",
                    "function": func.__name__,
                    "timestamp": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(end_time)
                    ),
                    "timestamp_epoch": end_time,
                    "duration_seconds": elapsed,
                    "status": "error",
                    "error": str(e),
                    "parameters": args[0] if args else kwargs.get("parameters", {}),
                }
            )

            raise

    return wrapper


@interceptor(source={"source": "mcp", "tool": "divida_ativa"})
async def pgm_api(
    endpoint: str = "", consumidor: str = "", data: dict | None = None
) -> dict:
    """
    Makes authenticated requests to PGM API.

    Args:
        endpoint: API endpoint path
        consumidor: Consumer identifier
        data: Request data payload

    Returns:
        API response data

    Raises:
        Exception: If authentication or API request fails
    """
    if data is None:
        data = {}
    try:
        auth_response = await internal_request(
            url=f"{env.CHATBOT_PGM_API_URL}/security/token",
            method="POST",
            request_kwargs={
                "verify": False,
                "headers": {},
                "data": {
                    "grant_type": "password",
                    "Consumidor": consumidor,
                    "ChaveAcesso": env.CHATBOT_PGM_ACCESS_KEY,
                },
            },
        )

        if "access_token" not in auth_response:
            raise Exception("Failed to get PGM access token")

        token = f"Bearer {auth_response['access_token']}"
        logger.info("Token de autenticação obtido com sucesso")

        response = await internal_request(
            url=f"{env.CHATBOT_PGM_API_URL}/{endpoint}",
            method="POST",
            request_kwargs={
                "verify": False,
                "headers": {"Authorization": token},
                "data": data,
            },
        )

        logger.info(f"pgm_api - Resposta recebida para [{data}]: {response}")

        if response is None:
            logger.info(
                "A API não retornou nada. Valor esperado para o endpoint de cadastro de usuários."
            )
            return {"success": True}

        if response.get("success"):
            logger.info("A API retornou registros.")
            return response.get("data")

        logger.info(f"Erro durante a solicitação: {response['data'][0]['value']}")

        mensagens_unicas = list(
            set(
                item.get("value")
                for item in response.get("data", [])
                if item.get("value")
            )
        )
        motivos = (
            "\n\n".join(mensagens_unicas)
            if len(mensagens_unicas) > 1
            else mensagens_unicas[0]
            if mensagens_unicas
            else "Erro desconhecido"
        )

        return {"erro": True, "motivos": motivos}

    except (asyncio.TimeoutError, TimeoutError) as e:
        logger.error(
            {
                "event": "pgm_api_timeout_error",
                "endpoint": endpoint,
                "consumidor": consumidor,
                "error": "Timeout ao conectar com o sistema de dívida ativa",
                "error_type": type(e).__name__,
            }
        )
        return {
            "erro": True,
            "motivos": "O sistema de dívida ativa está temporariamente indisponível. Por favor, tente novamente em alguns instantes.",
        }

    except Exception as e:
        logger.error(
            {
                "event": "pgm_api_general_error",
                "endpoint": endpoint,
                "consumidor": consumidor,
                "error": str(e),
                "error_type": type(e).__name__,
            }
        )
        if "timeout" in str(e).lower():
            return {
                "erro": True,
                "motivos": "O sistema de dívida ativa está temporariamente indisponível. Por favor, tente novamente em alguns instantes.",
            }
        raise


async def da_emitir_guia(
    parameters: Dict[str, Any], tipo: str
) -> Optional[Dict[str, Any]]:
    """
    Processa os parâmetros para emissão de guia.

    Args:
        parameters: Parâmetros da requisição
        tipo: Tipo de pagamento ("a_vista" ou "regularizacao")

    Returns:
        Parâmetros processados ou None se inválido
    """
    logger.info(
        {"event": "da_emitir_guia_started", "tipo": tipo, "parameters": parameters}
    )

    itens_raw = parameters.get("itens_informados", [])
    try:
        if itens_raw:
            if isinstance(itens_raw, str):
                itens_informados = ast.literal_eval(itens_raw.strip())
                if not isinstance(itens_informados, (list, tuple)):
                    itens_informados = [str(int(float(itens_informados)))]
            elif isinstance(itens_raw, list):
                itens_informados = itens_raw
        else:
            itens_informados = [str(int(float(parameters.get("apenas_um_item"))))]

    except Exception as e:
        logger.error(
            {
                "event": "da_emitir_guia_parse_error",
                "error": str(e),
                "parameters": parameters,
                "itens_informados_raw": itens_raw,
            }
        )
        itens_informados = [str(int(float(parameters.get("itens_informados", 1))))]

    try:
        dict_itens = ast.literal_eval(parameters.get("dicionario_itens", "{}"))
        if not isinstance(dict_itens, dict):
            raise ValueError("dict_itens não é um dicionário válido")

        def safe_eval(raw):
            if isinstance(raw, str) and raw.strip():
                return ast.literal_eval(raw)
            return raw or []

        lista_cdas = safe_eval(parameters.get("lista_cdas", "[]"))
        lista_efs = safe_eval(parameters.get("lista_efs", "[]"))
        lista_guias = safe_eval(parameters.get("lista_guias", "[]"))

        cdas, efs, guias = [], [], []

        for seq in itens_informados:
            valor = dict_itens.get(str(seq))

            if tipo == "a_vista":
                if valor in lista_cdas:
                    cdas.append(valor)
                elif valor in lista_efs:
                    efs.append(valor)
            elif tipo == "regularizacao" and valor in lista_guias:
                guias.append(valor)

        parametros_entrada = {"origem_solicitação": 0}
        if tipo == "a_vista":
            parametros_entrada.update({"cdas": cdas, "efs": efs})
        elif tipo == "regularizacao":
            parametros_entrada.update({"guias": guias})

        return parametros_entrada

    except Exception as e:
        logger.error(
            {
                "event": "da_emitir_guia_processing_error",
                "error": str(e),
                "parameters": parameters,
            }
        )
        return {"opcao_invalida": True}


# Campos que identificam um registro de guia na resposta da PGM.
CAMPOS_PGM_GUIA = ("codigoDeBarras", "pdf", "dataVencimento", "codigoQrEMVPix")

# Nomes possíveis do valor e da natureza dentro do registro de guia. Diferente
# dos quatro campos acima, estes não têm amostra confirmada: a resposta de
# emissão da PGM não é documentada e os registros conhecidos vêm de log, onde
# nunca precisamos destes dois. Cada campo é procurado numa lista de
# candidatos — mesmo padrão de GUIA_CAMPOS no workflow — para que o nome real
# seja absorvido sem novo deploy. Ver docs/decisions/guia-valor-e-natureza.md.
CAMPOS_PGM_VALOR = (
    "valorTotal",
    "valorTotalGuia",
    "valorGuia",
    "valorDocumento",
    "valorSaldoTotal",
    "valor",
)
CAMPOS_PGM_NATUREZA = (
    "naturezaDivida",
    "naturezaDebito",
    "descricaoNatureza",
    "tipoDebito",
    "natureza",
)

MENSAGEM_SEM_GUIA = (
    "A PGM respondeu sem nenhuma guia emitida. Nenhum pagamento pode ser "
    "feito com esta resposta; tente novamente."
)


def _primeiro_campo(registro: Dict[str, Any], campos: Tuple[str, ...]) -> Any:
    """Primeiro campo preenchido dentre os nomes candidatos."""
    for campo in campos:
        valor = registro.get(campo)
        if valor not in (None, "", [], {}):
            return valor
    return None


def _formatar_valor(valor: Any) -> str:
    """
    Valor da guia como texto, pronto para exibição.

    A consulta de débitos da PGM devolve valor já formatado ("R$5.000,00") e a
    emissão pode devolver número. Formatar aqui evita que cada consumidor
    tenha que adivinhar em qual dos dois formatos o campo chegou.
    """
    if valor is None:
        return ""
    if isinstance(valor, str):
        return valor.strip()
    if isinstance(valor, bool):
        return ""
    if isinstance(valor, (int, float)):
        inteiro, centavos = f"{valor:.2f}".split(".")
        milhar = f"{int(inteiro):,}".replace(",", ".")
        return f"R$ {milhar},{centavos}"
    return str(valor)


def valor_e_natureza_da_guia(registro: Dict[str, Any]) -> Tuple[str, str]:
    """
    Valor em dinheiro e natureza do débito de uma guia (CHATR-164).

    Pública — e não `_`-prefixada como as demais deste bloco — porque a v2
    também constrói guia a partir do registro cru (`GuiaEmitida.de_registro`)
    e o contrato da PGM não pode existir em duas cópias.

    Registro sem nenhum dos dois é logado com as chaves que a PGM enviou (só
    as chaves: os valores carregam o PDF em base64). É esse log que confirma o
    nome real dos campos, hoje deduzido dos demais endpoints da PGM.
    """
    valor = _formatar_valor(_primeiro_campo(registro, CAMPOS_PGM_VALOR))
    natureza = _primeiro_campo(registro, CAMPOS_PGM_NATUREZA)
    natureza = natureza.strip() if isinstance(natureza, str) else ""

    if not valor and not natureza:
        logger.warning(
            {
                "event": "guia_sem_valor_nem_natureza",
                "campos_do_registro": sorted(registro.keys()),
            }
        )

    return valor, natureza


def _extrair_guias(registros: Any) -> List[Dict[str, str]]:
    """
    Extrai todas as guias emitidas a partir da resposta da PGM.

    O EPGM emite uma guia por natureza de débito, então uma solicitação com N
    identificadores pode devolver N registros. Aceita também o registro único
    fora de lista.

    Cada guia leva, além dos dados de pagamento, o valor e a natureza do
    débito: sem eles o consumidor não tem como dizer ao cidadão o que cada
    guia cobra nem quanto custa.
    """
    if isinstance(registros, dict):
        registros = [registros]

    if not isinstance(registros, list):
        return []

    guias = []

    for item in registros:
        # `pgm_api` também devolve dicts que não são guia — {"success": True}
        # quando a resposta vem vazia. Sem o filtro, viraria guia em branco.
        if not isinstance(item, dict) or not any(
            item.get(campo) for campo in CAMPOS_PGM_GUIA
        ):
            continue

        valor, natureza = valor_e_natureza_da_guia(item)
        guias.append(
            {
                "codigo_de_barras": item.get("codigoDeBarras") or "",
                "link": item.get("pdf") or "",
                "data_vencimento": item.get("dataVencimento") or "",
                "pix": item.get("codigoQrEMVPix") or "",
                "valor": valor,
                "natureza": natureza,
            }
        )

    return guias


async def processar_registros(
    endpoint: str, consumidor: str, parametros_entrada: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Processa os registros para emissão de guia.

    Devolve todas as guias emitidas em 'guias_emitidas'. Até CHATR-164 o loop
    sobrescrevia os mesmos campos a cada registro e só a última guia chegava ao
    consumidor, fazendo o cidadão pagar uma achando que quitou todas. Os campos
    no topo (codigo_de_barras, link, data_vencimento, pix) seguem existindo para
    os consumidores que ainda leem uma guia só, agora com a PRIMEIRA guia.

    Args:
        endpoint: Endpoint da API para processar
        consumidor: Identificador do consumidor
        parametros_entrada: Parâmetros de entrada processados

    Returns:
        Resultado do processamento
    """
    registros = await pgm_api(
        endpoint=endpoint, consumidor=consumidor, data=parametros_entrada
    )

    if isinstance(registros, dict) and "erro" in registros:
        return {
            "api_resposta_sucesso": False,
            "api_descricao_erro": registros["motivos"],
        }

    guias_emitidas = _extrair_guias(registros)

    # Sucesso sem nenhuma guia não é sucesso: o consumidor receberia
    # 'api_resposta_sucesso: true' sem nada para o cidadão pagar.
    if not guias_emitidas:
        logger.error(
            {
                "event": "processar_registros_sem_guia",
                "endpoint": endpoint,
                "registros": registros,
            }
        )
        return {
            "api_resposta_sucesso": False,
            "api_descricao_erro": MENSAGEM_SEM_GUIA,
        }

    primeira = guias_emitidas[0]
    message = parametros_entrada.copy()
    message["api_resposta_sucesso"] = True
    message["guias_emitidas"] = guias_emitidas
    message["total_guias"] = len(guias_emitidas)
    # Campos no topo listados um a um, como a v2 faz no construtor de
    # EmitirGuiaResponse: um `update` com a guia inteira derramaria no topo
    # qualquer chave nova que `_extrair_guias` viesse a devolver.
    message["codigo_de_barras"] = primeira["codigo_de_barras"]
    message["link"] = primeira["link"]
    message["data_vencimento"] = primeira["data_vencimento"]
    message["pix"] = primeira["pix"]

    return message


@interceptor(source={"source": "mcp", "tool": "divida_ativa"})
@log_execution_time
async def emitir_guia_regularizacao(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """
    Emite guia de regularização.

    DEPRECATED (CHATR-120): use `emitir_guia_regularizacao_v2` de
    `src.tools.divida_ativa_v2`, exposta em POST /v2/emitir_guia_regularizacao.
    A v2 valida entrada e saída com Pydantic. Esta versão segue funcionando
    sem alterações enquanto os consumidores migram.
    """
    try:
        entrada = await da_emitir_guia(parameters, tipo="regularizacao")

        if not entrada:
            return {
                "api_resposta_sucesso": False,
                "api_descricao_erro": "Nenhum parâmetro válido fornecido",
            }

        return await processar_registros(
            endpoint="v2/guiapagamento/emitir/regularizacao",
            consumidor="emitir-guia-regularizacao",
            parametros_entrada=entrada,
        )

    except Exception as e:
        logger.error(
            {
                "event": "emitir_guia_regularizacao_error",
                "error": str(e),
                "parameters": parameters,
            }
        )
        return {
            "api_resposta_sucesso": False,
            "api_descricao_erro": f"Erro ao emitir guia de regularização: {str(e)}",
        }


@interceptor(source={"source": "mcp", "tool": "divida_ativa"})
@log_execution_time
async def emitir_guia_a_vista(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """
    Emite guia de pagamento à vista.

    DEPRECATED (CHATR-120): use `emitir_guia_a_vista_v2` de
    `src.tools.divida_ativa_v2`, exposta em POST /v2/emitir_guia. A v2 valida
    entrada e saída com Pydantic. Esta versão segue funcionando sem alterações
    enquanto os consumidores migram.
    """
    try:
        entrada = await da_emitir_guia(parameters, tipo="a_vista")

        if not entrada:
            return {
                "api_resposta_sucesso": False,
                "api_descricao_erro": "Nenhum parâmetro válido fornecido",
            }

        return await processar_registros(
            endpoint="v2/guiapagamento/emitir/avista",
            consumidor="emitir-guia-vista",
            parametros_entrada=entrada,
        )

    except Exception as e:
        logger.error(
            {
                "event": "emitir_guia_a_vista_error",
                "error": str(e),
                "parameters": parameters,
            }
        )

        return {
            "api_resposta_sucesso": False,
            "api_descricao_erro": f"Erro ao emitir guia à vista: {str(e)}",
        }


@interceptor(source={"source": "mcp", "tool": "divida_ativa"})
@log_execution_time
async def consultar_debitos(parameters: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return_dict = {}

        tipo_consulta = parameters["consulta_debitos"]
        valor_usuario = parameters.get(tipo_consulta, "").strip()
        valor_limpo = "".join(c for c in valor_usuario if c.isdigit())

        if not valor_limpo:
            return {
                "api_resposta_sucesso": False,
                "api_descricao_erro": f"O valor informado '{valor_usuario}' não é válido.",
            }

        parametros_entrada = {"origem_solicitação": 0, tipo_consulta: valor_limpo}

        ano_limpo = ""

        if tipo_consulta == "numeroAutoInfracao":
            ano_usuario = parameters.get("anoAutoInfracao", "").strip()
            ano_limpo = "".join(c for c in ano_usuario if c.isdigit())

            if not ano_limpo:
                return {
                    "api_resposta_sucesso": False,
                    "api_descricao_erro": f"Por favor, informe apenas números para o ano do Auto de Infração. O valor informado '{ano_usuario}' não contém números válidos.",
                }

            parametros_entrada["anoAutoInfracao"] = ano_limpo

        registros = await pgm_api(
            endpoint="v2/cdas/dividas-contribuinte",
            consumidor="consultar-dividas-contribuinte",
            data=parametros_entrada,
        )

        if "erro" in registros:
            return {
                "api_resposta_sucesso": False,
                "api_descricao_erro": registros["motivos"],
            }

        return_dict["api_resposta_sucesso"] = True

        mapeia_descricoes = {
            "inscricaoImobiliaria": "Inscrição Imobiliária",
            "cda": "Certidão de Dívida Ativa",
            "cpfCnpj": "CPF/CNPJ",
            "numeroExecucaoFiscal": "Número de Execução Fiscal",
            "numeroAutoInfracao": "Nº e Ano do Auto de Infração",
        }

        msg, debitos, itens_pagamento = [], [], {}
        indice = 0

        msg.append(f"*{mapeia_descricoes[tipo_consulta]}*:")

        if tipo_consulta == "numeroAutoInfracao":
            msg.append(f"{valor_limpo} {ano_limpo}")
        else:
            msg.append(f"{valor_limpo}")

        if tipo_consulta == "inscricaoImobiliaria":
            msg.append("\n*Endereço do Imóvel:*")
            msg.append(f"{registros.get('enderecoImovel', 'N/A')}")

        debitos_np = registros.get("debitosNaoParceladosComSaldoTotal", {})
        cdas = debitos_np.get("cdasNaoAjuizadasNaoParceladas", [])
        efs = debitos_np.get("efsNaoParceladas", [])
        guias = registros.get("guiasParceladasComSaldoTotal", {}).get(
            "guiasParceladas", []
        )
        naturezas_divida = registros.get("naturezasDivida", [])

        if naturezas_divida:
            naturezas_divida_txt = (", ").join(naturezas_divida)
            msg.append(f"\n*Natureza da dívida:* {naturezas_divida_txt}")

        if cdas:
            msg.append("\n*Certidões de Dívida Ativa não parceladas:*")
            for cda in cdas:
                indice += 1
                itens_pagamento[indice] = cda["cdaId"]
                msg.append(f"*{indice}.* *CDA {cda['cdaId']}*")
                msg.append(f"Valor: {cda.get('valorSaldoTotal', 'N/A')}")
                debitos.append(
                    {"cda": cda["cdaId"], "valor": cda.get("valorSaldoTotal", "N/A")}
                )
            return_dict["lista_cdas"] = [c["cdaId"] for c in cdas]

        if efs:
            msg.append("\n*Execuções Fiscais não parceladas:*")
            for ef in efs:
                indice += 1
                itens_pagamento[indice] = ef["numeroExecucaoFiscal"]
                msg.append(f"*{indice}.* *EF {ef['numeroExecucaoFiscal']}*")
                msg.append(f"Valor: {ef.get('saldoExecucaoFiscalNaoParcelada', 'N/A')}")
                debitos.append(
                    {
                        "ef": ef["numeroExecucaoFiscal"],
                        "valor": ef.get("saldoExecucaoFiscalNaoParcelada", "N/A"),
                    }
                )
            return_dict["lista_efs"] = [e["numeroExecucaoFiscal"] for e in efs]

        if guias:
            msg.append("\n*Guias de parcelamento encontradas:*")
            for guia in guias:
                indice += 1
                itens_pagamento[indice] = guia["numero"]
                msg.append(
                    f"*{indice}.* *Guia nº {guia['numero']}* - Data do Último Pagamento: {guia.get('dataUltimoPagamento', 'N/A')}"
                )
                debitos.append(
                    {
                        "guia": guia["numero"],
                        "data_ultimo_pagamento": guia.get("dataUltimoPagamento", "N/A"),
                    }
                )
            return_dict["lista_guias"] = [g["numero"] for g in guias]

            msg.append("\n*Débitos não parcelados:*")
            msg.append("Valor total da dívida:")
            msg.append(f"{debitos_np.get('saldoTotalNaoParcelado', 'N/A')}")

        msg.append(f"\n*Data de Vencimento:* {registros.get('dataVencimento', 'N/A')}")

        return_dict.update(
            {
                "dicionario_itens": itens_pagamento,
                "total_itens_pagamento": indice,
                "mensagem_divida_contribuinte": "\n".join(msg),
                "guias_quantidade_total": len(return_dict.get("lista_guias", [])),
                "efs_cdas_quantidade_total": len(return_dict.get("lista_efs", []))
                + len(return_dict.get("lista_cdas", [])),
                "total_nao_parcelado": len(efs) + len(cdas),
                "total_parcelado": len(guias),
                "debitos_msg": debitos,
            }
        )

        return return_dict

    except Exception as e:
        logger.error(
            {
                "event": "consultar_debitos_error",
                "error": str(e),
                "parameters": parameters,
            }
        )

        return {
            "api_resposta_sucesso": False,
            "api_descricao_erro": f"Erro ao consultar débitos: {str(e)}",
        }
