"""
Classe base para workflows usando hooks (React-style).

Este módulo implementa o framework hooks-based que simplifica drasticamente
a criação de workflows multi-step, reduzindo código em ~10x.
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Type
from pydantic import BaseModel, ValidationError
from loguru import logger

from src.tools.multi_step_service.core.models import ServiceState, AgentResponse
from src.tools.multi_hook_services.core.flow_exceptions import FlowPause, FlowCancelled


class BaseFlow(ABC):
    """
    Classe base para workflows usando hooks (inspirado em React Hooks).

    Esta classe permite escrever workflows de forma procedural (linear),
    eliminando a complexidade de grafos e roteadores condicionais.

    Exemplo de uso:
        class MeuFlow(BaseFlow):
            service_name = "meu_servico"
            description = "Descrição do serviço"

            async def run(self) -> AgentResponse:
                # Código procedural simples
                nome = await self.use_input("nome", NomePayload, "Informe seu nome:")
                idade = await self.use_input("idade", IdadePayload, "Informe sua idade:")
                confirmado = await self.confirm("Dados corretos?", {"nome": nome, "idade": idade})

                if not confirmado:
                    return self.cancel("Operação cancelada")

                # Processa dados
                resultado = await self.use_api(self.api.processar, nome, idade)

                return self.success("Sucesso!", {"resultado": resultado})
    """

    service_name: str = ""
    description: str = ""

    def __init__(self, state: ServiceState):
        """
        Inicializa o flow com o estado do usuário.

        Args:
            state: Estado compartilhado do serviço (persistido entre requisições)
        """
        self.state = state
        self._step_stack: List[str] = []  # Rastreia steps para navegação não-linear

    @abstractmethod
    async def run(self) -> AgentResponse:
        """
        Lógica procedural do workflow.

        Este método deve ser implementado por cada workflow filho.
        Use os hooks disponíveis (use_input, use_api, use_choice, etc) para
        construir o fluxo de forma linear e intuitiva.

        Returns:
            AgentResponse com o resultado final do workflow
        """
        pass

    # ============================================================================
    # HOOKS - Métodos auxiliares para construir workflows
    # ============================================================================

    async def use_input(
        self,
        field: str,
        schema: Type[BaseModel],
        message: str,
        on_error_message: Optional[str] = None
    ) -> Any:
        """
        Hook para coletar e validar input do usuário.

        Este hook:
        1. Verifica se o campo já está em state.data (retorna o valor)
        2. Verifica se o campo veio no payload atual (valida e salva)
        3. Se não tem: levanta FlowPause para solicitar ao usuário

        Args:
            field: Nome do campo a coletar
            schema: Pydantic model para validação
            message: Mensagem a exibir ao usuário
            on_error_message: Mensagem customizada para erro de validação

        Returns:
            Valor validado do campo

        Raises:
            FlowPause: Quando precisa pausar para coletar input

        Example:
            inscricao = await self.use_input(
                "inscricao_imobiliaria",
                InscricaoPayload,
                "Informe a inscrição imobiliária:"
            )
        """
        # Registra step para navegação não-linear
        if field not in self._step_stack:
            self._step_stack.append(field)

        # 1. Veio no payload? Valida e salva (prioridade para novo valor)
        if field in self.state.payload:
            try:
                validated = schema.model_validate(self.state.payload)
                value = getattr(validated, field)
                self.state.data[field] = value
                logger.info(f"Hook use_input('{field}'): validado e salvo = {value}")
                return value
            except ValidationError as e:
                logger.warning(f"Hook use_input('{field}'): erro de validação = {e}")
                # Erro de validação - solicita novamente com mensagem de erro
                raise FlowPause(AgentResponse(
                    service_name=self.service_name,
                    description=on_error_message or message,
                    payload_schema=schema.model_json_schema(),
                    error_message=str(e)
                ))

        # 2. Já tem o dado em cache? Retorna
        if field in self.state.data:
            logger.debug(f"Hook use_input('{field}'): valor já existe em state.data")
            return self.state.data[field]

        # 3. Não tem - solicita
        logger.debug(f"Hook use_input('{field}'): solicitando ao usuário")
        raise FlowPause(AgentResponse(
            service_name=self.service_name,
            description=message,
            payload_schema=schema.model_json_schema()
        ))

    async def use_api(
        self,
        api_func: Callable,
        *args,
        cache: bool = True,
        **kwargs
    ) -> Any:
        """
        Hook para chamar APIs externas com cache automático.

        Este hook automaticamente cacheia resultados de API em state.internal,
        evitando chamadas redundantes durante navegação não-linear.

        Args:
            api_func: Função assíncrona da API a chamar
            *args: Argumentos posicionais
            cache: Se True, cacheia resultado em state.internal
            **kwargs: Argumentos nomeados

        Returns:
            Resultado da chamada à API

        Example:
            imovel = await self.use_api(self.api.get_imovel_info, inscricao)
        """
        if cache:
            # Gera chave de cache baseada em função + argumentos
            cache_key = f"api_cache_{api_func.__name__}_{hash(str(args) + str(kwargs))}"

            # Verifica cache
            if cache_key in self.state.internal:
                logger.debug(f"Hook use_api({api_func.__name__}): usando cache")
                return self.state.internal[cache_key]

        # Chama API
        logger.info(f"Hook use_api({api_func.__name__}): chamando API")
        result = await api_func(*args, **kwargs)

        if cache:
            # Salva em cache
            self.state.internal[cache_key] = result
            logger.debug(f"Hook use_api({api_func.__name__}): resultado cacheado")

        return result

    async def use_choice(
        self,
        field: str,
        message: str,
        options: List[str]
    ) -> str:
        """
        Hook para escolha única entre opções.

        Args:
            field: Nome do campo
            message: Mensagem com contexto das opções
            options: Lista de opções válidas

        Returns:
            Opção escolhida

        Raises:
            FlowPause: Quando precisa pausar para coletar escolha

        Example:
            guia = await self.use_choice(
                "guia_escolhida",
                "Selecione a guia:",
                options=["00", "01", "02"]
            )
        """
        # Registra step
        if field not in self._step_stack:
            self._step_stack.append(field)

        # Já tem?
        if field in self.state.data:
            logger.debug(f"Hook use_choice('{field}'): valor já existe")
            return self.state.data[field]

        # Veio no payload?
        if field in self.state.payload:
            choice = self.state.payload[field]
            if choice in options:
                self.state.data[field] = choice
                logger.info(f"Hook use_choice('{field}'): escolhido = {choice}")
                return choice
            else:
                logger.warning(f"Hook use_choice('{field}'): opção inválida = {choice}")
                raise FlowPause(AgentResponse(
                    service_name=self.service_name,
                    description=message,
                    payload_schema=self._build_choice_schema(field, options),
                    error_message=f"Opção inválida. Escolha uma das opções: {', '.join(options)}"
                ))

        # Solicita
        logger.debug(f"Hook use_choice('{field}'): solicitando escolha")
        raise FlowPause(AgentResponse(
            service_name=self.service_name,
            description=message,
            payload_schema=self._build_choice_schema(field, options)
        ))

    async def use_multi_choice(
        self,
        field: str,
        message: str,
        options: List[str]
    ) -> List[str]:
        """
        Hook para múltipla escolha entre opções.

        Args:
            field: Nome do campo
            message: Mensagem com contexto das opções
            options: Lista de opções válidas

        Returns:
            Lista de opções escolhidas

        Raises:
            FlowPause: Quando precisa pausar para coletar escolhas

        Example:
            cotas = await self.use_multi_choice(
                "cotas_escolhidas",
                "Selecione as cotas:",
                options=["1", "2", "3", "4"]
            )
        """
        # Registra step
        if field not in self._step_stack:
            self._step_stack.append(field)

        # Já tem?
        if field in self.state.data:
            logger.debug(f"Hook use_multi_choice('{field}'): valores já existem")
            return self.state.data[field]

        # Veio no payload?
        if field in self.state.payload:
            choices = self.state.payload[field]

            # Garante que é lista
            if not isinstance(choices, list):
                choices = [choices]

            # Valida cada escolha
            invalid_choices = [c for c in choices if c not in options]
            if invalid_choices:
                logger.warning(f"Hook use_multi_choice('{field}'): opções inválidas = {invalid_choices}")
                raise FlowPause(AgentResponse(
                    service_name=self.service_name,
                    description=message,
                    payload_schema=self._build_multi_choice_schema(field, options),
                    error_message=f"Opções inválidas: {', '.join(invalid_choices)}"
                ))

            self.state.data[field] = choices
            logger.info(f"Hook use_multi_choice('{field}'): escolhidos = {choices}")
            return choices

        # Solicita
        logger.debug(f"Hook use_multi_choice('{field}'): solicitando escolhas")
        raise FlowPause(AgentResponse(
            service_name=self.service_name,
            description=message,
            payload_schema=self._build_multi_choice_schema(field, options)
        ))

    async def confirm(
        self,
        message: str,
        data: Dict[str, Any]
    ) -> bool:
        """
        Hook para confirmação de dados com resumo.

        Se usuário não confirmar, reseta o estado e retorna False.

        Args:
            message: Mensagem de confirmação com resumo dos dados
            data: Dados a confirmar (apenas para referência, não altera state)

        Returns:
            True se confirmado, False se cancelado

        Raises:
            FlowPause: Quando precisa pausar para coletar confirmação

        Example:
            confirmado = await self.confirm(
                "Dados corretos?",
                {"nome": nome, "idade": idade}
            )
            if not confirmado:
                return self.cancel("Operação cancelada")
        """
        if "confirmacao" in self.state.payload:
            confirmed = self.state.payload["confirmacao"]

            if not confirmed:
                # Usuário cancelou - reseta para início
                logger.info("Hook confirm(): usuário cancelou, resetando estado")
                self.state.data.clear()
                self.state.internal.clear()
                self._step_stack.clear()
                return False

            logger.info("Hook confirm(): confirmado pelo usuário")
            return True

        # Solicita confirmação
        logger.debug("Hook confirm(): solicitando confirmação")
        raise FlowPause(AgentResponse(
            service_name=self.service_name,
            description=message,
            payload_schema={
                "type": "object",
                "properties": {
                    "confirmacao": {
                        "type": "boolean",
                        "description": "Confirmação dos dados apresentados"
                    }
                },
                "required": ["confirmacao"]
            }
        ))

    # ============================================================================
    # FINALIZADORES - Métodos para retornar resultado final do workflow
    # ============================================================================

    def success(
        self,
        message: str,
        data: Optional[Dict[str, Any]] = None
    ) -> AgentResponse:
        """
        Finaliza workflow com sucesso.

        Args:
            message: Mensagem de sucesso para o usuário
            data: Dados finais do workflow

        Returns:
            AgentResponse com status de sucesso

        Example:
            return self.success(
                "Boletos gerados com sucesso!",
                {"guias_geradas": darms}
            )
        """
        logger.info(f"Workflow {self.service_name}: finalizado com sucesso")
        return AgentResponse(
            service_name=self.service_name,
            description=message,
            payload_schema=None,  # Sem schema = workflow finalizado
            data=data or self.state.data
        )

    def error(
        self,
        message: str,
        error_detail: str = ""
    ) -> AgentResponse:
        """
        Finaliza workflow com erro.

        Args:
            message: Mensagem de erro para o usuário
            error_detail: Detalhes técnicos do erro

        Returns:
            AgentResponse com status de erro

        Example:
            return self.error(
                "Não foi possível processar sua solicitação",
                str(e)
            )
        """
        logger.error(f"Workflow {self.service_name}: erro = {error_detail}")
        return AgentResponse(
            service_name=self.service_name,
            description=message,
            payload_schema=None,
            error_message=error_detail,
            data=self.state.data
        )

    def cancel(
        self,
        message: str = "Operação cancelada"
    ) -> AgentResponse:
        """
        Finaliza workflow por cancelamento do usuário.

        Args:
            message: Mensagem de cancelamento

        Returns:
            AgentResponse indicando cancelamento

        Example:
            if not confirmado:
                return self.cancel("Operação cancelada pelo usuário")
        """
        logger.info(f"Workflow {self.service_name}: cancelado pelo usuário")
        return AgentResponse(
            service_name=self.service_name,
            description=message,
            payload_schema=None,
            data=self.state.data
        )

    # ============================================================================
    # MÉTODOS AUXILIARES PRIVADOS
    # ============================================================================

    def _build_choice_schema(self, field: str, options: List[str]) -> Dict:
        """Constrói schema JSON para escolha única."""
        return {
            "type": "object",
            "properties": {
                field: {
                    "type": "string",
                    "enum": options,
                    "description": f"Escolha uma opção entre: {', '.join(options)}"
                }
            },
            "required": [field]
        }

    def _build_multi_choice_schema(self, field: str, options: List[str]) -> Dict:
        """Constrói schema JSON para múltipla escolha."""
        return {
            "type": "object",
            "properties": {
                field: {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": options
                    },
                    "description": f"Escolha uma ou mais opções entre: {', '.join(options)}"
                }
            },
            "required": [field]
        }
