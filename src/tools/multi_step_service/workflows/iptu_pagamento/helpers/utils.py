"""
Funções utilitárias para o workflow IPTU.

Este módulo contém funções auxiliares para processamento de dados,
formatação e lógica reutilizável do workflow.
"""

from typing import Dict, List, Any, Optional, Protocol
from src.tools.multi_step_service.core.models import ServiceState
from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import DadosCotas


def formatar_valor_brl(valor: Optional[float]) -> str:
    """
    Formata valor numérico para formato brasileiro (R$ 1.234,56).

    Args:
        valor: Valor numérico a ser formatado

    Returns:
        String formatada no padrão brasileiro

    Examples:
        >>> formatar_valor_brl(1234.56)
        'R$ 1.234,56'
        >>> formatar_valor_brl(0)
        'R$ 0,00'
        >>> formatar_valor_brl(None)
        'R$ 0,00'
        >>> formatar_valor_brl(1000000.50)
        'R$ 1.000.000,50'
    """
    if valor is None or valor == 0:
        return "R$ 0,00"

    # Formata com separador de milhares e 2 casas decimais
    valor_str = f"{valor:,.2f}"

    # Substitui vírgula por ponto (milhares) e ponto por vírgula (decimais)
    # Formato americano: 1,234.56 -> Formato brasileiro: 1.234,56
    valor_str = valor_str.replace(",", "X").replace(".", ",").replace("X", ".")

    return f"R$ {valor_str}"


class IPTUAPIProtocol(Protocol):
    """Protocol para tipo do serviço de API IPTU."""

    def parse_brazilian_currency(self, value_str: str) -> float:
        """Converte string de valor brasileiro para float."""
        ...


def preparar_dados_guias_para_template(
    dados_guias: Dict[str, Any], api_service: IPTUAPIProtocol
) -> List[Dict[str, Any]]:
    """
    Prepara dados das guias no formato esperado pelo template.

    Args:
        dados_guias: Dicionário com dados brutos das guias
        api_service: Instância do serviço de API para parsing de moeda

    Returns:
        Lista de dicionários com dados formatados das guias

    Examples:
        >>> from src.tools.multi_step_service.workflows.iptu_pagamento.api_service import IPTUAPIService
        >>> dados = {"guias": [{"numero_guia": "00", "tipo": "IPTU", "valor_iptu_original_guia": "1.234,56", "situacao": {"descricao": "EM ABERTO"}}]}
        >>> api = IPTUAPIService()
        >>> result = preparar_dados_guias_para_template(dados, api)
        >>> result[0]["valor_original"]
        1234.56
    """
    guias_formatadas = []
    guias_disponiveis = dados_guias.get("guias", [])

    for guia in guias_disponiveis:
        valor_original = api_service.parse_brazilian_currency(
            guia.get("valor_iptu_original_guia", "0,00")
        )
        situacao = guia.get("situacao", {}).get("descricao", "EM ABERTO")

        guias_formatadas.append(
            {
                "numero_guia": guia.get("numero_guia", "N/A"),
                "tipo": guia.get("tipo", "IPTU").upper(),
                "valor_original": valor_original,
                "situacao": situacao,
            }
        )

    return guias_formatadas


def preparar_dados_cotas_para_template(dados_cotas: DadosCotas) -> List[Dict[str, Any]]:
    """
    Prepara dados das cotas no formato esperado pelo template.

    Filtra apenas cotas em aberto (não pagas) e extrai campos relevantes
    para exibição ao usuário.

    Args:
        dados_cotas: Objeto DadosCotas com as cotas disponíveis

    Returns:
        Lista de dicionários com dados formatados das cotas em aberto

    Examples:
        >>> from src.tools.multi_step_service.workflows.iptu_pagamento.models import DadosCotas, Cota
        >>> cota = Cota(situacao={"codigo": "02"}, numero_cota="01", valor_cota="100,00",
        ...             data_vencimento="01/01/2025", valor_pago="0,00",
        ...             data_pagamento="", quantidade_dias_atraso="0")
        >>> cota.esta_paga = False
        >>> cota.valor_numerico = 100.0
        >>> dados = DadosCotas(inscricao_imobiliaria="123", exercicio="2025",
        ...                     numero_guia="00", tipo_guia="IPTU", cotas=[cota])
        >>> result = preparar_dados_cotas_para_template(dados)
        >>> len(result)
        1
    """
    cotas_formatadas = []
    cotas_em_aberto = [c for c in dados_cotas.cotas if not c.esta_paga]

    for cota in cotas_em_aberto:
        cotas_formatadas.append(
            {
                "numero_cota": cota.numero_cota,
                "data_vencimento": cota.data_vencimento,
                "valor_cota": cota.valor_cota,
                "esta_vencida": cota.esta_vencida,
                "valor_numerico": cota.valor_numerico or 0.0,
            }
        )

    return cotas_formatadas


