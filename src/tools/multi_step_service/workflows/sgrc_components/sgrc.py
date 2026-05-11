import time
from datetime import datetime
from typing import Any, Dict, Union

from loguru import logger
from prefeitura_rio.integrations.sgrc import async_new_ticket
from prefeitura_rio.integrations.sgrc.exceptions import (
    SGRCBusinessRuleException,
    SGRCDuplicateTicketException,
    SGRCEquivalentTicketException,
    SGRCInternalErrorException,
    SGRCInvalidBodyException,
    SGRCMalformedBodyException,
)
from prefeitura_rio.integrations.sgrc.models import Address, Requester

from src.tools.multi_step_service.core.base_workflow import handle_errors
from src.tools.multi_step_service.core.models import ServiceState
from src.tools.multi_step_service.workflows.sgrc_components.ticket_state import (
    ticket_failed,
    ticket_opened,
)


class SGRCTicketMixin:
    async def new_ticket(
        self,
        classification_code: str,
        description: str = "",
        address: Address = None,
        date_time: Union[datetime, str] = None,
        requester: Requester = None,
        occurrence_origin_code: str = "28",
        specific_attributes: Dict[str, Any] = None,
    ):
        """Cria um novo ticket no SGRC."""
        start_time = time.time()
        end_time = None

        try:
            ticket = await async_new_ticket(
                classification_code=classification_code,
                description=description,
                address=address,
                date_time=date_time,
                requester=requester,
                occurrence_origin_code=occurrence_origin_code,
                specific_attributes=specific_attributes or {},
            )

            end_time = time.time()
            logger.info(
                f"Ticket criado com sucesso. Protocol ID: {ticket.protocol_id}. Tempo: {end_time - start_time:.2f}s"
            )
            return ticket

        except Exception as exc:
            end_time = end_time if end_time else time.time()
            logger.error(
                f"Erro ao criar ticket. Tempo: {end_time - start_time:.2f}s. Erro: {exc}"
            )
            raise exc

    @handle_errors
    async def _open_ticket(self, state: ServiceState) -> ServiceState:
        """Abre um ticket no SGRC com os dados coletados."""
        logger.info("[ENTRADA] _open_ticket")

        if self.use_fake_api:
            protocol = f"FAKE-{int(time.time())}"
            logger.info(f"Ticket fake criado: {protocol}")

            return ticket_opened(
                state,
                protocol,
                self.templates.solicitacao_criada_sucesso(protocol),
            )

        try:
            address, requester, description = self.build_ticket_payload(state)
            specific_attributes = self.build_specific_attributes(state)

            ticket = await self.new_ticket(
                classification_code=self.service_id,
                description=description,
                address=address,
                requester=requester,
                occurrence_origin_code=self.common_config.occurrence_origin_code,
                specific_attributes=specific_attributes,
            )

            return ticket_opened(
                state,
                ticket.protocol_id,
                self.templates.solicitacao_criada_sucesso(ticket.protocol_id),
            )

        except (
            SGRCBusinessRuleException,
            SGRCInvalidBodyException,
            SGRCMalformedBodyException,
            ValueError,
        ) as exc:
            logger.exception(exc)
            return ticket_failed(
                state,
                error_code="erro_interno",
                description=self.templates.erro_criar_solicitacao(),
                error_message=str(exc),
            )

        except (SGRCDuplicateTicketException, SGRCEquivalentTicketException) as exc:
            logger.exception(exc)
            return ticket_failed(
                state,
                error_code="erro_ticket_duplicado",
                description=self.templates.solicitacao_existente(
                    getattr(exc, "protocol_id", "seu protocolo")
                ),
                error_message=str(exc),
            )

        except SGRCInternalErrorException as exc:
            logger.exception(exc)
            return ticket_failed(
                state,
                error_code="erro_sgrc",
                description=self.templates.sistema_indisponivel(),
                error_message="Sistema indisponível",
            )

        except Exception as exc:
            logger.exception(exc)
            return ticket_failed(
                state,
                error_code="erro_geral",
                description=self.templates.erro_geral_chamado(),
                error_message=str(exc),
            )

    def build_ticket_payload(self, state: ServiceState):
        raise NotImplementedError

    def build_specific_attributes(self, state: ServiceState) -> Dict[str, Any]:
        return {}
