import httpx
import re
import traceback as tb
from typing import Optional, Dict, Any

from loguru import logger

from src.config import env
from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import DadosDividaAtiva
from src.utils.error_interceptor import send_api_error


class DividaAtivaAPIService:
    def __init__(self, user_id: str = "unknown"):
        self.api_base_url = env.IPTU_API_URL
        self.api_token = env.IPTU_API_TOKEN
        self.proxy = env.PROXY_URL
        self.user_id = user_id

    def _limpar_inscricao(self, inscricao: str) -> str:
        """
        Limpa a inscrição imobiliária removendo caracteres não numéricos.

        Args:
            inscricao (str): Inscrição imobiliária original.

        Returns:
            str: Inscrição imobiliária limpa.
        """
        return "".join(filter(str.isdigit, inscricao))
    
    def _limpar_cpf_cnpj(self, documento: str) -> str:
        """
        Limpa CPF ou CNPJ removendo caracteres não numéricos.

        Args:
            documento (str): CPF ou CNPJ original.

        Returns:
            str: Documento limpo.
        """
        return "".join(filter(str.isdigit, documento))
    
    def _identificar_tipo_entrada(self, entrada: str) -> tuple[str, str]:
        """
        Identifica o tipo de entrada fornecida.

        Args:
            entrada (str): Entrada fornecida pelo usuário.

        Returns:
            tuple[str, str]: Tupla com (tipo_entrada, valor_limpo).
        """
        # Remove espaços e caracteres especiais para análise
        entrada_limpa = re.sub(r'[\s\-\.\,\/]', '', entrada)
        
        # Verifica se é CPF (11 dígitos)
        if re.match(r'^\d{11}$', entrada_limpa):
            return ('cpf', entrada_limpa)
        
        # Verifica se é CNPJ (14 dígitos)
        if re.match(r'^\d{14}$', entrada_limpa):
            return ('cnpj', entrada_limpa)
        
        # Verifica se é inscrição imobiliária (7 dígitos)
        if re.match(r'^\d{7}$', entrada_limpa):
            return ('inscricao_imobiliaria', entrada_limpa)
        
        # Verifica se é certidão de dívida ativa (formato: CDA ou número)
        if 'cda' in entrada.lower() or 'certidao' in entrada.lower() or 'certidão' in entrada.lower():
            # Extrai apenas números
            numeros = ''.join(filter(str.isdigit, entrada))
            if numeros:
                return ('certidao_divida_ativa', numeros)
        
        # Verifica se é execução fiscal (formato: EF ou número de processo)
        if 'ef' in entrada.lower() or 'execucao' in entrada.lower() or 'execução' in entrada.lower() or 'fiscal' in entrada.lower():
            # Extrai apenas números
            numeros = ''.join(filter(str.isdigit, entrada))
            if numeros:
                return ('execucao_fiscal', numeros)
        
        # Verifica se é auto de infração (formato: ano + número)
        # Padrão: pode ser "2024 123456" ou "2024/123456" ou "123456/2024"
        auto_match = re.match(r'(\d{4})[\s\/\-]+(\d+)', entrada)
        if not auto_match:
            auto_match = re.match(r'(\d+)[\s\/\-]+(\d{4})', entrada)
            if auto_match:
                # Inverte a ordem se o ano vier depois
                auto_match = (auto_match.group(2), auto_match.group(1))
            else:
                auto_match = None
        
        if auto_match:
            if isinstance(auto_match, tuple):
                ano = auto_match[0]
                numero = auto_match[1]
            else:
                ano = auto_match.group(1)
                numero = auto_match.group(2)
            return ('auto_infracao', f'{ano}_{numero}')
        
        # Se contém a palavra "auto" e números, tenta extrair como auto de infração
        if 'auto' in entrada.lower():
            numeros = re.findall(r'\d+', entrada)
            if len(numeros) >= 2:
                # Assume que o número de 4 dígitos é o ano
                for num in numeros:
                    if len(num) == 4:
                        ano = num
                        numeros.remove(num)
                        numero = ''.join(numeros)
                        return ('auto_infracao', f'{ano}_{numero}')
        
        # Tenta identificar se é um número de processo judicial
        if re.match(r'^\d{7}\d{2}\d{4}\d{3}\d{4}$', entrada_limpa) or len(entrada_limpa) == 20:
            return ('execucao_fiscal', entrada_limpa)
        
        # Se não identificou, assume que é inscrição imobiliária
        return ('inscricao_imobiliaria', self._limpar_inscricao(entrada))
    
    def _preparar_payload(self, tipo_entrada: str, valor: str) -> Dict[str, Any]:
        """
        Prepara o payload para a API de acordo com o tipo de entrada.

        Args:
            tipo_entrada (str): Tipo de entrada identificado.
            valor (str): Valor limpo da entrada.

        Returns:
            Dict[str, Any]: Payload para a API.
        """
        payload = {"origem_solicitação": 0}
        
        if tipo_entrada == 'cpf':
            payload['cpf'] = valor
        elif tipo_entrada == 'cnpj':
            payload['cnpj'] = valor
        elif tipo_entrada == 'inscricao_imobiliaria':
            payload['inscricaoImobiliaria'] = valor
        elif tipo_entrada == 'auto_infracao':
            # Separa ano e número
            partes = valor.split('_')
            if len(partes) == 2:
                payload['anoAutoInfracao'] = partes[0]
                payload['numeroAutoInfracao'] = partes[1]
            else:
                # Fallback para inscrição
                payload['inscricaoImobiliaria'] = valor
        elif tipo_entrada == 'certidao_divida_ativa':
            payload['certidaoDividaAtiva'] = valor
        elif tipo_entrada == 'execucao_fiscal':
            payload['execucaoFiscal'] = valor
        else:
            # Default para inscrição imobiliária
            payload['inscricaoImobiliaria'] = valor
        
        return payload
    
    async def get_divida_ativa_info(self, entrada: str) -> Optional[DadosDividaAtiva]:
        """
        Consulta a API de Dívida Ativa para obter informações sobre débitos.

        Args:
            entrada (str): Pode ser CPF, CNPJ, inscrição imobiliária, 
                          ano + número do auto de infração, certidão de dívida ativa,
                          ou execução fiscal.

        Returns:
            DadosDividaAtiva com informações processadas de dívida ativa, ou None se não houver débitos.

        Raises:
            APIUnavailableError: Quando API está indisponível (timeout, 500, 503, etc.)
            AuthenticationError: Quando falha autenticação (401)
        """
        # Identifica o tipo de entrada e prepara o valor
        tipo_entrada, valor_limpo = self._identificar_tipo_entrada(entrada)

        logger.info(
            f"Iniciando consulta de dívida ativa - Tipo: {tipo_entrada}, Valor: {valor_limpo}"
        )

        try:
            async with httpx.AsyncClient(timeout=30.0, proxy=self.proxy) as client:
                # Autenticação
                try:
                    auth_response = await client.post(
                        f"{env.DIVIDA_ATIVA_API_URL}/security/token",
                        data={
                            "verify": False,
                            "grant_type": "password",
                            "Consumidor": "consultar-dividas-contribuinte",
                            "ChaveAcesso": env.DIVIDA_ATIVA_ACCESS_KEY,
                        },
                    )

                    if auth_response.status_code == 401:
                        logger.error("Falha na autenticação da Dívida Ativa")
                        # Reporta erro ao interceptor
                        await send_api_error(
                            user_id=self.user_id,
                            service_name="iptu_pagamento",
                            api_endpoint=f"{env.DIVIDA_ATIVA_API_URL}/security/token",
                            request_body={
                                "Consumidor": "consultar-dividas-contribuinte"
                            },
                            status_code=auth_response.status_code,
                            error_message="Falha na autenticação do serviço de Dívida Ativa",
                        )
                        raise Exception(
                            "Falha na autenticação do serviço de Dívida Ativa"
                        )
                    elif auth_response.status_code in [500, 503]:
                        logger.error(
                            f"Erro de servidor na autenticação da Dívida Ativa: {auth_response.status_code}"
                        )
                        # Reporta erro ao interceptor
                        await send_api_error(
                            user_id=self.user_id,
                            service_name="iptu_pagamento",
                            api_endpoint=f"{env.DIVIDA_ATIVA_API_URL}/security/token",
                            request_body={
                                "Consumidor": "consultar-dividas-contribuinte"
                            },
                            status_code=auth_response.status_code,
                            error_message=f"Serviço de Dívida Ativa temporariamente indisponível (autenticação): {auth_response.text[:500]}",
                        )
                        raise Exception(
                            f"Serviço de Dívida Ativa temporariamente indisponível (HTTP {auth_response.status_code})"
                        )

                    auth_response_json = auth_response.json()
                    if "access_token" not in auth_response_json:
                        logger.error(
                            f"Token não encontrado na resposta de autenticação: {auth_response.status_code} - {auth_response.text}"
                        )
                        raise Exception(
                            "Falha ao obter token de autenticação da Dívida Ativa"
                        )

                    token = f'Bearer {auth_response_json["access_token"]}'
                    logger.info("Token de autenticação obtido com sucesso")

                except httpx.TimeoutException:
                    logger.error("Timeout ao autenticar na Dívida Ativa")
                    # Reporta erro ao interceptor com traceback
                    await send_api_error(
                        user_id=self.user_id,
                        service_name="iptu_pagamento",
                        api_endpoint=f"{env.DIVIDA_ATIVA_API_URL}/security/token",
                        request_body={"Consumidor": "consultar-dividas-contribuinte"},
                        status_code=408,
                        error_message="Serviço de Dívida Ativa não respondeu no tempo esperado (autenticação)",
                        traceback=tb.format_exc(),
                    )
                    raise Exception(
                        "Serviço de Dívida Ativa não respondeu no tempo esperado (autenticação)"
                    )

                # Consulta de dívidas
                try:
                    # Prepara o payload de acordo com o tipo de entrada
                    payload = self._preparar_payload(tipo_entrada, valor_limpo)
                    
                    response = await client.post(
                        f"{env.DIVIDA_ATIVA_API_URL}/v2/cdas/dividas-contribuinte",
                        headers={"Authorization": token},
                        data=payload,
                    )
                    if response.status_code == 200:
                        response_data = response.json()
                        logger.info(f"Consulta de dívida ativa realizada com sucesso")
                        # Usa o método from_api_response do modelo para processar os dados
                        return DadosDividaAtiva.from_api_response(response_data)
                    elif response.status_code == 404:
                        # Não encontrou débitos - retorna None
                        logger.info(
                            f"Nenhuma dívida ativa encontrada para {tipo_entrada}: {valor_limpo}"
                        )
                        return None
                    elif response.status_code == 401:
                        logger.error("Erro de autenticação ao consultar dívidas")
                        # Reporta erro ao interceptor
                        await send_api_error(
                            user_id=self.user_id,
                            service_name="iptu_pagamento",
                            api_endpoint=f"{env.DIVIDA_ATIVA_API_URL}/v2/cdas/dividas-contribuinte",
                            request_body=payload,
                            status_code=response.status_code,
                            error_message="Falha na autenticação ao consultar dívidas",
                        )
                        raise Exception(
                            "Falha na autenticação ao consultar dívidas"
                        )
                    elif response.status_code in [500, 503]:
                        logger.error(
                            f"Erro de servidor ao consultar dívidas. Status: {response.status_code}"
                        )
                        # Reporta erro ao interceptor
                        await send_api_error(
                            user_id=self.user_id,
                            service_name="iptu_pagamento",
                            api_endpoint=f"{env.DIVIDA_ATIVA_API_URL}/v2/cdas/dividas-contribuinte",
                            request_body=payload,
                            status_code=response.status_code,
                            error_message=f"Serviço de Dívida Ativa temporariamente indisponível: {response.text[:500]}",
                        )
                        raise Exception(
                            f"Serviço de Dívida Ativa temporariamente indisponível (HTTP {response.status_code})"
                        )
                    else:
                        logger.error(
                            f"Erro ao consultar dívida ativa. Status: {response.status_code}, Texto: {response.text}"
                        )
                        # Reporta erro ao interceptor
                        await send_api_error(
                            user_id=self.user_id,
                            service_name="iptu_pagamento",
                            api_endpoint=f"{env.DIVIDA_ATIVA_API_URL}/v2/cdas/dividas-contribuinte",
                            request_body=payload,
                            status_code=response.status_code,
                            error_message=f"Erro HTTP {response.status_code}: {response.text[:500]}",
                        )
                        raise Exception(
                            f"Erro ao comunicar com serviço de Dívida Ativa (HTTP {response.status_code})"
                        )

                except httpx.TimeoutException:
                    logger.error("Timeout ao consultar dívidas")
                    # Reporta erro ao interceptor com traceback
                    await send_api_error(
                        user_id=self.user_id,
                        service_name="iptu_pagamento",
                        api_endpoint=f"{env.DIVIDA_ATIVA_API_URL}/v2/cdas/dividas-contribuinte",
                        request_body=payload,
                        status_code=408,
                        error_message="Serviço de Dívida Ativa não respondeu no tempo esperado",
                        traceback=tb.format_exc(),
                    )
                    raise Exception(
                        "Serviço de Dívida Ativa não respondeu no tempo esperado"
                    )

        except Exception:
            # Re-lança exceções customizadas
            raise
        except Exception as e:
            logger.error(f"Erro ao consultar dívida ativa: {str(e)}")
            # Reporta erro ao interceptor com traceback
            await send_api_error(
                user_id=self.user_id,
                service_name="iptu_pagamento",
                api_endpoint=f"{env.DIVIDA_ATIVA_API_URL}/v2/cdas/dividas-contribuinte",
                request_body=self._preparar_payload(tipo_entrada, valor_limpo),
                status_code=0,
                error_message=f"Erro ao comunicar com serviço de Dívida Ativa: {str(e)}",
                traceback=tb.format_exc(),
            )
            raise Exception(
                f"Erro ao comunicar com serviço de Dívida Ativa: {str(e)}"
            )