def preparar_dados_boletos_para_template(
    guias_geradas: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Prepara dados dos boletos gerados no formato esperado pelo template.

    Args:
        guias_geradas: Lista de guias geradas pelo sistema

    Returns:
        Lista formatada para exibição
    """
    # Já está no formato correto, apenas garante que campos necessários existem
    for guia in guias_geradas:
        if "pdf" not in guia:
            guia["pdf"] = "Não disponível"

    return guias_geradas


def tem_mais_cotas_disponiveis(state: ServiceState) -> bool:
    """
    Verifica se há mais cotas disponíveis da guia atual para pagar.

    Compara o número de cotas já selecionadas com o total de cotas
    disponíveis para a guia atual.

    Args:
        state: Estado do serviço contendo dados das cotas e escolhas

    Returns:
        True se ainda há cotas não selecionadas, False caso contrário

    Examples:
        >>> from src.tools.multi_step_service.core.models import ServiceState
        >>> state = ServiceState()
        >>> state.data["dados_cotas"] = {"cotas": [{"id": 1}, {"id": 2}, {"id": 3}]}
        >>> state.data["cotas_escolhidas"] = ["1", "2"]
        >>> tem_mais_cotas_disponiveis(state)
        True
        >>> state.data["cotas_escolhidas"] = ["1", "2", "3"]
        >>> tem_mais_cotas_disponiveis(state)
        False
    """
    dados_cotas_dict = state.data.get("dados_cotas")
    cotas_escolhidas = state.data.get("cotas_escolhidas", [])

    if not dados_cotas_dict or not cotas_escolhidas:
        return False

    cotas_disponiveis = dados_cotas_dict.get("cotas", [])
    total_cotas = len(cotas_disponiveis)
    cotas_selecionadas = len(cotas_escolhidas)

    return cotas_selecionadas < total_cotas


def tem_outras_guias_disponiveis(state: ServiceState) -> bool:
    """
    Verifica se há outras guias disponíveis no imóvel.

    Args:
        state: Estado do serviço

    Returns:
        True se há outras guias disponíveis, False caso contrário
    """
    dados_guias_dict = state.data.get("dados_guias")

    if not dados_guias_dict:
        return False

    guias_disponiveis = dados_guias_dict.get("guias", [])
    total_guias = len(guias_disponiveis)

    # Se há mais de uma guia disponível, significa que há outras além da atual
    return total_guias > 1


def reset_campos_seletivo(
    state: ServiceState,
    fields: Dict[str, List[str]],
    manter_inscricao: bool = False,
) -> None:
    """
    Faz reset seletivo dos campos especificados.

    Args:
        state: Estado do serviço
        fields: Dict com 'data' e 'internal' contendo listas de campos para resetar
        manter_inscricao: Se True, mantém a inscrição imobiliária atual
    """
    inscricao_atual = (
        state.data.get("inscricao_imobiliaria") if manter_inscricao else None
    )

    # Reset seletivo do data
    if "data" in fields:
        for field in fields["data"]:
            state.data.pop(field, None)

    # Reset seletivo do internal
    if "internal" in fields:
        for field in fields["internal"]:
            state.internal.pop(field, None)

    # Restaura inscrição se necessário e não foi removida no reset
    if inscricao_atual and "inscricao_imobiliaria" not in fields.get("data", []):
        state.data["inscricao_imobiliaria"] = inscricao_atual


def reset_completo(
    state: ServiceState,
    manter_inscricao: bool = False,
) -> None:
    """
    Faz reset completo dos dados e flags internas.

    Args:
        state: Estado do serviço
        manter_inscricao: Se True, mantém a inscrição imobiliária atual
    """
    inscricao_atual = (
        state.data.get("inscricao_imobiliaria") if manter_inscricao else None
    )

    # Reset completo do data
    state.data.clear()

    # Reset completo do internal
    state.internal.clear()

    # Restaura inscrição se necessário
    if inscricao_atual:
        state.data["inscricao_imobiliaria"] = inscricao_atual


def calcular_numero_boletos(darm_separado: bool, num_cotas: int) -> int:
    """
    Calcula o número de boletos que serão gerados.

    Se darm_separado=True, gera um boleto por cota.
    Se darm_separado=False, gera um boleto único com todas as cotas.

    Args:
        darm_separado: Se True, gera um boleto por cota
        num_cotas: Número de cotas selecionadas

    Returns:
        Número de boletos a serem gerados

    Examples:
        >>> calcular_numero_boletos(True, 3)
        3
        >>> calcular_numero_boletos(False, 3)
        1
        >>> calcular_numero_boletos(True, 1)
        1
    """
    if darm_separado:
        return num_cotas
    return 1
