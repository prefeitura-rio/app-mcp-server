"""
Templates de mensagens para o workflow de poda de árvore.

Este módulo centraliza todos os textos e mensagens exibidas ao usuário
durante o fluxo de solicitação de poda de árvore.
"""


def solicitar_cpf() -> str:
    """Mensagem solicitando CPF."""
    return "Por favor, informe seu CPF para que possamos buscar seus dados cadastrais. Caso prefira não se identificar, digite 'avançar' para prosseguir sem identificação."


# Mantém a classe para compatibilidade com código existente
class IdentificationMessageTemplates:
    """Templates de mensagens para cada etapa do workflow."""
        
    @staticmethod
    def solicitar_cpf() -> str:
        """Mensagem solicitando CPF."""
        return solicitar_cpf()
