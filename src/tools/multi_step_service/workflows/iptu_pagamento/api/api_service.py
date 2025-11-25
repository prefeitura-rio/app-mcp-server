"""
Serviço de API para consulta de IPTU - Integração Real

Este módulo implementa a integração com a API real da Prefeitura do Rio
para consulta de IPTU e geração de guias de pagamento.
"""

import re
import json
from typing import List, Optional, Dict, Any
import httpx
import base64
import textwrap
import datetime as dt

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
import uuid

from google.cloud import storage
from google.oauth2 import service_account

from src.config import env
from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import (
    DadosGuias,
    Guia,
    Cota,
    DadosCotas,
    Darm,
    DadosDarm,
    DadosDividaAtiva,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.api.exceptions import (
    APIUnavailableError,
    DataNotFoundError,
    AuthenticationError,
)
from loguru import logger


class IPTUAPIService:
    """
    Serviço de API para consulta de IPTU da Prefeitura do Rio.

    Integra com a API real para:
    - Consultar guias disponíveis (ConsultarGuias)
    - Consultar cotas/parcelas (ConsultarCotas)
    - Gerar DARM para pagamento (ConsultarDARM)
    - Download PDF do DARM (DownloadPdfDARM)
    """

    def __init__(self):
        """Inicializa o serviço com configurações da API."""
        self.api_base_url = env.IPTU_API_URL
        self.api_token = env.IPTU_API_TOKEN
        self.proxy = env.PROXY_URL

        logger.info(f"IPTUAPIService initialized with API URL: {self.api_base_url}")

    @staticmethod
    def _limpar_inscricao(inscricao: str) -> str:
        """
        Remove caracteres não numéricos da inscrição imobiliária.

        Args:
            inscricao: Inscrição imobiliária

        Returns:
            Inscrição apenas com números
        """
        return re.sub(r"[^0-9]", "", inscricao)

    async def _make_api_request(
        self, endpoint: str, params: Dict[str, Any], expect_json: bool = True
    ) -> Optional[Any]:
        """
        Faz requisição à API com tratamento de erros.

        Args:
            endpoint: Nome do endpoint (ex: "ConsultarGuias")
            params: Parâmetros da requisição
            expect_json: Se True, espera resposta JSON. Se False, retorna texto bruto (para PDFs)

        Returns:
            Resposta JSON da API ou texto bruto

        Raises:
            APIUnavailableError: Quando API está indisponível (timeout, 500, 503, etc.)
            AuthenticationError: Quando falha autenticação (401)
            DataNotFoundError: Quando endpoint não existe (404)
        """
        # Adiciona token aos parâmetros
        params["token"] = self.api_token

        url = f"{self.api_base_url}/{endpoint}"

        try:
            async with httpx.AsyncClient(proxy=self.proxy, timeout=30.0) as client:
                response = await client.get(url, params=params)

                if response.status_code == 200:
                    if expect_json:
                        data = response.json()
                        logger.info(f"API response successful for {endpoint}")
                        return data
                    else:
                        # Para downloads de PDF (base64)
                        logger.info(
                            f"API response successful for {endpoint} (binary/text)"
                        )
                        return response.text
                elif response.status_code == 404:
                    logger.warning(f"API endpoint not found: {endpoint}")
                    raise DataNotFoundError(f"Endpoint não encontrado: {endpoint}")
                elif response.status_code == 401:
                    logger.error(f"API authentication failed for {endpoint}")
                    raise AuthenticationError(f"Falha na autenticação do serviço IPTU")
                elif response.status_code in [500, 503]:
                    logger.error(f"API internal error for {endpoint}: {response.text}")
                    raise APIUnavailableError(
                        f"Serviço IPTU temporariamente indisponível (HTTP {response.status_code})"
                    )
                else:
                    logger.error(
                        f"API error {response.status_code} for {endpoint}: {response.text}"
                    )
                    raise APIUnavailableError(
                        f"Erro ao comunicar com serviço IPTU (HTTP {response.status_code})"
                    )

        except httpx.TimeoutException:
            logger.error(f"Timeout calling API endpoint {endpoint}")
            raise APIUnavailableError(
                "Serviço IPTU não respondeu no tempo esperado. Por favor, tente novamente."
            )
        except (APIUnavailableError, AuthenticationError, DataNotFoundError):
            # Re-lança exceções customizadas sem modificar
            raise
        except Exception as e:
            logger.error(f"Error calling API endpoint {endpoint}: {str(e)}")
            raise APIUnavailableError(f"Erro ao comunicar com serviço IPTU: {str(e)}")

    @staticmethod
    def parse_brazilian_currency(value_str: str) -> float:
        """
        Converte string de valor brasileiro para float.

        Formato brasileiro: "4.123,92" -> 4123.92

        Args:
            value_str: String com valor no formato brasileiro

        Returns:
            Valor convertido para float

        Examples:
            >>> IPTUAPIService.parse_brazilian_currency("1.234,56")
            1234.56
            >>> IPTUAPIService.parse_brazilian_currency("0,00")
            0.0
        """
        if not value_str or value_str == "0,00":
            return 0.0

        try:
            # Remove pontos (separador de milhar) e substitui vírgula por ponto
            clean_value = value_str.replace(".", "").replace(",", ".")
            return float(clean_value)
        except (ValueError, AttributeError):
            logger.warning(f"Failed to parse currency value: {value_str}")
            return 0.0

    # Alias para compatibilidade com código existente
    @staticmethod
    def _parse_brazilian_currency(value_str: str) -> float:
        """
        DEPRECATED: Use parse_brazilian_currency() instead.
        Mantido para compatibilidade com código existente.
        """
        return IPTUAPIService.parse_brazilian_currency(value_str)

    async def consultar_guias(
        self, inscricao_imobiliaria: str, exercicio: int
    ) -> Optional[DadosGuias]:
        """
        Consulta dados e guias disponíveis do IPTU por inscrição imobiliária.

        Args:
            inscricao_imobiliaria: Número da inscrição imobiliária
            exercicio: Ano do exercício fiscal (ex: 2025)

        Returns:
            DadosGuias com informações do IPTU e guias disponíveis ou None se não encontrado
        """
        # Limpa inscrição removendo caracteres não numéricos
        inscricao_clean = self._limpar_inscricao(inscricao_imobiliaria)

        # Consulta guias disponíveis
        guias_response = await self._make_api_request(
            endpoint="ConsultarGuias",
            params={"inscricao": inscricao_clean, "exercicio": str(exercicio)},
        )
        logger.debug(f"Guias response: {guias_response}")
        if not guias_response:
            logger.info(
                f"No guides found for inscricao {inscricao_clean}, exercicio {exercicio}"
            )
            return None

        if not isinstance(guias_response, list) or len(guias_response) == 0:
            logger.info(f"Empty guide list for inscricao {inscricao_clean}")
            return None

        # Converte response para objetos Guia usando Pydantic
        guias = []
        for guia_data in guias_response:
            try:
                guia = Guia(**guia_data)

                # Processa campos calculados
                guia.valor_numerico = self._parse_brazilian_currency(
                    guia.valor_iptu_original_guia
                )
                guia.valor_desconto_numerico = self._parse_brazilian_currency(
                    guia.valor_iptu_desconto_avista
                )
                guia.valor_parcelas_numerico = self._parse_brazilian_currency(
                    guia.valor_parcelas
                )
                guia.esta_quitada = guia.situacao.get("codigo") == "02"
                guia.esta_em_aberto = guia.situacao.get("codigo") == "01"

                guias.append(guia)
            except Exception as e:
                logger.warning(f"Failed to parse guia data: {guia_data}, error: {e}")
                continue

        # Filtra apenas as guias em aberto para retorno
        guias_em_aberto = [g for g in guias if g.esta_em_aberto]

        if not guias_em_aberto:
            logger.info(f"No open guides found for inscricao {inscricao_clean}")
            return None

        # Cria objeto de dados das guias usando a estrutura simplificada
        dados_guias = DadosGuias(
            inscricao_imobiliaria=inscricao_clean,
            exercicio=str(exercicio),
            guias=guias_em_aberto,
            total_guias=len(guias_em_aberto),
        )

        logger.info(
            f"IPTU data retrieved for inscricao with {len(guias)} guides available"
        )
        return dados_guias

    async def obter_cotas(
        self,
        inscricao_imobiliaria: str,
        exercicio: int,
        numero_guia: str,
        tipo_guia: Optional[str] = None,
    ) -> Optional[DadosCotas]:
        """
        Consulta cotas disponíveis para uma guia específica.

        Args:
            inscricao_imobiliaria: Número da inscrição imobiliária
            exercicio: Ano do exercício fiscal
            numero_guia: Número da guia (ex: "00")
            tipo_guia: Tipo da guia (opcional, para evitar consulta redundante)
                      No workflow, pode ser obtido dos dados das guias já carregadas:
                      guia_selecionada = next((g for g in state.data["dados_guias"].guias if g.numero_guia == numero_guia), None)
                      tipo_guia = guia_selecionada.tipo if guia_selecionada else None

        Returns:
            DadosCotas com informações das cotas disponíveis ou None se não encontrado
        """
        # Limpa inscrição removendo caracteres não numéricos
        inscricao_clean = self._limpar_inscricao(inscricao_imobiliaria)

        # Consulta cotas disponíveis para esta guia
        cotas_response = await self._make_api_request(
            endpoint="ConsultarCotas",
            params={
                "inscricao": inscricao_clean,
                "exercicio": str(exercicio),
                "guia": numero_guia,
            },
        )
        logger.debug(f"Cotas response: {cotas_response}")

        if not cotas_response or "Cotas" not in cotas_response:
            logger.warning(f"No cotas found for guia {numero_guia}")
            return None

        # Converte response para objetos Cota usando Pydantic
        cotas = []
        for cota_data in cotas_response["Cotas"]:
            try:
                cota = Cota(**cota_data)

                # Processa campos calculados
                cota.valor_numerico = self._parse_brazilian_currency(cota.valor_cota)
                cota.valor_pago_numerico = self._parse_brazilian_currency(
                    cota.valor_pago
                )
                cota.dias_atraso_numerico = (
                    int(cota.quantidade_dias_atraso)
                    if cota.quantidade_dias_atraso.isdigit()
                    else 0
                )
                cota.esta_paga = cota.situacao.get("codigo") == "01"
                cota.esta_vencida = cota.situacao.get("codigo") == "03"

                cotas.append(cota)
            except Exception as e:
                logger.warning(f"Failed to parse cota data: {cota_data}, error: {e}")
                continue

        # Calcula valor total
        valor_total = sum(c.valor_numerico for c in cotas)

        # Usa tipo da guia fornecido ou valor padrão se não informado
        if not tipo_guia:
            tipo_guia = "ORDINÁRIA"

        # Cria objeto de dados das cotas
        dados_cotas = DadosCotas(
            inscricao_imobiliaria=inscricao_clean,
            exercicio=str(exercicio),
            numero_guia=numero_guia,
            tipo_guia=tipo_guia,
            cotas=cotas,
            total_cotas=len(cotas),
            valor_total=valor_total,
        )

        logger.info(
            f"Cotas data retrieved for guia {numero_guia} with {len(cotas)} cotas"
        )
        return dados_cotas

    async def consultar_darm(
        self,
        inscricao_imobiliaria: str,
        exercicio: int,
        numero_guia: str,
        cotas_selecionadas: List[str],
    ) -> Optional[DadosDarm]:
        """
        Consulta DARM para cotas específicas de uma guia.

        Args:
            inscricao_imobiliaria: Inscrição imobiliária
            exercicio: Ano do exercício
            numero_guia: Número da guia
            cotas_selecionadas: Lista das cotas selecionadas (ex: ["01", "02"])

        Returns:
            DadosDarm com dados do DARM ou None se não encontrar
        """
        # Limpa inscrição removendo caracteres não numéricos
        inscricao_clean = self._limpar_inscricao(inscricao_imobiliaria)

        # Converte lista de cotas para string separada por vírgula
        cotas_str = ",".join(cotas_selecionadas)

        # Faz requisição para ConsultarDARM
        darm_response = await self._make_api_request(
            endpoint="ConsultarDARM",
            params={
                "inscricao": inscricao_clean,
                "exercicio": str(exercicio),
                "guia": numero_guia,
                "cotas": cotas_str,
            },
        )

        if not darm_response:
            logger.warning(f"No DARM found for guia {numero_guia} cotas {cotas_str}")
            return None

        try:
            # Cria objeto Darm usando Pydantic
            darm = Darm(**darm_response)

            # Processa valores numéricos
            darm.valor_numerico = self._parse_brazilian_currency(darm.valor_a_pagar)

            # Gera código de barras a partir da sequencia_numerica (linha digitável)
            if darm.sequencia_numerica:
                # Remove pontos e espaços para criar código de barras
                darm.codigo_barras = darm.sequencia_numerica.replace(".", "").replace(
                    " ", ""
                )

            # Cria DadosDarm
            dados_darm = DadosDarm(
                inscricao_imobiliaria=inscricao_clean,
                exercicio=str(exercicio),
                numero_guia=numero_guia,
                cotas_selecionadas=cotas_selecionadas,
                darm=darm,
            )

            logger.info(f"DARM data retrieved for guia {numero_guia} cotas {cotas_str}")
            return dados_darm

        except Exception as e:
            logger.error(
                f"Error processing DARM data: {str(e)} - Data: {darm_response}"
            )
            return None

    async def download_pdf_darm(
        self,
        inscricao_imobiliaria: str,
        exercicio: int,
        numero_guia: str,
        cotas_selecionadas: List[str],
    ) -> Optional[str]:
        """
        Faz download do PDF da DARM em formato base64.

        Args:
            inscricao_imobiliaria: Inscrição imobiliária
            exercicio: Ano do exercício
            numero_guia: Número da guia
            cotas_selecionadas: Lista das cotas selecionadas (ex: ["01", "02"])

        Returns:
            String base64 do PDF ou None se falhar
        """
        # Limpa inscrição removendo caracteres não numéricos
        inscricao_clean = self._limpar_inscricao(inscricao_imobiliaria)

        # Converte lista de cotas para string separada por vírgula
        cotas_str = ",".join(cotas_selecionadas)

        # Faz requisição esperando resposta de texto (base64)
        pdf_base64 = await self._make_api_request(
            endpoint="DownloadPdfDARM",
            params={
                "inscricao": inscricao_clean,
                "exercicio": str(exercicio),
                "guia": numero_guia,
                "cotas": cotas_str,
            },
            expect_json=False,  # Espera texto/base64, não JSON
        )

        if pdf_base64 and not pdf_base64.startswith("<!DOCTYPE"):
            # Retorna apenas se não for uma página de erro HTML
            logger.info(f"PDF downloaded successfully for inscricao {inscricao_clean}")
            signed_url = await self.upload_base64_to_gcs(base64_content=pdf_base64)
            shorted_url = await self.get_short_url(url=signed_url)
            return shorted_url
        else:
            logger.warning(f"PDF download failed or returned HTML error page")
            return None

    async def get_imovel_info(self, inscricao: str) -> Optional[Dict]:
        """
        Faz uma requisição GET na API REST de IPTU utilizando a VPN interna.

        Args:
            inscricao (str): Número da inscrição do imóvel.

        Returns:
            dict: Resposta JSON da API com endereco e proprietario, ou None se erro

        Raises:
            APIUnavailableError: Quando API está indisponível (timeout, 500, 503, etc.)
            AuthenticationError: Quando falha autenticação (401)
        """
        inscricao_clean = self._limpar_inscricao(inscricao=inscricao)

        logger.info(
            f"Iniciando consulta de imóvel via VPN para inscrição: {inscricao_clean}"
        )

        try:
            encrypted_token = encrypt_token_rsa(
                chave_publica_pem=env.WA_IPTU_PUBLIC_KEY, token=env.WA_IPTU_TOKEN
            )

            auth_header = f"Basic {encrypted_token}"

            url = f"{env.WA_IPTU_URL}/{inscricao_clean}"
            headers = {"Authorization": auth_header}

            async with httpx.AsyncClient(proxy=self.proxy, timeout=30.0) as client:
                response = await client.get(url, headers=headers)

            if response.status_code == 200:
                response_data = response.json()

                # Construindo o endereço completo
                endereco_completo = f"{response_data['tipoLogradouro']} {response_data['nomeLogradouro']}, {response_data['numPorta']}, {response_data.get('complEndereco', '')}, {response_data['bairro']}, {response_data['cep']}"

                # Retornando os dados desejados
                return {
                    "endereco": endereco_completo.strip(", "),
                    "proprietario": response_data["proprietarioPrincipal"],
                }
            elif response.status_code == 401:
                logger.error(
                    f"Erro de autenticação ao consultar imóvel. Status: {response.status_code}"
                )
                raise AuthenticationError(
                    "Falha na autenticação do serviço de dados do imóvel"
                )
            elif response.status_code in [500, 503]:
                logger.error(
                    f"Erro de servidor ao consultar imóvel. Status: {response.status_code}, Texto: {response.text}"
                )
                raise APIUnavailableError(
                    f"Serviço de dados do imóvel temporariamente indisponível (HTTP {response.status_code})"
                )
            elif response.status_code == 404:
                # 404 não é erro de API, apenas não encontrou - retorna None
                logger.warning(
                    f"Imóvel não encontrado para inscrição: {inscricao_clean}"
                )
                return None
            else:
                logger.error(
                    f"Erro ao consultar imóvel. Status: {response.status_code}, Texto: {response.text}"
                )
                raise APIUnavailableError(
                    f"Erro ao comunicar com serviço de dados do imóvel (HTTP {response.status_code})"
                )

        except httpx.TimeoutException:
            logger.error("Timeout ao consultar dados do imóvel")
            raise APIUnavailableError(
                "Serviço de dados do imóvel não respondeu no tempo esperado"
            )
        except (APIUnavailableError, AuthenticationError):
            # Re-lança exceções customizadas
            raise
        except Exception as e:
            logger.error(f"Erro ao consultar dados do imóvel: {str(e)}")
            raise APIUnavailableError(
                f"Erro ao comunicar com serviço de dados do imóvel: {str(e)}"
            )

    async def get_divida_ativa_info(self, inscricao: str) -> Optional[DadosDividaAtiva]:
        """
        Consulta a API de Dívida Ativa para obter informações sobre débitos.

        Args:
            inscricao (str): Número da inscrição do imóvel.

        Returns:
            DadosDividaAtiva com informações processadas de dívida ativa, ou None se não houver débitos.

        Raises:
            APIUnavailableError: Quando API está indisponível (timeout, 500, 503, etc.)
            AuthenticationError: Quando falha autenticação (401)
        """
        inscricao_clean = self._limpar_inscricao(inscricao=inscricao)

        logger.info(
            f"Iniciando consulta de dívida ativa para inscrição: {inscricao_clean}"
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
                        raise AuthenticationError(
                            "Falha na autenticação do serviço de Dívida Ativa"
                        )
                    elif auth_response.status_code in [500, 503]:
                        logger.error(
                            f"Erro de servidor na autenticação da Dívida Ativa: {auth_response.status_code}"
                        )
                        raise APIUnavailableError(
                            f"Serviço de Dívida Ativa temporariamente indisponível (HTTP {auth_response.status_code})"
                        )

                    auth_response_json = auth_response.json()
                    if "access_token" not in auth_response_json:
                        logger.error(
                            f"Token não encontrado na resposta de autenticação: {auth_response.status_code} - {auth_response.text}"
                        )
                        raise AuthenticationError(
                            "Falha ao obter token de autenticação da Dívida Ativa"
                        )

                    token = f'Bearer {auth_response_json["access_token"]}'
                    logger.info("Token de autenticação obtido com sucesso")

                except httpx.TimeoutException:
                    logger.error("Timeout ao autenticar na Dívida Ativa")
                    raise APIUnavailableError(
                        "Serviço de Dívida Ativa não respondeu no tempo esperado (autenticação)"
                    )

                # Consulta de dívidas
                try:
                    response = await client.post(
                        f"{env.DIVIDA_ATIVA_API_URL}/v2/cdas/dividas-contribuinte",
                        headers={"Authorization": token},
                        data={
                            "origem_solicitação": 0,
                            "inscricaoImobiliaria": inscricao_clean,
                        },
                    )

                    if response.status_code == 200:
                        response_data = response.json()
                        logger.info(f"Consulta de dívida ativa realizada com sucesso")
                        # Usa o método from_api_response do modelo para processar os dados
                        return DadosDividaAtiva.from_api_response(response_data)
                    elif response.status_code == 404:
                        # Não encontrou débitos - retorna None
                        logger.info(
                            f"Nenhuma dívida ativa encontrada para inscrição {inscricao_clean}"
                        )
                        return None
                    elif response.status_code == 401:
                        logger.error("Erro de autenticação ao consultar dívidas")
                        raise AuthenticationError(
                            "Falha na autenticação ao consultar dívidas"
                        )
                    elif response.status_code in [500, 503]:
                        logger.error(
                            f"Erro de servidor ao consultar dívidas. Status: {response.status_code}"
                        )
                        raise APIUnavailableError(
                            f"Serviço de Dívida Ativa temporariamente indisponível (HTTP {response.status_code})"
                        )
                    else:
                        logger.error(
                            f"Erro ao consultar dívida ativa. Status: {response.status_code}, Texto: {response.text}"
                        )
                        raise APIUnavailableError(
                            f"Erro ao comunicar com serviço de Dívida Ativa (HTTP {response.status_code})"
                        )

                except httpx.TimeoutException:
                    logger.error("Timeout ao consultar dívidas")
                    raise APIUnavailableError(
                        "Serviço de Dívida Ativa não respondeu no tempo esperado"
                    )

        except (APIUnavailableError, AuthenticationError):
            # Re-lança exceções customizadas
            raise
        except Exception as e:
            logger.error(f"Erro ao consultar dívida ativa: {str(e)}")
            raise APIUnavailableError(
                f"Erro ao comunicar com serviço de Dívida Ativa: {str(e)}"
            )

    async def upload_base64_to_gcs(self, base64_content) -> str:
        """
        Faz o upload de um arquivo em base64 para o Google Cloud Storage e retorna uma URL assinada válida por 7 dias.

        Args:
            base64_content (str): Conteúdo do arquivo codificado em base64.

        Returns:
            str: URL assinada para download do arquivo válida por 7 dias.
        """

        google_credentials = self.get_credentials_from_env()
        client = storage.Client(credentials=google_credentials)
        bucket = client.bucket(env.WORKFLOWS_GCS_BUCKET)

        file_data = base64.b64decode(base64_content)

        file_name = f"iptu/{uuid.uuid4()}.pdf"

        blob = bucket.blob(file_name)

        blob.upload_from_string(file_data, content_type="application/pdf")

        expiration = dt.timedelta(days=7)
        signed_url = blob.generate_signed_url(
            expiration=expiration,
        )

        return signed_url

    def get_credentials_from_env(self) -> service_account.Credentials:
        """
        Gets credentials from env vars
        """
        info: dict = json.loads(base64.b64decode(env.WORKFLOWS_GCP_SERVICE_ACCOUNT))
        return service_account.Credentials.from_service_account_info(info)

    async def get_short_url(self, url) -> Optional[str]:
        """
        Envia uma URL para o endpoint de encurtamento de URL e retorna a URL encurtada.

        :param url: A URL que será encurtada.
        :return: A URL encurtada como string.
        """
        api_url = f"{env.SHORT_API_URL}/link/api/urls"
        headers = {
            "Authorization": f"Bearer {env.SHORT_API_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "description": "Link for IPTU generated pdf",
            "destination": url,
            "title": "IPTU EAI Workflow",
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(api_url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                logger.info(f"URL shortened successfully: {data}")
                return f"{env.SHORT_API_URL}/link/{data['short_path']}"
            except httpx.RequestError as e:
                logger.error(f"Erro ao encurtar a URL: {e}")
                return None


def encrypt_token_rsa(chave_publica_pem: str, token: str) -> str:
    utc_now = dt.datetime.now(dt.timezone.utc)
    datahora_str = utc_now.strftime("%d/%m/%Y %H:%M:%S")

    dataHoraToken = datahora_str + token

    dataHoraToken_bytes = dataHoraToken.encode("utf-16-le")

    pem_formatado = convert_base64_to_pem(chave_publica_pem)
    public_key = serialization.load_pem_public_key(pem_formatado.encode())

    encrypted = public_key.encrypt(dataHoraToken_bytes, padding.PKCS1v15())

    return base64.b64encode(encrypted).decode("ascii")


def convert_base64_to_pem(base64_key: str) -> str:
    if "BEGIN PUBLIC KEY" in base64_key:
        return base64_key.strip()
    wrapped = textwrap.fill(base64_key, 64)
    return f"-----BEGIN PUBLIC KEY-----\n{wrapped}\n-----END PUBLIC KEY-----"
