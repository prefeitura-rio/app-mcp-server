"""
API services para comunicação com APIs externas do IPTU.

Contém a implementação real da API, a versão fake para testes e exceções relacionadas.
"""

from src.tools.multi_step_service.workflows.iptu_pagamento.api.api_service import (
    IPTUAPIService,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.api.api_service_fake import (
    IPTUAPIServiceFake,
)
from src.tools.multi_step_service.workflows.iptu_pagamento.api.exceptions import (
    APIUnavailableError,
    DataNotFoundError,
    AuthenticationError,
)

__all__ = [
    "IPTUAPIService",
    "IPTUAPIServiceFake",
    "APIUnavailableError",
    "DataNotFoundError",
    "AuthenticationError",
]
