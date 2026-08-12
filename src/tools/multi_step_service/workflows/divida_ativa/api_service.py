from typing import Dict, Any

from loguru import logger

from src.tools.divida_ativa import (
    consultar_debitos as consultar_debitos_tool,
    emitir_guia_a_vista as emitir_guia_a_vista_tool,
)


class DividaAtivaAPIService:
    CONSULTA_CONFIGS = {
        "cpf_cnpj": {
            "tool_field": "cpfCnpj",
        },
        "inscricao_imobiliaria": {
            "tool_field": "inscricaoImobiliaria",
        },
        "auto_infracao": {
            "tool_field": "numeroAutoInfracao",
            "extra_fields": {"ano": "anoAutoInfracao"},
        },
        "cda": {
            "tool_field": "cda",
        },
        "execucao_fiscal": {
            "tool_field": "numeroExecucaoFiscal",
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
        self.user_id = user_id

    def _limpar_valor(self, valor: str) -> str:
        """Remove caracteres não numéricos do valor informado pelo usuário."""
        return "".join(filter(str.isdigit, valor or ""))

    def _preparar_payload(
        self, tipo_consulta: str, valor: str, dados: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """
        Prepara o payload esperado pela tool legada de Dívida Ativa.

        Args:
            tipo_consulta (str): Tipo de consulta escolhido no workflow.
            valor (str): Valor limpo da entrada.
            dados (Optional[Dict[str, Any]]): Campos extras coletados no nó.

        Returns:
            Dict[str, Any]: Payload para a tool consultar_debitos.
        """
        dados = dados or {}
        config = self._get_consulta_config(tipo_consulta)
        tool_field = config["tool_field"]
        payload = {
            "consulta_debitos": tool_field,
            tool_field: self._limpar_valor(valor),
        }

        for input_field, tool_extra_field in config.get("extra_fields", {}).items():
            payload[tool_extra_field] = self._limpar_valor(
                str(dados.get(input_field, ""))
            )

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

    def _deduplicar_identificadores(self, identificadores: list[str]) -> list[str]:
        """Remove duplicados preservando a ordem original."""
        return list(dict.fromkeys(identificadores))

    async def consultar_debitos(self, tipo_consulta: str, valor: str, **dados: Any):
        """
        Consulta débitos usando a tool oficial de Dívida Ativa.

        Exemplos:
            consultar_debitos("cpf_cnpj", "123.456.789-00")
            consultar_debitos("auto_infracao", "12345", ano="2024")
        """
        config = self._get_consulta_config(tipo_consulta)
        payload = self._preparar_payload(tipo_consulta, valor, dados)

        if not payload.get(config["tool_field"]):
            raise ValueError(f"Valor ausente para consulta por {tipo_consulta}")

        for api_field in config.get("extra_fields", {}).values():
            if not payload.get(api_field):
                raise ValueError(
                    f"Campo obrigatório ausente para {tipo_consulta}: {api_field}"
                )

        logger.info(f"Iniciando consulta de dívida ativa via tool: {tipo_consulta}")
        return await consultar_debitos_tool(payload)

    async def consultar_debitos_por_no(
        self, node_name: str, valor: str, **dados: Any
    ) -> Dict[str, Any]:
        """
        Consulta débitos usando o nome do nó do LangGraph como chave de roteamento.
        """
        tipo_consulta = self._get_tipo_consulta_by_node(node_name)
        return await self.consultar_debitos(tipo_consulta, valor, **dados)

    async def emitir_guia_a_vista(
        self, cdas: list[str], efs: list[str]
    ) -> Dict[str, Any]:
        """
        Emite guia de pagamento à vista usando a tool oficial de Dívida Ativa.
        """
        cdas = self._deduplicar_identificadores(cdas)
        efs = self._deduplicar_identificadores(efs)
        dicionario_itens = {}
        itens_informados = []
        for indice, identificador in enumerate([*cdas, *efs], start=1):
            dicionario_itens[str(indice)] = identificador
            itens_informados.append(str(indice))

        payload = {
            "itens_informados": itens_informados,
            "dicionario_itens": repr(dicionario_itens),
            "lista_cdas": repr(cdas),
            "lista_efs": repr(efs),
            "lista_guias": "[]",
        }
        return await emitir_guia_a_vista_tool(payload)
