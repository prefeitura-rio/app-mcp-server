"""
Ferramentas para emissão de guia de regularização.
"""

import ast
import time
from functools import wraps
from typing import Dict, Any, Optional
from src.tools.utils import internal_request
from src.utils.log import logger
from src.config import env


def log_execution_time(func):
    """Decorator to log function execution time."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Check if request start time was passed from HTTP endpoint
        parameters = args[0] if args else kwargs.get('parameters', {})
        if isinstance(parameters, dict) and '_request_start_time' in parameters:
            start_time = parameters.pop('_request_start_time')
            logger.info({
                "event": "function_processing_started",
                "function": func.__name__,
                "http_request_start": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time)),
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
            })
        else:
            start_time = time.time()
            logger.info({
                "event": "request_started",
                "function": func.__name__,
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time)),
                "timestamp_epoch": start_time
            })
        
        try:
            result = await func(*args, **kwargs)
            end_time = time.time()
            elapsed_time = end_time - start_time
            
            logger.info({
                "event": "request_completed",
                "function": func.__name__,
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time)),
                "timestamp_epoch": end_time,
                "duration_seconds": round(elapsed_time, 3),
                "status": "success"
            })
            
            return result
            
        except Exception as e:
            end_time = time.time()
            elapsed_time = end_time - start_time
            
            logger.error({
                "event": "request_failed",
                "function": func.__name__,
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time)),
                "timestamp_epoch": end_time,
                "duration_seconds": round(elapsed_time, 3),
                "status": "error",
                "error": str(e)
            })
            
            raise
    
    return wrapper


async def pgm_api(endpoint: str = "", consumidor: str = "", data: dict = {}) -> dict:
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
    auth_response = await internal_request(
        url=env.CHATBOT_PGM_API_URL + "/security/token",
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
    token = f'Bearer {auth_response["access_token"]}'
    logger.info("Token de autenticação obtido com sucesso")

    response = await internal_request(
        url=env.CHATBOT_PGM_API_URL + f"/{endpoint}",
        method="POST",
        request_kwargs={
            "verify": False,
            "headers": {"Authorization": token},
            "data": data,
        },
    )

    logger.info("Resposta da solicitação POST:")
    logger.info(response)

    if response is None:
        logger.info("A API não retornou nada. Valor esperado para o endpoint de cadastro de usuários.")
        return {"success": True}
    elif response.get("success"):
        logger.info("A API retornou registros.")
        return response.get("data")
    else:
        logger.info(f'Algo deu errado durante a solicitação, segue justificativa: {response["data"][0]["value"]}')
        motivos = ""
        for item in response.get("data"):
            if motivos:
                motivos += "\n\n"
            motivos += item.get("value")
        return {"erro": True, "motivos": motivos}
    

async def da_emitir_guia(parameters: Dict[str, Any], tipo: str) -> Optional[Dict[str, Any]]:
    """
    Processa os parâmetros para emissão de guia.
    
    Args:
        parameters: Parâmetros da requisição
        tipo: Tipo de pagamento
    
    Returns:
        Parâmetros processados ou None se não houver parâmetros válidos
    """
    logger.info("Parâmetros recebidos para emissão de guia:")
    logger.info(parameters)

    try:
        itens_informados = list(ast.literal_eval(parameters.get("itens_informados", [])).values())
    except:
        itens_informados = [str(int(float(parameters.get("itens_informados", 1))))]

    try:
        cdas = []
        efs = []
        guias = []

        dict_itens = ast.literal_eval(parameters.get("dicionario_itens"))

        for sequencial in itens_informados:
            if tipo == "a_vista":
                if dict_itens[sequencial] in parameters.get("lista_cdas", []):
                    cdas.append(dict_itens[sequencial])
                elif dict_itens in parameters.get("lista_efs", []):
                    efs.append(dict_itens[sequencial])
            elif tipo == "regularizacao":
                if dict_itens[sequencial] in parameters.get("lista_guias", []):
                    guias.append(dict_itens[sequencial])

        parametros_entrada = {
            "origem_solicitação": 0,
        }
        if tipo == "a_vista":
            parametros_entrada.update({"cdas": cdas, "efs": efs})
        elif tipo == "regularizacao":
            parametros_entrada.update({"guias": guias})

        return parametros_entrada

    except:  # noqa
        return {"opcao_invalida": True}


async def processar_registros(
    endpoint: str,
    consumidor: str,
    parametros_entrada: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Processa os registros para emissão de guia.
    
    Args:
        endpoint: Endpoint da API para processar
        consumidor: Identificador do consumidor
        parametros_entrada: Parâmetros de entrada processados
    
    Returns:
        Resultado do processamento
    """
    registros = await pgm_api(endpoint=endpoint, consumidor=consumidor, data=parametros_entrada)

    if "erro" in registros:
        return {
            "api_resposta_sucesso": False,
            "api_descricao_erro": registros["motivos"],
        }
    
    message = parametros_entrada
    message["api_resposta_sucesso"] = True

    for _, item in enumerate(registros):
        barcode = item["codigoDeBarras"]
        pdf_file = item["pdf"]
        pix = item["codigoQrEMVPix"]

        if pix:
            message["pix"] = pix
        else:
            message["codigo_de_barras"] = barcode

        message["link"] = pdf_file

    return message


