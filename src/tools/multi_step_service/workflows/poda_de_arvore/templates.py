"""
Templates de mensagens para o workflow de identificação.

Este módulo centraliza todos os textos e mensagens exibidas ao agente
durante o fluxo de identificação de usuários, seguindo o padrão do IPTU.
"""


class IdentificationMessageTemplates:
    """Templates de mensagens para cada etapa do workflow de identificação."""
        
    @staticmethod
    def solicitar_cpf() -> str:
        """Mensagem solicitando CPF."""
        return "Por favor, informe seu CPF para que possamos buscar seus dados cadastrais. Caso prefira não se identificar, digite 'avançar' para prosseguir sem identificação."
    