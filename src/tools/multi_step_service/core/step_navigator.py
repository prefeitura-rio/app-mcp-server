"""
Gerenciador de navegação não-linear entre steps do workflow.

Este módulo permite que workflows suportem navegação não-linear,
onde usuários podem "voltar" para steps anteriores enviando payload
de campos de steps anteriores.
"""

from typing import Dict, List, Optional
from src.tools.multi_step_service.core.models import ServiceState
from loguru import logger


class StepNavigator:
    """
    Gerencia navegação não-linear entre steps do workflow.

    Responsabilidades:
    - Detectar step atual baseado em state.data
    - Identificar quando payload contém campos de steps anteriores
    - Executar reset em cascata de dependências

    Attributes:
        step_order: Lista com ordem dos campos principais do workflow
        step_dependencies: Dicionário mapeando campo → lista de campos dependentes
    """

    def __init__(self, step_order: List[str], step_dependencies: Dict[str, List[str]]):
        """
        Inicializa o navegador de steps.

        Args:
            step_order: Lista com ordem dos campos principais (ex: ['inscricao', 'ano', 'guia'])
            step_dependencies: Mapa de campo → campos que devem ser resetados quando ele muda
        """
        self.step_order = step_order
        self.step_dependencies = step_dependencies

    def get_current_step_index(self, state: ServiceState) -> int:
        """
        Detecta índice do step atual baseado em quais campos existem em state.data.

        Retorna o índice do último step que tem dados completos.

        Args:
            state: Estado do serviço

        Returns:
            Índice do step atual (0-based), ou -1 se nenhum step foi iniciado

        Examples:
            >>> navigator = StepNavigator(['inscricao', 'ano', 'guia'], {})
            >>> state = ServiceState()
            >>> state.data['inscricao'] = '12345'
            >>> state.data['ano'] = 2024
            >>> navigator.get_current_step_index(state)
            1  # Último step com dados é 'ano' (índice 1)
        """
        for i in range(len(self.step_order) - 1, -1, -1):
            step_field = self.step_order[i]
            if step_field in state.data:
                return i
        return -1  # Nenhum step iniciado

    def detect_previous_step_in_payload(
        self, state: ServiceState, current_step_index: int
    ) -> Optional[str]:
        """
        Detecta se payload contém algum campo de step anterior ao atual,
        OU se está alterando o valor de um step já preenchido.

        Args:
            state: Estado do serviço
            current_step_index: Índice do step atual

        Returns:
            Nome do campo de step anterior/alterado encontrado, ou None se não houver

        Examples:
            >>> navigator = StepNavigator(['inscricao', 'ano', 'guia'], {})
            >>> state = ServiceState()
            >>> state.payload = {'ano': 2024}  # Campo de step anterior
            >>> navigator.detect_previous_step_in_payload(state, 2)  # Atual é step 2 (guia)
            'ano'  # Encontrou campo de step 1
        """
        for field in state.payload.keys():
            if field in self.step_order:
                field_index = self.step_order.index(field)
                # Detecta se é step anterior OU se está alterando valor existente
                if field_index <= current_step_index:
                    return field
        return None

    def reset_cascade(
        self,
        state: ServiceState,
        from_step_field: str,
        keep_fields: Optional[List[str]] = None,
    ) -> ServiceState:
        """
        Reseta campos dependentes de um step específico.

        Remove todos os campos listados em step_dependencies[from_step_field],
        exceto os listados em keep_fields.

        Args:
            state: Estado do serviço
            from_step_field: Campo do step que foi alterado
            keep_fields: Campos a manter (exceções ao reset)

        Returns:
            Estado modificado com campos resetados

        Examples:
            >>> navigator = StepNavigator(
            ...     ['inscricao', 'ano'],
            ...     {'ano': ['dados_guias', 'guia_escolhida']}
            ... )
            >>> state = ServiceState()
            >>> state.data = {'inscricao': '123', 'ano': 2023, 'dados_guias': {...}, 'guia_escolhida': '01'}
            >>> navigator.reset_cascade(state, 'ano')
            # Remove 'dados_guias' e 'guia_escolhida', mantém 'inscricao' e 'ano'
        """
        keep_fields = keep_fields or []

        # Busca campos a resetar
        fields_to_reset = self.step_dependencies.get(from_step_field, [])

        # Filtra exceções
        fields_to_reset = [f for f in fields_to_reset if f not in keep_fields]

        # Remove campos de state.data
        for field in fields_to_reset:
            if field in state.data:
                state.data.pop(field)
                logger.debug(
                    f"🔄 Reset: removido '{field}' devido a mudança em '{from_step_field}'"
                )

        # Remove flags internas relacionadas aos campos resetados
        # Por exemplo, se resetou 'dados_guias', remove 'has_consulted_guias'
        internal_keys_to_remove = [
            k for k in state.internal.keys() if any(f in k for f in fields_to_reset)
        ]
        for key in internal_keys_to_remove:
            state.internal.pop(key)
            logger.debug(
                f"🔄 Reset: removido flag interna '{key}' devido a mudança em '{from_step_field}'"
            )

        return state

    def auto_reset(self, state: ServiceState) -> ServiceState:
        """
        Executa reset automático se payload contém campo de step anterior.

        Este é o método principal chamado por BaseWorkflow.execute().

        Fluxo:
        1. Detecta step atual baseado em state.data
        2. Verifica se payload tem campo de step anterior
        3. Se sim, executa reset em cascata dos campos dependentes

        Args:
            state: Estado do serviço

        Returns:
            Estado modificado (ou inalterado se não precisa reset)

        Examples:
            >>> # Usuário está em step 3 (escolha_cotas) mas envia payload de step 1 (ano)
            >>> navigator = StepNavigator(
            ...     ['inscricao', 'ano', 'guia', 'cotas'],
            ...     {'ano': ['dados_guias', 'guia', 'dados_cotas', 'cotas']}
            ... )
            >>> state = ServiceState()
            >>> state.data = {'inscricao': '123', 'ano': 2023, 'guia': '01', 'cotas': ['1', '2']}
            >>> state.payload = {'ano': 2024}  # Usuário quer mudar o ano
            >>> navigator.auto_reset(state)
            # Remove 'dados_guias', 'guia', 'dados_cotas', 'cotas'
            # Mantém 'inscricao' e 'ano' será atualizado pelo nó
        """
        # 1. Detecta step atual
        current_step_index = self.get_current_step_index(state)

        if current_step_index == -1:
            # Workflow no início, não precisa reset
            return state

        # 2. Verifica se payload tem campo de step anterior
        previous_step_field = self.detect_previous_step_in_payload(
            state, current_step_index
        )

        if previous_step_field:
            logger.info(
                f"🔄 Navegação não-linear detectada: campo '{previous_step_field}' "
                f"de step anterior no payload (step atual: índice {current_step_index})"
            )
            # 3. Executa reset em cascata
            state = self.reset_cascade(state, previous_step_field)

        return state