@log_execution_time
async def emitir_guia_regularizacao(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """
    Emite guia de regularização.
    
    Args:
        parameters: Parâmetros da requisição
    
    Returns:
        Resultado da emissão da guia
    """
    try:
        parametros_entrada = await da_emitir_guia(parameters, tipo="regularizacao")
        
        if not parametros_entrada:
            return {
                "success": False,
                "message": "Nenhum parâmetro válido fornecido",
                "data": {}
            }
        
        result = await processar_registros(
            endpoint="v2/guiapagamento/emitir/regularizacao",
            consumidor="emitir-guia-regularizacao",
            parametros_entrada=parametros_entrada
        )
        
        return result
        
    except Exception as e:
        logger.error({
            f"Error emitting guia regularizacao: {str(e)}",
            parameters,
        })
        return {
            "success": False,
            "error": str(e),
            "message": "Erro ao emitir guia de regularização",
        }

@log_execution_time
async def emitir_guia_a_vista(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """
    Emite guia à vista.
    
    Args:
        parameters: Parâmetros da requisição
    
    Returns:
        Resultado da emissão da guia
    """
    try:
        parametros_entrada = await da_emitir_guia(parameters, tipo="a_vista")
        
        if not parametros_entrada:
            return {
                "success": False,
                "message": "Nenhum parâmetro válido fornecido",
                "data": {}
            }
        
        result = await processar_registros(
            endpoint="v2/guiapagamento/emitir/avista",
            consumidor="emitir-guia-vista",
            parametros_entrada=parametros_entrada,
        )
        
        return result
        
    except Exception as e:
        logger.error({
            f"Error emitting guia a vista: {str(e)}",
            parameters,
        })

        return {
            "success": False,
            "error": str(e),
            "message": "Erro ao emitir guia à vista",
        }


@log_execution_time
async def consultar_debitos(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """
    Consulta débitos do contribuinte.
    
    Args:
        parameters: Parâmetros da requisição contendo:
            - consulta_debitos: Tipo de consulta
            - Valor correspondente ao tipo de consulta
            - anoAutoInfracao: Ano do auto de infração (quando aplicável)
    
    Returns:
        Resultado da consulta com informações dos débitos
    """
    try:
        return_dict = {}

        # Build input parameters
        parametros_entrada = {
            "origem_solicitação": 0,
            parameters["consulta_debitos"]: parameters[parameters["consulta_debitos"]]
        }

        if parameters["consulta_debitos"] == "numeroAutoInfracao":
            parametros_entrada["anoAutoInfracao"] = parameters["anoAutoInfracao"]
        
        # Call PGM API
        registros = await pgm_api(
            endpoint="v2/cdas/dividas-contribuinte", 
            consumidor="consultar-dividas-contribuinte", 
            data=parametros_entrada
        )

        # Handle API error
        if "erro" in registros:
            return_dict["api_resposta_sucesso"] = False
            return_dict["api_descricao_erro"] = registros["motivos"]
            return return_dict

        return_dict["api_resposta_sucesso"] = True

        # Description mapping
        mapeia_descricoes = {
            "inscricaoImobiliaria": "Inscrição Imobiliária",
            "cda": "Certidão de Dívida Ativa",
            "cpfCnpj": "CPF/CNPJ",
            "numeroExecucaoFiscal": "Número de Execução Fiscal",
            "numeroAutoInfracao": "Nº e Ano do Auto de Infração",
        }

        msg = []
        debitos = []
        itens_pagamento = {} 
        indice = 0

        msg.append(f'*{mapeia_descricoes[parameters["consulta_debitos"]]}*:')

        if parameters["consulta_debitos"] == "numeroAutoInfracao":
            msg.append(f'{parameters[parameters["consulta_debitos"]]} {parameters["anoAutoInfracao"]}')
        else:
            msg.append(f'{parameters[parameters["consulta_debitos"]]}')
                
        if parameters["consulta_debitos"] == "inscricaoImobiliaria":
            msg.append('\n*Endereço do Imóvel:*')
            msg.append(f'{registros.get("enderecoImovel", "N/A")}')
        
        debitos_nao_parcelados = registros.get("debitosNaoParceladosComSaldoTotal", {})
        cdas_nao_ajuizadas = debitos_nao_parcelados.get("cdasNaoAjuizadasNaoParceladas", [])
        efs_nao_parceladas = debitos_nao_parcelados.get("efsNaoParceladas", [])
        guias_parceladas = registros.get("guiasParceladasComSaldoTotal", {}).get("guiasParceladas", [])

        if guias_parceladas:
            msg.append("\n*Guias de parcelamento encontradas:*")
            for _, guia in enumerate(guias_parceladas):
                indice += 1
                itens_pagamento[indice] = guia["numero"]
                msg_guia = f'*{indice}.* *Guia nº {guia["numero"]}* - Data do Último Pagamento: {guia.get("dataUltimoPagamento", "N/A")}'
                msg.append(msg_guia)
                debitos.append({"guia": guia["numero"], "data_ultimo_pagamento": guia.get("dataUltimoPagamento", "N/A")})
            return_dict["lista_guias"] = [guia["numero"] for guia in guias_parceladas]

        if cdas_nao_ajuizadas or efs_nao_parceladas:
            if cdas_nao_ajuizadas:
                msg.append("\n*Certidões de Dívida Ativa não parceladas:*")
                for _, cda in enumerate(cdas_nao_ajuizadas):
                    indice += 1
                    itens_pagamento[indice] = cda["cdaId"]
                    msg_cda = f'*{indice}.* *CDA {cda["cdaId"]}*'
                    msg.append(msg_cda)
                    msg.append(f'Valor: R$ {cda.get("valorSaldoTotal", "N/A")}')
                    debitos.append({"cda": cda["cdaId"], "valor": cda.get("valorSaldoTotal", "N/A")})
                return_dict["lista_cdas"] = [cda["cdaId"] for cda in cdas_nao_ajuizadas]

            if efs_nao_parceladas:
                msg.append("\n*Execuções Fiscais não parceladas:*")
                for _, ef in enumerate(efs_nao_parceladas):
                    indice += 1
                    itens_pagamento[indice] = ef["numeroExecucaoFiscal"]
                    msg_exec = f'*{indice}.* *EF {ef["numeroExecucaoFiscal"]}*'
                    msg.append(msg_exec)
                    msg.append(f'Valor: R$ {ef.get("saldoExecucaoFiscalNaoParcelada", "N/A")}')
                    debitos.append({"ef": ef["numeroExecucaoFiscal"], "valor": ef.get("saldoExecucaoFiscalNaoParcelada", "N/A")})
                return_dict["lista_efs"] = [ef["numeroExecucaoFiscal"] for ef in efs_nao_parceladas]
            
            msg.append('\n*Débitos não parcelados:*')
            msg.append('Valor total da dívida:')
            msg.append(f'R$ {debitos_nao_parcelados.get("saldoTotalNaoParcelado", "N/A")}')

        msg.append(f'\n*Data de Vencimento:* {registros.get("dataVencimento", "N/A")}')

        # Update return dictionary
        return_dict.update({
            "dicionario_itens": itens_pagamento,
            "total_itens_pagamento": indice,
            "mensagem_divida_contribuinte": "\n".join(msg),
            "guias_quantidade_total": len(return_dict.get("lista_guias", [])),
            "efs_cdas_quantidade_total": len(return_dict.get("lista_efs", [])) + len(return_dict.get("lista_cdas", [])),
            "total_nao_parcelado": len(efs_nao_parceladas) + len(cdas_nao_ajuizadas),
            "total_parcelado": len(guias_parceladas),
            "debitos_msg": debitos,
        })

        return return_dict
        
    except Exception as e:
        logger.error({
            f"Error consulting debts: {str(e)}",
            parameters,
        })

        return {
            "api_resposta_sucesso": False,
            "api_descricao_erro": f"Erro ao consultar débitos: {str(e)}",
        }