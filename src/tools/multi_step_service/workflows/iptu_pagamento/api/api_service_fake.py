"""
Serviço de API FAKE para consulta de IPTU - Mock Data para Testes

Este módulo implementa a mesma interface do api_service.py porém com dados mockados
para permitir testes completos de todos os cenários possíveis.
"""

import re
from typing import List, Optional, Dict, Any
from datetime import datetime, date

from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import (
    DadosGuias,
    Guia,
    Cota,
    DadosCotas,
    Darm,
    DadosDarm,
    CotaDarm,
    DadosDividaAtiva,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.api.exceptions import (
    APIUnavailableError,
    DataNotFoundError,
    AuthenticationError,
)
from loguru import logger


class IPTUAPIServiceFake:
    """
    Serviço de API FAKE para consulta de IPTU com dados mockados.

    Implementa a mesma interface do IPTUAPIService real para permitir
    testes completos de todos os cenários possíveis.

    Suporta simulação de erros de API através de inscrições especiais:
    - 77777777777777: Simula APIUnavailableError
    - 88888888888888: Simula AuthenticationError
    - 99999999990000: Simula timeout na consulta de guias
    - 99999999990001: Simula erro 500 na consulta de cotas
    - 99999999990002: Simula erro 503 na geração de DARM
    """

    def __init__(self, user_id: str = "unknown"):
        """
        Inicializa o serviço fake.

        Args:
            user_id: ID do usuário (para compatibilidade com API real)
        """
        self.user_id = user_id
        logger.info("IPTUAPIServiceFake initialized - MOCK DATA MODE")

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

    @staticmethod
    def parse_brazilian_currency(value_str: str) -> float:
        """
        Converte string de valor brasileiro para float.

        Args:
            value_str: String com valor no formato brasileiro

        Returns:
            Valor convertido para float
        """
        if not value_str or value_str == "0,00":
            return 0.0

        try:
            clean_value = value_str.replace(".", "").replace(",", ".")
            return float(clean_value)
        except (ValueError, AttributeError):
            return 0.0

    # Alias para compatibilidade
    @staticmethod
    def _parse_brazilian_currency(value_str: str) -> float:
        """DEPRECATED: Use parse_brazilian_currency() instead."""
        return IPTUAPIServiceFake.parse_brazilian_currency(value_str)

    def _get_mock_guias_data(
        self, inscricao_clean: str, exercicio: int
    ) -> Optional[List[Dict]]:
        """
        Retorna dados mockados de guias baseados na inscrição e exercício.

        Cenários de teste baseados na inscrição:
        - 01234567890123: IPTU ORDINÁRIA + EXTRAORDINÁRIA (ambas em aberto)
        - 11111111111111: Apenas IPTU ORDINÁRIA em aberto
        - 22222222222222: Apenas IPTU EXTRAORDINÁRIA em aberto
        - 33333333333333: Todas as guias quitadas
        - 44444444444444: IPTU ORDINÁRIA com valor alto (teste desconto à vista)
        - 55555555555555: IPTU ORDINÁRIA com valores baixos
        - 66666666666666: Múltiplas guias EXTRAORDINÁRIAS (01, 02)
        - 12345678: Nenhuma guia para ano 2024 (lista vazia), tem guias em 2025
        - 10000000: Nenhuma guia (migrado para dívida ativa - parcelamento)
        - 20000000: Nenhuma guia (migrado para dívida ativa - CDAs)
        - 30000000: Nenhuma guia (migrado para dívida ativa - EFs)
        - Qualquer outra: Nenhuma guia encontrada
        """

        if exercicio < 2020 or exercicio > 2025:
            # Exercício fora do range válido
            return None

        if inscricao_clean == "12345678":
            # Cenário específico: Nenhuma guia para 2024, mas tem para 2025
            if exercicio == 2024:
                return []  # Lista vazia (sem guias para este ano)
            else:
                # Tem guias para outros anos (ex: 2025)
                return [
                    {
                        "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                        "Inscricao": inscricao_clean,
                        "Exercicio": str(exercicio),
                        "NGuia": "00",
                        "Tipo": "ORDINÁRIA",
                        "ValorIPTUOriginalGuia": "1.200,00",
                        "DataVenctoDescCotaUnica": "07/02/2025",
                        "QuantDiasEmAtraso": "0",
                        "PercentualDescCotaUnica": "00007",
                        "ValorIPTUDescontoAvista": "84,00",
                        "ValorParcelas": "37,50",
                        "CreditoNotaCarioca": "0,00",
                        "CreditoDECAD": "0,00",
                        "CreditoIsencao": "0,00",
                        "CreditoCotaUnica": "84,00",
                        "ValorQuitado": "0,00",
                        "DataQuitacao": "",
                        "Deposito": "N",
                    }
                ]

        if inscricao_clean == "01234567890123":
            # Cenário padrão: IPTU ORDINÁRIA + EXTRAORDINÁRIA
            return [
                {
                    "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                    "Inscricao": inscricao_clean,
                    "Exercicio": str(exercicio),
                    "NGuia": "00",
                    "Tipo": "ORDINÁRIA",
                    "ValorIPTUOriginalGuia": "2.878,00",
                    "DataVenctoDescCotaUnica": "07/02/2024",
                    "QuantDiasEmAtraso": "290",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "201,46",
                    "ValorParcelas": "89,44",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "201,46",
                    "ValorQuitado": "0,00",
                    "DataQuitacao": "",
                    "Deposito": "N",
                },
                {
                    "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                    "Inscricao": inscricao_clean,
                    "Exercicio": str(exercicio),
                    "NGuia": "01",
                    "Tipo": "EXTRAORDINÁRIA",
                    "ValorIPTUOriginalGuia": "520,00",
                    "DataVenctoDescCotaUnica": "07/02/2024",
                    "QuantDiasEmAtraso": "290",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "36,40",
                    "ValorParcelas": "86,67",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "36,40",
                    "ValorQuitado": "0,00",
                    "DataQuitacao": "",
                    "Deposito": "N",
                },
            ]
        elif inscricao_clean == "11111111111111":
            # Apenas IPTU ORDINÁRIA
            return [
                {
                    "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                    "Inscricao": inscricao_clean,
                    "Exercicio": str(exercicio),
                    "NGuia": "00",
                    "Tipo": "ORDINÁRIA",
                    "ValorIPTUOriginalGuia": "1.500,00",
                    "DataVenctoDescCotaUnica": "07/02/2024",
                    "QuantDiasEmAtraso": "290",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "105,00",
                    "ValorParcelas": "46,88",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "105,00",
                    "ValorQuitado": "0,00",
                    "DataQuitacao": "",
                    "Deposito": "N",
                }
            ]
        elif inscricao_clean == "22222222222222":
            # Apenas IPTU EXTRAORDINÁRIA
            return [
                {
                    "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                    "Inscricao": inscricao_clean,
                    "Exercicio": str(exercicio),
                    "NGuia": "01",
                    "Tipo": "EXTRAORDINÁRIA",
                    "ValorIPTUOriginalGuia": "320,00",
                    "DataVenctoDescCotaUnica": "07/02/2024",
                    "QuantDiasEmAtraso": "290",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "22,40",
                    "ValorParcelas": "53,33",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "22,40",
                    "ValorQuitado": "0,00",
                    "DataQuitacao": "",
                    "Deposito": "N",
                }
            ]
        elif inscricao_clean == "33333333333333":
            # Todas quitadas - filtradas fora, então retorna lista vazia
            return [
                {
                    "Situacao": {"codigo": "02", "descricao": "QUITADA"},
                    "Inscricao": inscricao_clean,
                    "Exercicio": str(exercicio),
                    "NGuia": "00",
                    "Tipo": "ORDINÁRIA",
                    "ValorIPTUOriginalGuia": "2.878,00",
                    "DataVenctoDescCotaUnica": "07/02/2024",
                    "QuantDiasEmAtraso": "0",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "0,00",
                    "ValorParcelas": "0,00",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "201,46",
                    "ValorQuitado": "2.676,54",
                    "DataQuitacao": "28/01/2024",
                    "Deposito": "N",
                }
            ]
        elif inscricao_clean == "44444444444444":
            # IPTU ORDINÁRIA com valor alto (teste desconto)
            return [
                {
                    "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                    "Inscricao": inscricao_clean,
                    "Exercicio": str(exercicio),
                    "NGuia": "00",
                    "Tipo": "ORDINÁRIA",
                    "ValorIPTUOriginalGuia": "8.500,00",
                    "DataVenctoDescCotaUnica": "07/02/2024",
                    "QuantDiasEmAtraso": "290",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "595,00",
                    "ValorParcelas": "265,63",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "595,00",
                    "ValorQuitado": "0,00",
                    "DataQuitacao": "",
                    "Deposito": "N",
                }
            ]
        elif inscricao_clean == "55555555555555":
            # IPTU ORDINÁRIA com valores baixos
            return [
                {
                    "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                    "Inscricao": inscricao_clean,
                    "Exercicio": str(exercicio),
                    "NGuia": "00",
                    "Tipo": "ORDINÁRIA",
                    "ValorIPTUOriginalGuia": "180,00",
                    "DataVenctoDescCotaUnica": "07/02/2024",
                    "QuantDiasEmAtraso": "290",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "12,60",
                    "ValorParcelas": "5,63",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "12,60",
                    "ValorQuitado": "0,00",
                    "DataQuitacao": "",
                    "Deposito": "N",
                }
            ]
        elif inscricao_clean == "66666666666666":
            # Múltiplas guias EXTRAORDINÁRIAS (01, 02)
            return [
                {
                    "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                    "Inscricao": inscricao_clean,
                    "Exercicio": str(exercicio),
                    "NGuia": "01",
                    "Tipo": "EXTRAORDINÁRIA",
                    "ValorIPTUOriginalGuia": "450,00",
                    "DataVenctoDescCotaUnica": "07/02/2024",
                    "QuantDiasEmAtraso": "290",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "31,50",
                    "ValorParcelas": "75,00",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "31,50",
                    "ValorQuitado": "0,00",
                    "DataQuitacao": "",
                    "Deposito": "N",
                },
                {
                    "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                    "Inscricao": inscricao_clean,
                    "Exercicio": str(exercicio),
                    "NGuia": "02",
                    "Tipo": "EXTRAORDINÁRIA",
                    "ValorIPTUOriginalGuia": "380,00",
                    "DataVenctoDescCotaUnica": "07/02/2024",
                    "QuantDiasEmAtraso": "290",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "26,60",
                    "ValorParcelas": "63,33",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "26,60",
                    "ValorQuitado": "0,00",
                    "DataQuitacao": "",
                    "Deposito": "N",
                },
            ]
        elif inscricao_clean in ["10000000", "20000000", "30000000"]:
            # Cenários de dívida ativa: retorna lista vazia (sem guias de IPTU)
            # Estas inscrições têm débitos na dívida ativa
            return []
        else:
            # Qualquer outra inscrição: não encontrada
            return None

    def _get_mock_cotas_data(
        self, inscricao_clean: str, exercicio: int, numero_guia: str
    ) -> Optional[Dict]:
        """
        Retorna dados mockados de cotas baseados na inscrição, exercício e número da guia.
        """

        # Verifica se a inscrição tem guias (reutiliza lógica)
        guias_data = self._get_mock_guias_data(inscricao_clean, exercicio)
        if not guias_data:
            return None

        # Verifica se a guia específica existe
        guia_existe = any(g["NGuia"] == numero_guia for g in guias_data)
        if not guia_existe:
            return None

        if numero_guia == "00":  # IPTU ORDINÁRIA
            # Cotas IPTU ORDINÁRIA padrão - 32 cotas
            cotas = []
            for i in range(1, 33):
                numero_cota = f"{i:02d}"
                valor_cota = "89,44" if inscricao_clean == "01234567890123" else "46,88"

                # Algumas cotas com situações diferentes para teste
                if i <= 3:
                    situacao = {"codigo": "01", "descricao": "PAGA"}
                    valor_pago = valor_cota
                    data_pagamento = f"15/0{i}/2024"
                elif i <= 25:
                    situacao = {"codigo": "02", "descricao": "EM ABERTO"}
                    valor_pago = "0,00"
                    data_pagamento = ""
                else:
                    situacao = {"codigo": "03", "descricao": "VENCIDA"}
                    valor_pago = "0,00"
                    data_pagamento = ""

                cotas.append(
                    {
                        "Situacao": situacao,
                        "NCota": numero_cota,
                        "ValorCota": valor_cota,
                        "DataVencimento": f"07/{i if i <= 12 else i-12:02d}/2024",
                        "ValorPago": valor_pago,
                        "DataPagamento": data_pagamento,
                        "QuantDiasEmAtraso": (
                            "290" if situacao["codigo"] == "03" else "0"
                        ),
                    }
                )

            return {"Cotas": cotas}

        elif numero_guia in ["01", "02"]:  # IPTU EXTRAORDINÁRIA
            # Cotas IPTU EXTRAORDINÁRIA - 6 cotas
            cotas = []
            for i in range(1, 7):
                numero_cota = f"{i:02d}"

                # Valores diferentes baseados na inscrição e número da guia
                if inscricao_clean == "01234567890123":
                    valor_cota = "86,67"
                elif inscricao_clean == "66666666666666" and numero_guia == "01":
                    valor_cota = "75,00"
                elif inscricao_clean == "66666666666666" and numero_guia == "02":
                    valor_cota = "63,33"
                else:
                    valor_cota = "53,33"

                situacao = {"codigo": "02", "descricao": "EM ABERTO"}
                valor_pago = "0,00"
                data_pagamento = ""

                cotas.append(
                    {
                        "Situacao": situacao,
                        "NCota": numero_cota,
                        "ValorCota": valor_cota,
                        "DataVencimento": f"07/{(i*2):02d}/2024",
                        "ValorPago": valor_pago,
                        "DataPagamento": data_pagamento,
                        "QuantDiasEmAtraso": "0",
                    }
                )

            return {"Cotas": cotas}

        return None

    def _get_mock_darm_data(
        self,
        inscricao_clean: str,
        exercicio: int,
        numero_guia: str,
        cotas_selecionadas: List[str],
    ) -> Optional[Dict]:
        """
        Retorna dados mockados de DARM baseados nos parâmetros.
        """

        # Verifica se tem cotas para esta guia
        cotas_data = self._get_mock_cotas_data(inscricao_clean, exercicio, numero_guia)
        if not cotas_data:
            return None

        # Verifica se as cotas selecionadas existem
        cotas_disponiveis = [c["NCota"] for c in cotas_data["Cotas"]]
        for cota in cotas_selecionadas:
            if cota not in cotas_disponiveis:
                return None

        # Calcula valor total das cotas selecionadas
        valor_total = 0.0
        cotas_darm = []

        for cota_data in cotas_data["Cotas"]:
            if cota_data["NCota"] in cotas_selecionadas:
                valor_cota = self._parse_brazilian_currency(cota_data["ValorCota"])
                valor_total += valor_cota

                cotas_darm.append(
                    {"ncota": cota_data["NCota"], "valor": cota_data["ValorCota"]}
                )

        # Formata valor total no padrão brasileiro
        valor_total_str = (
            f"{valor_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        )

        # Gera linha digitável fictícia
        sequencia_base = f"310-7.{inscricao_clean[:8]}.{exercicio}.{numero_guia}.{len(cotas_selecionadas):02d}"
        sequencia_numerica = f"{sequencia_base}.{int(valor_total * 100):08d}"

        return {
            "Cotas": cotas_darm,
            "Inscricao": inscricao_clean,
            "Exercicio": str(exercicio),
            "NGuia": numero_guia,
            "Tipo": "ORDINÁRIA",
            "DataVencimento": "29/11/2024",
            "ValorIPTUOriginal": "860,00",
            "ValorDARM": valor_total_str,
            "ValorDescCotaUnica": "0,00",
            "CreditoNotaCarioca": "0,00",
            "CreditoDECAD": "0,00",
            "CreditoIsencao": "0,00",
            "CreditoEmissao": "0,00",
            "ValorAPagar": valor_total_str,
            "SequenciaNumerica": sequencia_numerica,
            "DescricaoDARM": f"DARM por cota ref.cotas {','.join(cotas_selecionadas)}",
            "CodReceita": "310-7",
            "DesReceita": "RECEITA DE PAGAMENTO",
            "Endereco": "RUA EXEMPLO, 123 - CENTRO",
            "Nome": "PROPRIETARIO TESTE",
        }

    async def consultar_guias(
        self, inscricao_imobiliaria: str, exercicio: int
    ) -> Optional[DadosGuias]:
        """
        Consulta dados e guias disponíveis do IPTU por inscrição imobiliária (MOCK).

        Args:
            inscricao_imobiliaria: Número da inscrição imobiliária
            exercicio: Ano do exercício fiscal (ex: 2025)

        Returns:
            DadosGuias com informações do IPTU e guias disponíveis ou None se não encontrado

        Raises:
            APIUnavailableError: Para inscrições 77777777777777 ou 99999999990000
            AuthenticationError: Para inscrição 88888888888888
        """
        # Limpa inscrição removendo caracteres não numéricos
        inscricao_clean = self._limpar_inscricao(inscricao_imobiliaria)

        logger.info(
            f"FAKE API: Consulting guides for inscription {inscricao_clean}, year {exercicio}"
        )

        # Simulação de erros baseada em inscrições especiais
        if inscricao_clean == "77777777777777":
            logger.error("FAKE API: Simulating APIUnavailableError (generic)")
            raise APIUnavailableError(
                "Serviço IPTU temporariamente indisponível (erro simulado)"
            )

        if inscricao_clean == "88888888888888":
            logger.error("FAKE API: Simulating AuthenticationError")
            raise AuthenticationError(
                "Falha na autenticação do serviço IPTU (erro simulado)"
            )

        if inscricao_clean == "99999999990000":
            logger.error("FAKE API: Simulating APIUnavailableError (timeout)")
            raise APIUnavailableError(
                "Serviço IPTU não respondeu no tempo esperado. Por favor, tente novamente. (erro simulado)"
            )

        # Busca dados mockados
        guias_response = self._get_mock_guias_data(inscricao_clean, exercicio)

        if not guias_response:
            logger.info(
                f"FAKE API: No guides found for inscription {inscricao_clean}, year {exercicio}"
            )
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
                logger.warning(
                    f"FAKE API: Failed to parse guide data: {guia_data}, error: {e}"
                )
                continue

        # Filtra apenas as guias em aberto para retorno
        guias_em_aberto = [g for g in guias if g.esta_em_aberto]

        if not guias_em_aberto:
            logger.info(
                f"FAKE API: No open guides found for inscription {inscricao_clean}"
            )
            return None

        # Cria objeto de dados das guias usando a estrutura simplificada
        dados_guias = DadosGuias(
            inscricao_imobiliaria=inscricao_clean,
            exercicio=str(exercicio),
            guias=guias_em_aberto,
            total_guias=len(guias_em_aberto),
        )

        logger.info(
            f"FAKE API: IPTU data retrieved for inscription with {len(guias)} guides available"
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
        Consulta cotas disponíveis para uma guia específica (MOCK).

        Args:
            inscricao_imobiliaria: Número da inscrição imobiliária
            exercicio: Ano do exercício fiscal
            numero_guia: Número da guia (ex: "00")
            tipo_guia: Tipo da guia (opcional)

        Returns:
            DadosCotas com informações das cotas disponíveis ou None se não encontrado

        Raises:
            APIUnavailableError: Para inscrição 99999999990001
        """
        # Limpa inscrição removendo caracteres não numéricos
        inscricao_clean = self._limpar_inscricao(inscricao_imobiliaria)

        logger.info(
            f"FAKE API: Consulting cotas for inscription {inscricao_clean}, guide {numero_guia}"
        )

        # Simulação de erro 500 na consulta de cotas
        if inscricao_clean == "99999999990001":
            logger.error(
                "FAKE API: Simulating APIUnavailableError (500) on obter_cotas"
            )
            raise APIUnavailableError(
                "Serviço IPTU temporariamente indisponível (HTTP 500) (erro simulado)"
            )

        # Consulta cotas disponíveis para esta guia
        cotas_response = self._get_mock_cotas_data(
            inscricao_clean, exercicio, numero_guia
        )

        if not cotas_response or "Cotas" not in cotas_response:
            logger.warning(f"FAKE API: No cotas found for guide {numero_guia}")
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
                logger.warning(
                    f"FAKE API: Failed to parse cota data: {cota_data}, error: {e}"
                )
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
            f"FAKE API: Cotas data retrieved for guide {numero_guia} with {len(cotas)} cotas"
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
        Consulta DARM para cotas específicas de uma guia (MOCK).

        Args:
            inscricao_imobiliaria: Inscrição imobiliária
            exercicio: Ano do exercício
            numero_guia: Número da guia
            cotas_selecionadas: Lista das cotas selecionadas (ex: ["01", "02"])

        Returns:
            DadosDarm com dados do DARM ou None se não encontrar

        Raises:
            APIUnavailableError: Para inscrição 99999999990002
        """
        # Limpa inscrição removendo caracteres não numéricos
        inscricao_clean = self._limpar_inscricao(inscricao_imobiliaria)

        # Converte lista de cotas para string separada por vírgula
        cotas_str = ",".join(cotas_selecionadas)

        logger.info(
            f"FAKE API: Consulting DARM for inscription {inscricao_clean}, guide {numero_guia}, cotas {cotas_str}"
        )

        # Simulação de erro 503 na geração de DARM
        if inscricao_clean == "99999999990002":
            logger.error(
                "FAKE API: Simulating APIUnavailableError (503) on consultar_darm"
            )
            raise APIUnavailableError(
                "Serviço IPTU temporariamente indisponível (HTTP 503) (erro simulado)"
            )

        # Faz consulta mockada
        darm_response = self._get_mock_darm_data(
            inscricao_clean, exercicio, numero_guia, cotas_selecionadas
        )

        if not darm_response:
            logger.warning(
                f"FAKE API: No DARM found for guide {numero_guia} cotas {cotas_str}"
            )
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

            logger.info(
                f"FAKE API: DARM data retrieved for guide {numero_guia} cotas {cotas_str}"
            )
            return dados_darm

        except Exception as e:
            logger.error(
                f"FAKE API: Error processing DARM data: {str(e)} - Data: {darm_response}"
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
        Faz download do PDF da DARM em formato base64 (MOCK).

        Args:
            inscricao_imobiliaria: Inscrição imobiliária
            exercicio: Ano do exercício
            numero_guia: Número da guia
            cotas_selecionadas: Lista das cotas selecionadas (ex: ["01", "02"])

        Returns:
            String base64 do PDF mockado ou None se falhar
        """
        # Limpa inscrição removendo caracteres não numéricos
        inscricao_clean = self._limpar_inscricao(inscricao_imobiliaria)

        # Converte lista de cotas para string separada por vírgula
        cotas_str = ",".join(cotas_selecionadas)

        logger.info(
            f"FAKE API: Downloading PDF for inscription {inscricao_clean}, guide {numero_guia}, cotas {cotas_str}"
        )

        # Verifica se tem DARM válido para essas cotas
        darm_data = self._get_mock_darm_data(
            inscricao_clean, exercicio, numero_guia, cotas_selecionadas
        )

        if not darm_data:
            logger.warning(f"FAKE API: PDF download failed - no DARM data available")
            return None

        # Gera PDF base64 mockado (um PDF mínimo válido)
        # Este é um PDF extremamente simples em base64 para teste
        fake_pdf_base64 = "www.fake.url.com"

        logger.info(
            f"FAKE API: PDF downloaded successfully for inscription {inscricao_clean}"
        )
        return fake_pdf_base64

    async def get_imovel_info(self, inscricao):
        return {
            "endereco": "Rua Fake, Bairro Fake, 0000-0000",
            "proprietario": "Fake da Silva",
        }

    async def get_divida_ativa_info(self, inscricao: str) -> Optional[DadosDividaAtiva]:
        """
        Consulta a API de Dívida Ativa para obter informações sobre débitos (MOCK).

        Args:
            inscricao: Número da inscrição do imóvel

        Returns:
            DadosDividaAtiva com informações processadas de dívida ativa, ou None se não houver débitos

        Cenários de teste baseados na inscrição:
        - 10000000: Tem parcelamento ativo na dívida ativa
        - 20000000: Tem CDAs não ajuizadas
        - 30000000: Tem EFs não parceladas
        - Outros: Sem débitos na dívida ativa
        """
        inscricao_clean = self._limpar_inscricao(inscricao)

        logger.info(
            f"FAKE API: Consulting dívida ativa for inscription {inscricao_clean}"
        )

        # Cria o mock response baseado na inscrição
        mock_response = None

        if inscricao_clean == "10000000":
            # Cenário: Parcelamento ativo
            mock_response = {
                "success": True,
                "data": {
                    "dataVencimento": "25/11/2025",
                    "saldoTotalDivida": "R$0,00",
                    "enderecoImovel": "RUA BARATA RIBEIRO, 264 - APT 902",
                    "bairroImovel": "COPACABANA",
                    "pdf": None,
                    "urlPdf": None,
                    "debitosNaoParceladosComSaldoTotal": {
                        "cdasNaoAjuizadasNaoParceladas": [],
                        "efsNaoParceladas": [],
                        "saldoTotalNaoParcelado": "R$0,00",
                    },
                    "guiasParceladasComSaldoTotal": {
                        "guiasParceladas": [
                            {
                                "qtdeParcelas": "84",
                                "qtdPagas": "9",
                                "dataUltimoPagamento": "02/06/2025",
                                "nomeRequerente": "MAURICEA BATISTA DO CARMO",
                                "descricaoTipoPagamento": "Parcelamento Compartilhado",
                                "numero": "2024/0256907",
                                "descricaoSituacaoGuia": "Concedido",
                                "valorTotalGuia": "R$15.000,00",
                            }
                        ],
                        "saldoTotalParcelado": "R$0,00",
                    },
                },
            }
        elif inscricao_clean == "20000000":
            # Cenário: CDAs não ajuizadas
            mock_response = {
                "success": True,
                "data": {
                    "dataVencimento": "25/11/2025",
                    "saldoTotalDivida": "R$5.000,00",
                    "enderecoImovel": "AV ATLÂNTICA, 1000",
                    "bairroImovel": "COPACABANA",
                    "pdf": None,
                    "urlPdf": None,
                    "debitosNaoParceladosComSaldoTotal": {
                        "cdasNaoAjuizadasNaoParceladas": [
                            {
                                "numero": "2024/123456",
                                "exercicio": "2024",
                                "valorOriginal": "R$3.000,00",
                            },
                            {
                                "numero": "2023/654321",
                                "exercicio": "2023",
                                "valorOriginal": "R$2.000,00",
                            },
                        ],
                        "efsNaoParceladas": [],
                        "saldoTotalNaoParcelado": "R$5.000,00",
                    },
                    "guiasParceladasComSaldoTotal": {
                        "guiasParceladas": [],
                        "saldoTotalParcelado": "R$0,00",
                    },
                },
            }
        elif inscricao_clean == "30000000":
            # Cenário: EFs não parceladas
            mock_response = {
                "success": True,
                "data": {
                    "dataVencimento": "25/11/2025",
                    "saldoTotalDivida": "R$10.000,00",
                    "enderecoImovel": "RUA VISCONDE DE PIRAJÁ, 500",
                    "bairroImovel": "IPANEMA",
                    "pdf": None,
                    "urlPdf": None,
                    "debitosNaoParceladosComSaldoTotal": {
                        "cdasNaoAjuizadasNaoParceladas": [],
                        "efsNaoParceladas": [
                            {
                                "numeroEF": "2024/789012",
                                "numeroProcesso": "0123456-78.2024.8.19.0001",
                                "valorOriginal": "R$10.000,00",
                            }
                        ],
                        "saldoTotalNaoParcelado": "R$10.000,00",
                    },
                    "guiasParceladasComSaldoTotal": {
                        "guiasParceladas": [],
                        "saldoTotalParcelado": "R$0,00",
                    },
                },
            }
        else:
            # Cenário: Sem débitos na dívida ativa
            logger.info(
                f"FAKE API: No dívida ativa found for inscription {inscricao_clean}"
            )
            return None

        # Usa o método from_api_response do modelo para processar os dados mockados
        return DadosDividaAtiva.from_api_response(mock_response)
