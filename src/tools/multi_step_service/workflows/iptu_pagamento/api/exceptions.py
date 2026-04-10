"""
Exceções customizadas para o workflow IPTU.

Define exceções específicas para diferenciar tipos de erro.
"""


class IPTUAPIException(Exception):
    """Exceção base para erros de API do IPTU."""
    pass


class APIUnavailableError(IPTUAPIException):
    """
    Erro quando a API está indisponível.

    Casos:
    - Timeout de requisição
    - Erro 500 (Internal Server Error)
    - Erro 503 (Service Unavailable)
    - Erros de rede/conexão
    """
    pass


class DataNotFoundError(IPTUAPIException):
    """
    Erro quando os dados solicitados não foram encontrados.

    Casos:
    - Lista vazia retornada pela API
    - Inscrição imobiliária não existe
    - Ano de exercício sem guias
    - Guia não possui cotas
    """
    pass


class AuthenticationError(IPTUAPIException):
    """
    Erro de autenticação na API.

    Casos:
    - Erro 401 (Unauthorized)
    - Token inválido ou expirado
    """
    pass


class InvalidInscricaoError(IPTUAPIException):
    """
    Erro quando a inscrição imobiliária é inválida.

    Casos:
    - Código 033: Inscrição imobiliária inválida
    - Inscrição não existe no sistema
    - Formato de inscrição incorreto
    """

    def __init__(self, inscricao: str, message: str = None):
        self.inscricao = inscricao
        self.message = message or f"Inscrição imobiliária '{inscricao}' não encontrada ou inválida"
        super().__init__(self.message)
