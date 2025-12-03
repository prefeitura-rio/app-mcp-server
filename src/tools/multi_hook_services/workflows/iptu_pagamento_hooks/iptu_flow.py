"""
Workflow IPTU usando framework hooks-based.

Esta implementação demonstra como o framework hooks reduz drasticamente
a complexidade do código, de 992 linhas (versão LangGraph) para ~120 linhas.
"""

from typing import List, Dict, Any
from loguru import logger

from src.tools.multi_step_service.core.models import AgentResponse
from src.tools.multi_hook_services.core.base_flow import BaseFlow
from src.tools.multi_hook_services.core.flow_exceptions import FlowError

# Reutiliza modelos Pydantic do workflow existente
from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import (
    InscricaoImobiliariaPayload,
    EscolhaAnoPayload,
    EscolhaFormatoDarmPayload,
)

# Reutiliza API service do workflow existente
from src.tools.multi_step_service.workflows.iptu_pagamento.api.api_service import IPTUAPIService
from src.tools.multi_step_service.workflows.iptu_pagamento.api.exceptions import (
    DataNotFoundError,
    APIUnavailableError,
)

# Nota: Para o POC, usamos mensagens inline simples ao invés dos templates complexos
# que esperam formato dict específico. Em produção, poderia converter Pydantic → dict
# ou criar versão dos templates que aceita objetos Pydantic.


