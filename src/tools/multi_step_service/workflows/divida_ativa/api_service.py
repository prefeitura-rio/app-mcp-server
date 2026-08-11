import os
from typing import Optional, Dict, Any

from loguru import logger

from src.config import env
from src.tools.divida_ativa import pgm_api
from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import (
    DadosDividaAtiva,
)


class DividaAtivaAPIService:
    CONSULTA_DEBITOS_CONSUMIDOR = "consultar-dividas-contribuinte"
    CONSULTA_DEBITOS_ENDPOINT = "v2/cdas/dividas-contribuinte"
    EMITIR_GUIA_A_VISTA_CONSUMIDOR = "emitir-guia-vista"
    EMITIR_GUIA_A_VISTA_ENDPOINT = "v2/guiapagamento/emitir/avista"
    ORIGEM_SOLICITACAO = "origem_solicitação"
    CONSULTA_CONFIGS = {
        "numero_guia_pagamento": {
            "endpoint": CONSULTA_DEBITOS_ENDPOINT,
            "consumer": CONSULTA_DEBITOS_CONSUMIDOR,
            "value_field": "numeroGuiaPagamento",
        },
        "cpf_cnpj": {
            "endpoint": CONSULTA_DEBITOS_ENDPOINT,
            "consumer": CONSULTA_DEBITOS_CONSUMIDOR,
            "value_field": "cpfCnpj",
        },
        "inscricao_imobiliaria": {
            "endpoint": CONSULTA_DEBITOS_ENDPOINT,
            "consumer": CONSULTA_DEBITOS_CONSUMIDOR,
            "value_field": "inscricaoImobiliaria",
        },
        "auto_infracao": {
            "endpoint": CONSULTA_DEBITOS_ENDPOINT,
            "consumer": CONSULTA_DEBITOS_CONSUMIDOR,
            "value_field": "numeroAutoInfracao",
            "extra_fields": {"ano": "anoAutoInfracao"},
        },
        "cda": {
            "endpoint": CONSULTA_DEBITOS_ENDPOINT,
            "consumer": CONSULTA_DEBITOS_CONSUMIDOR,
            "value_field": "cda",
        },
        "execucao_fiscal": {
            "endpoint": CONSULTA_DEBITOS_ENDPOINT,
            "consumer": CONSULTA_DEBITOS_CONSUMIDOR,
            "value_field": "numeroExecucaoFiscal",
        },
    }
    NODE_TO_TIPO_CONSULTA = {
        "consultar_cpf_cnpj": "cpf_cnpj",
        "consultar_inscricao_imobiliaria": "inscricao_imobiliaria",
        "consultar_auto_infracao": "auto_infracao",
        "consultar_cda": "cda",
        "consultar_execucao_fiscal": "execucao_fiscal",
    }
    ERROR_SOURCE = {
        "source": "mcp",
        "tool": "multi_step_service",
        "workflow": "divida_ativa",
    }

    def __init__(self, user_id: str = "unknown"):
        self.api_base_url = env.CHATBOT_PGM_API_URL
        self.access_key = env.CHATBOT_PGM_ACCESS_KEY
        self.proxy = env.PROXY_URL
        self.user_id = user_id

    def _build_url(self, path: str) -> str:
        """
        Monta URLs da Dívida Ativa aceitando base com ou sem o sufixo /api.
        """
        base_url = self.api_base_url.rstrip("/")
        normalized_path = path.strip("/")

        if base_url.endswith("/api") and normalized_path.startswith("api/"):
            normalized_path = normalized_path.removeprefix("api/")

        return f"{base_url}/{normalized_path}"

    def _limpar_valor(self, valor: str) -> str:
        """Remove caracteres não numéricos do valor informado pelo usuário."""
        return "".join(filter(str.isdigit, valor or ""))

    def _debug_api_enabled(self) -> bool:
        return os.getenv("DIVIDA_ATIVA_DEBUG_API", "").lower() in {
            "1",
            "true",
            "yes",
            "sim",
        }

    def _debug_response_summary(
        self,
        response_data: Dict[str, Any],
        parsed: DadosDividaAtiva,
    ) -> None:
        if not self._debug_api_enabled():
            return

        data = response_data.get("data", response_data)
        if isinstance(data, list):
            first_item = data[0] if data else None
            logger.info(
                "DEBUG Dívida Ativa API: data_type=list data_len={} first_item_keys={} "
                "parsed_counts={{cdas: {}, efs: {}, parcelamentos: {}}} parsed_tem_divida={}",
                len(data),
                list(first_item.keys()) if isinstance(first_item, dict) else None,
                len(parsed.cdas),
                len(parsed.efs),
                len(parsed.parcelamentos),
                parsed.tem_divida_ativa,
            )
            return

        if not isinstance(data, dict):
            logger.info(
                "DEBUG Dívida Ativa API: data_type={} parsed_tem_divida={}",
                type(data).__name__,
                parsed.tem_divida_ativa,
            )
            return

        debitos_nao_parcelados = data.get("debitosNaoParceladosComSaldoTotal", {})
        guias_parceladas = data.get("guiasParceladasComSaldoTotal", {})
        logger.info(
            "DEBUG Dívida Ativa API: top_keys={} data_keys={} "
            "raw_counts={{cdasNaoAjuizadasNaoParceladas: {}, efsNaoParceladas: {}, "
            "guiasParceladas: {}}} parsed_counts={{cdas: {}, efs: {}, parcelamentos: {}}} "
            "parsed_tem_divida={}",
            list(response_data.keys()),
            list(data.keys()),
            len(debitos_nao_parcelados.get("cdasNaoAjuizadasNaoParceladas", []) or []),
            len(debitos_nao_parcelados.get("efsNaoParceladas", []) or []),
            len(guias_parceladas.get("guiasParceladas", []) or []),
            len(parsed.cdas),
            len(parsed.efs),
            len(parsed.parcelamentos),
            parsed.tem_divida_ativa,
        )

    def _preparar_payload(
        self, tipo_consulta: str, valor: str, dados: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Prepara o payload para a API de acordo com o tipo de entrada.

        Args:
            tipo_consulta (str): Tipo de consulta escolhido no workflow.
            valor (str): Valor limpo da entrada.
            dados (Optional[Dict[str, Any]]): Campos extras coletados no nó.

        Returns:
            Dict[str, Any]: Payload para a API.
        """
        dados = dados or {}
        config = self._get_consulta_config(tipo_consulta)
        payload = {self.ORIGEM_SOLICITACAO: 0}

        payload[config["value_field"]] = self._limpar_valor(valor)

        for input_field, api_field in config.get("extra_fields", {}).items():
            payload[api_field] = self._limpar_valor(str(dados.get(input_field, "")))

        return payload

    def _get_consulta_config(self, tipo_consulta: str) -> Dict[str, Any]:
        """
        Retorna a configuração de endpoint e campos da API para o tipo de consulta.
        """
        try:
            return self.CONSULTA_CONFIGS[tipo_consulta]
        except KeyError:
            raise ValueError(f"Tipo de consulta inválido: {tipo_consulta}")

    def _get_tipo_consulta_by_node(self, node_name: str) -> str:
        """
        Resolve o tipo de consulta a partir do nome do nó que está chamando a API.
        """
        try:
            return self.NODE_TO_TIPO_CONSULTA[node_name]
        except KeyError:
            raise ValueError(f"Nó de consulta inválido: {node_name}")

    async def consultar_debitos(
        self, tipo_consulta: str, valor: str, **dados: Any
    ) -> Optional[DadosDividaAtiva]:
        """
        Consulta débitos usando endpoint e payload definidos pelo tipo de consulta.

        Exemplos:
            consultar_debitos("cpf_cnpj", "123.456.789-00")
            consultar_debitos("auto_infracao", "12345", ano="2024")
        """
        config = self._get_consulta_config(tipo_consulta)
        endpoint = config["endpoint"]
        consumer = config["consumer"]
        payload = self._preparar_payload(tipo_consulta, valor, dados)

        if not payload.get(config["value_field"]):
            raise ValueError(f"Valor ausente para consulta por {tipo_consulta}")

        for api_field in config.get("extra_fields", {}).values():
            if not payload.get(api_field):
                raise ValueError(
                    f"Campo obrigatório ausente para {tipo_consulta}: {api_field}"
                )

        logger.info(
            f"Iniciando consulta de dívida ativa - Tipo: {tipo_consulta}, Endpoint: {endpoint}"
        )

        return await self._post_consulta_debitos(
            endpoint=endpoint,
            consumer=consumer,
            payload=payload,
        )

    async def consultar_debitos_por_no(
        self, node_name: str, valor: str, **dados: Any
    ) -> Optional[DadosDividaAtiva]:
        """
        Consulta débitos usando o nome do nó do LangGraph como chave de roteamento.
        """
        tipo_consulta = self._get_tipo_consulta_by_node(node_name)
        return await self.consultar_debitos(tipo_consulta, valor, **dados)

    async def emitir_guia_a_vista(
        self, cdas: list[str], efs: list[str]
    ) -> list[Dict[str, Any]]:
        """
        Emite guia de pagamento à vista para CDAs e EFs selecionadas.
        """
        payload = {
            "cdas": cdas,
            "efs": efs,
            self.ORIGEM_SOLICITACAO: 0,
        }
        return await self._post_emitir_guia_a_vista(
            endpoint=self.EMITIR_GUIA_A_VISTA_ENDPOINT,
            consumer=self.EMITIR_GUIA_A_VISTA_CONSUMIDOR,
            payload=payload,
        )

    async def _post_consulta_debitos(
        self, endpoint: str, consumer: str, payload: Dict[str, Any]
    ) -> Optional[DadosDividaAtiva]:
        """
        Executa a consulta usando a tool interna legada de PGM.
        """
        registros = await pgm_api(endpoint=endpoint, consumidor=consumer, data=payload)
        if isinstance(registros, dict) and registros.get("erro"):
            logger.info(
                f"Nenhuma dívida ativa encontrada ou erro de negócio: {registros['motivos']}"
            )
            return None

        response_data = {"success": True, "data": registros}
        parsed = DadosDividaAtiva.from_api_response(response_data)
        self._debug_response_summary(response_data, parsed)
        return parsed

    async def _post_emitir_guia_a_vista(
        self, endpoint: str, consumer: str, payload: Dict[str, Any]
    ) -> list[Dict[str, Any]]:
        registros = await pgm_api(endpoint=endpoint, consumidor=consumer, data=payload)
        if isinstance(registros, dict) and registros.get("erro"):
            logger.error(f"Erro ao emitir guia à vista: {registros['motivos']}")
            raise Exception(registros["motivos"])

        if isinstance(registros, list):
            return registros

        return registros or []