class IPTUFlow(BaseFlow):
    """
    Workflow para consulta e emissão de guias IPTU usando hooks.

    Fluxo:
    1. Coleta inscrição imobiliária
    2. Busca dados do imóvel (API)
    3. Coleta ano de exercício
    4. Consulta guias disponíveis (com tratamento de dívida ativa)
    5. Escolhe guia
    6. Consulta cotas da guia (API)
    7. Escolhe cotas a pagar
    8. Escolhe formato DARM (se múltiplas cotas)
    9. Confirmação dos dados
    10. Gera DARMs
    11. Retorna sucesso com guias geradas
    """

    service_name = "iptu_pagamento"
    description = "Consulta e emissão de guias de pagamento do IPTU"

    def __init__(self, state):
        super().__init__(state)
        self.api = IPTUAPIService()

    async def run(self) -> AgentResponse:
        """Executa workflow de forma procedural usando hooks."""

        # 1. Coleta inscrição imobiliária
        inscricao = await self.use_input(
            "inscricao_imobiliaria",
            InscricaoImobiliariaPayload,
            "📋 Para consultar o IPTU, informe a **inscrição imobiliária** do seu imóvel."
        )

        # 2. Busca dados do imóvel (endereco, proprietario)
        imovel_info = await self.use_api(self.api.get_imovel_info, inscricao)

        endereco = imovel_info.get("endereco", "N/A") if imovel_info else "N/A"
        proprietario = imovel_info.get("proprietario", "N/A") if imovel_info else "N/A"

        self.state.data["endereco"] = endereco
        self.state.data["proprietario"] = proprietario

        # 3. Coleta ano de exercício
        ano = await self.use_input(
            "ano_exercicio",
            EscolhaAnoPayload,
            f"🏠 **Dados do Imóvel:**\n"
            f"🆔 Inscrição: {inscricao}\n"
            f"📍 Endereço: {endereco}\n"
            f"💼 Proprietário: {proprietario}\n\n"
            f"📅 Informe o **ano de exercício** para consulta do IPTU:"
        )

        # 4. Consulta guias disponíveis (com tratamento de dívida ativa)
        guias_data = await self._consultar_guias_com_tratamento_divida(inscricao, ano)

        if not guias_data or not guias_data.guias:
            raise FlowError(
                "Nenhuma guia encontrada para esta inscrição e ano",
                f"inscricao={inscricao}, ano={ano}"
            )

        # Salva dados das guias
        self.state.data["dados_guias"] = guias_data

        # 5. Escolhe guia
        guia_opcoes = [g.numero_guia for g in guias_data.guias]

        # Monta mensagem com guias disponíveis
        guias_texto = "\n".join([
            f"💳 **Guia {g.numero_guia}** - {g.tipo}\n"
            f"• Valor: R$ {g.valor_numerico:.2f}\n"
            f"• Situação: {g.situacao.get('descricao', 'EM ABERTO')}"
            for g in guias_data.guias
        ])

        guia_escolhida = await self.use_choice(
            "guia_escolhida",
            f"🏠 **Dados do Imóvel:**\n"
            f"🆔 Inscrição: {inscricao}\n"
            f"📍 Endereço: {endereco}\n"
            f"💼 Proprietário: {proprietario}\n\n"
            f"📋 **Guias Disponíveis para IPTU {ano}:**\n\n"
            f"{guias_texto}\n\n"
            f"🎯 Selecione o número da guia desejada:",
            options=guia_opcoes
        )

        # Pega tipo da guia selecionada (para API de cotas)
        guia_obj = next((g for g in guias_data.guias if g.numero_guia == guia_escolhida), None)
        tipo_guia = guia_obj.tipo if guia_obj else "ORDINÁRIA"

        # 6. Consulta cotas da guia
        cotas_data = await self.use_api(
            self.api.obter_cotas,
            inscricao, ano, guia_escolhida, tipo_guia
        )

        if not cotas_data or not cotas_data.cotas:
            raise FlowError(
                "Nenhuma cota encontrada para esta guia",
                f"guia={guia_escolhida}"
            )

        # Salva dados das cotas
        self.state.data["dados_cotas"] = cotas_data

        # Filtra apenas cotas não pagas
        cotas_nao_pagas = [c for c in cotas_data.cotas if not c.esta_paga]

        if not cotas_nao_pagas:
            raise FlowError(
                "Todas as cotas desta guia já estão pagas",
                f"guia={guia_escolhida}"
            )

        # 7. Escolhe cotas a pagar
        cotas_opcoes = [c.numero_cota for c in cotas_nao_pagas]

        # Monta mensagem com cotas disponíveis
        cotas_texto = "\n".join([
            f"📅 **Cota {c.numero_cota}**\n"
            f"• Valor: R$ {c.valor_numerico:.2f}\n"
            f"• Vencimento: {c.data_vencimento}\n"
            f"• Situação: {c.situacao.get('descricao', 'EM ABERTO')}"
            for c in cotas_nao_pagas
        ])

        cotas_escolhidas = await self.use_multi_choice(
            "cotas_escolhidas",
            f"💳 **Guia {guia_escolhida} - {tipo_guia}**\n\n"
            f"📋 **Cotas Disponíveis:**\n\n"
            f"{cotas_texto}\n\n"
            f"🎯 Selecione as cotas que deseja pagar (pode escolher uma ou várias):",
            options=cotas_opcoes
        )

        # 8. Escolhe formato DARM (se múltiplas cotas)
        darm_separado = False
        if len(cotas_escolhidas) > 1:
            darm_separado_input = await self.use_input(
                "darm_separado",
                EscolhaFormatoDarmPayload,
                f"📄 Você selecionou {len(cotas_escolhidas)} cotas.\n\n"
                f"Deseja gerar:\n"
                f"• `False` - Um boleto único com todas as cotas\n"
                f"• `True` - Um boleto separado para cada cota\n\n"
                f"Informe sua escolha:"
            )
            darm_separado = darm_separado_input

        # 9. Confirmação dos dados
        confirmado = await self.confirm(
            f"✅ **Confirmação dos Dados:**\n\n"
            f"🏠 Inscrição: {inscricao}\n"
            f"📅 Ano: {ano}\n"
            f"💳 Guia: {guia_escolhida}\n"
            f"📋 Cotas: {', '.join(cotas_escolhidas)}\n"
            f"📄 Formato: {'Boletos separados' if darm_separado else 'Boleto único'}\n\n"
            f"Os dados estão corretos?",
            data={
                "inscricao": inscricao,
                "ano": ano,
                "guia": guia_escolhida,
                "cotas": cotas_escolhidas,
                "darm_separado": darm_separado
            }
        )

        if not confirmado:
            return self.cancel("Operação cancelada pelo usuário")

        # 10. Gera DARMs
        darms_gerados = await self._gerar_darms(
            inscricao, ano, guia_escolhida, cotas_escolhidas, darm_separado
        )

        # 11. Retorna sucesso
        darms_info = "\n\n".join([
            f"💳 **DARM {i+1}:**\n"
            f"• Cotas: {d['cotas']}\n"
            f"• Valor: R$ {d['valor']:.2f}\n"
            f"• Vencimento: {d['vencimento']}\n"
            f"• Código de barras: {d['codigo_barras']}\n"
            f"• PDF: {d['pdf']}"
            for i, d in enumerate(darms_gerados)
        ])

        return self.success(
            f"✅ **Boletos Gerados com Sucesso!**\n\n"
            f"📋 Inscrição: {inscricao}\n\n"
            f"{darms_info}\n\n"
            f"💡 Use os códigos de barras para pagamento ou faça download dos PDFs.",
            data={"guias_geradas": darms_gerados}
        )

    async def _consultar_guias_com_tratamento_divida(self, inscricao: str, ano: int):
        """
        Consulta guias com tratamento especial para dívida ativa.

        Se não encontrar guias para o ano solicitado, verifica se há dívida ativa
        e mostra informações ao usuário.
        """
        try:
            guias_data = await self.use_api(self.api.consultar_guias, inscricao, ano)
            return guias_data

        except (DataNotFoundError, Exception):
            # Não encontrou guias - verifica dívida ativa
            logger.info(f"Nenhuma guia encontrada para {inscricao}/{ano}, verificando dívida ativa")

            try:
                divida_data = await self.use_api(self.api.get_divida_ativa_info, inscricao)

                if divida_data and divida_data.tem_divida_ativa:
                    # Tem dívida ativa - informa ao usuário e solicita novo ano
                    from src.tools.multi_hook_services.core.flow_exceptions import FlowPause
                    raise FlowPause(AgentResponse(
                        service_name=self.service_name,
                        description=f"⚠️ **Dívida Ativa Encontrada**\n\n"
                                    f"🏠 Inscrição: {inscricao}\n"
                                    f"📍 Endereço: {self.state.data.get('endereco', 'N/A')}\n"
                                    f"💰 Saldo total: {divida_data.saldo_total_divida}\n\n"
                                    f"Não há guias disponíveis para o ano solicitado, mas há dívida ativa.\n\n"
                                    f"Tente outro ano de exercício:",
                        payload_schema=EscolhaAnoPayload.model_json_schema()
                    ))
            except FlowPause:
                # Re-lança FlowPause para não ser capturado
                raise
            except Exception as e:
                logger.warning(f"Erro ao consultar dívida ativa: {e}")

            # Não tem dívida ativa ou erro ao consultar - retorna None
            return None

    async def _gerar_darms(
        self,
        inscricao: str,
        ano: int,
        guia: str,
        cotas: List[str],
        separado: bool
    ) -> List[Dict[str, Any]]:
        """
        Gera DARMs para as cotas selecionadas.

        Args:
            inscricao: Inscrição imobiliária
            ano: Ano do exercício
            guia: Número da guia
            cotas: Lista de cotas selecionadas
            separado: True para gerar DARM separado por cota, False para único

        Returns:
            Lista de DARMs gerados com dados completos
        """
        # Define grupos de cotas (separado ou único)
        grupos = [[c] for c in cotas] if separado else [cotas]

        darms_gerados = []

        for grupo_cotas in grupos:
            try:
                # Consulta DARM para este grupo de cotas
                darm_data = await self.use_api(
                    self.api.consultar_darm,
                    inscricao, ano, guia, grupo_cotas,
                    cache=False  # Não cacheia pois pode ter múltiplas combinações
                )

                if not darm_data or not darm_data.darm:
                    logger.warning(f"DARM não gerado para cotas {grupo_cotas}")
                    continue

                # Download PDF do DARM
                pdf_url = await self.use_api(
                    self.api.download_pdf_darm,
                    inscricao, ano, guia, grupo_cotas,
                    cache=False
                )

                # Monta dados do DARM gerado
                darm_info = {
                    "tipo": "darm",
                    "numero_guia": guia,
                    "cotas": ", ".join(grupo_cotas),
                    "valor": darm_data.darm.valor_numerico,
                    "vencimento": darm_data.darm.data_vencimento,
                    "codigo_barras": darm_data.darm.codigo_barras,
                    "linha_digitavel": darm_data.darm.sequencia_numerica,
                    "pdf": pdf_url
                }

                darms_gerados.append(darm_info)
                logger.info(f"DARM gerado com sucesso para cotas {grupo_cotas}")

            except Exception as e:
                logger.error(f"Erro ao gerar DARM para cotas {grupo_cotas}: {e}")
                # Continua para tentar próximo grupo
                continue

        if not darms_gerados:
            raise FlowError(
                "Não foi possível gerar nenhum DARM",
                "Todas as tentativas de geração falharam"
            )

        return darms_gerados
