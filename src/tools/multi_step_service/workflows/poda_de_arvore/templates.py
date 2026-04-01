"""
Templates de mensagens para o workflow de poda de árvore.

Este módulo centraliza todos os textos e mensagens exibidas ao usuário
durante o fluxo de solicitação de poda de árvore.
"""


# ========== ENDEREÇO ==========


def solicitar_endereco() -> str:
    """Mensagem solicitando endereço."""
    return (
        "🌳 **Informe o endereço onde está localizada a árvore que precisa de poda:**\n\n"
        "Por favor, inclua:\n"
        "• Nome da rua, avenida, praça ou estrada\n"
        "• Número (se souber)\n"
        "• Bairro\n\n"
        "⚠️ **Atenção:** Este deve ser o endereço da árvore, não o seu endereço residencial.\n\n"
        "**Exemplo:** Rua Afonso Cavalcanti, 455, Cidade Nova"
    )


def endereco_nao_localizado(tentativa: int, max_tentativas: int) -> str:
    """Mensagem quando endereço não foi localizado."""
    return (
        f"❌ **Endereço incorreto ou não encontrado.**\n\n"
        f"O endereço informado não foi localizado na base de dados da Prefeitura do Rio de Janeiro. "
        f"Isso pode acontecer quando:\n"
        f"• O bairro informado não corresponde ao endereço\n"
        f"• O nome da rua está incorreto ou incompleto\n"
        f"• Há erro de digitação\n\n"
        f"Por favor, verifique e informe o endereço correto (tentativa {tentativa}/{max_tentativas}).\n\n"
        f"**Exemplo:** Rua Afonso Cavalcanti, 455, Cidade Nova"
    )


def endereco_erro_processamento(tentativa: int, max_tentativas: int) -> str:
    """Mensagem de erro ao processar endereço."""
    return f"Ocorreu um erro ao processar o endereço. Por favor, tente novamente (tentativa {tentativa}/{max_tentativas})."


def endereco_maximo_tentativas() -> str:
    """Mensagem quando atinge máximo de tentativas de endereço."""
    return (
        "Não foi possível validar o endereço após 3 tentativas. "
        "Por favor, tente novamente mais tarde. Seu atendimento está finalizado."
    )


def confirmar_endereco(endereco_formatado: str) -> str:
    """Mensagem pedindo confirmação do endereço."""
    return f"Por favor, confirme se o endereço está correto:\n\n{endereco_formatado}\n\nO endereço está correto?"


def endereco_historico(endereco_formatado: str) -> str:
    """Mensagem quando há endereço no histórico."""
    return (
        "Vi que você tem um endereço registrado no histórico. "
        f"Gostaria de solicitar a poda de árvore para o endereço abaixo?\n\n"
        f"{endereco_formatado}\n\nEste endereço está correto?"
    )


def confirmar_resposta_invalida() -> str:
    """Mensagem quando resposta de confirmação é inválida."""
    return (
        "Por favor, confirme se o endereço está correto respondendo com 'sim' ou 'não'."
    )


def solicitar_novo_endereco(tentativa: int, max_tentativas: int) -> str:
    """Mensagem solicitando novo endereço após recusa."""
    return (
        f"Por favor, informe novamente o endereço correto (tentativa {tentativa}/{max_tentativas}).\n\n"
        f"**Lembre-se de incluir:**\n"
        f"• Nome da rua/avenida\n"
        f"• Número (se souber)\n"
        f"• Bairro correto\n\n"
        f"**Exemplo:** Rua Afonso Cavalcanti, 455, Cidade Nova"
    )


# ========== PONTO DE REFERÊNCIA ==========


def solicitar_ponto_referencia() -> str:
    """Mensagem solicitando ponto de referência."""
    return (
        "Agora você pode informar um ponto de referência para ajudar a encontrar o local para o atendimento.\n\n"
        "Se for dentro de loteamento, conjunto habitacional, vila ou condomínio, "
        "descreva como chegar no local a partir do endereço de acesso.\n"
        "Se for vila com portão, informe também a casa que abrirá o portão.\n"
        "Se não for necessário, responda AVANÇAR."
    )


# ========== IDENTIFICAÇÃO (CPF) ==========


def solicitar_cpf() -> str:
    """Mensagem solicitando CPF."""
    return "Por favor, informe seu CPF para que possamos buscar seus dados cadastrais. Caso prefira não se identificar, digite 'avançar' para prosseguir sem identificação."


def cpf_invalido(tentativa: int, max_tentativas: int = 3) -> str:
    """Mensagem quando CPF é inválido."""
    return f"CPF inválido. Tentativa {tentativa}/{max_tentativas}. {solicitar_cpf()}"


def maximo_tentativas_excedido() -> str:
    """Mensagem genérica quando máximo de tentativas é excedido."""
    return "Número máximo de tentativas excedido. Continuando sem identificação."


# ========== EMAIL ==========


def solicitar_email() -> str:
    """Mensagem solicitando email."""
    return "Por favor, informe seu email. Caso não queira enviar, digite 'avançar' para prosseguir sem email."


def email_invalido(tentativa: int, max_tentativas: int = 3) -> str:
    """Mensagem quando email é inválido."""
    return f"Email inválido. Tentativa {tentativa}/{max_tentativas}. Por favor, informe um email válido (ou deixe em branco para pular)."


def email_maximo_tentativas() -> str:
    """Mensagem quando máximo de tentativas de email é excedido."""
    return "Número máximo de tentativas excedido. Continuando sem email."


# ========== NOME ==========


def solicitar_nome() -> str:
    """Mensagem solicitando nome."""
    return "Por favor, informe seu nome completo. Caso não queira enviar, digite 'avançar' para prosseguir sem nome."


def nome_invalido(tentativa: int, max_tentativas: int = 3) -> str:
    """Mensagem quando nome é inválido."""
    return f"Nome inválido. Tentativa {tentativa}/{max_tentativas}. Por favor, informe um nome válido com nome e sobrenome (ou deixe em branco para pular)."


def nome_maximo_tentativas() -> str:
    """Mensagem quando máximo de tentativas de nome é excedido."""
    return "Número máximo de tentativas excedido. Continuando sem nome."


# ========== DADOS PESSOAIS SALVOS ==========


def confirmar_dados_salvos(dados_mascarados: list) -> str:
    """Mensagem pedindo confirmação de dados pessoais salvos."""
    message = "Você tem os seguintes dados salvos:\n\n"
    message += "\n".join(dados_mascarados)
    message += "\n\nDeseja usar esses dados?"
    return message


# ========== CRIAÇÃO DE TICKET ==========


def solicitacao_criada_sucesso(protocol_id: str) -> str:
    """Mensagem de sucesso ao criar solicitação."""
    return (
        f"Sua solicitação foi criada com sucesso. "
        f"O número do protocolo é {protocol_id}.\n\n" + msg_solicitacao()
    )


def solicitacao_existente(protocol_id: str) -> str:
    """Mensagem quando solicitação já existe."""
    return f"A solicitação {protocol_id} já existe.\n\n" + msg_solicitacao()


def msg_solicitacao() -> str:
    """Mensagem informando sobre a criação da solicitação."""
    return "Você pode acompanhar sua solicitação informando o protocolo em https://www.1746.rio/hc/pt-br/p/solicitacoes"


def erro_criar_solicitacao() -> str:
    """Mensagem de erro ao criar solicitação."""
    return "Infelizmente houve um erro e a solicitação não pôde ser criada.\n\nPor favor, tente novamente em alguns minutos."


def sistema_indisponivel() -> str:
    """Mensagem quando sistema está indisponível."""
    return "O sistema está indisponível no momento.\n\nPor favor, tente novamente em alguns minutos."


def erro_geral_chamado() -> str:
    """Mensagem de erro geral ao abrir chamado."""
    return "Houve um erro ao abrir o chamado.\n\nPor favor, tente novamente mais tarde."


def reiniciar_apos_erro(error_msg: str) -> str:
    """Mensagem ao reiniciar após erro."""
    return (
        f"❌ {error_msg}\n\n"
        "Vamos tentar novamente.\n\n"
        "📍 Por favor, informe novamente o endereço completo de onde está a árvore:"
    )


# ========== CONFIRMAÇÃO DE DADOS DO TICKET ==========


def confirmar_dados_ticket(dados_formatados: str) -> str:
    """Mensagem pedindo confirmação final dos dados antes de criar o ticket."""
    return (
        "Por favor, confirme os dados da sua solicitação:\n\n"
        f"{dados_formatados}\n\n"
        "Os dados estão corretos? Responda com SIM ou NÃO."
    )


def solicitar_correcao_dados() -> str:
    """Mensagem solicitando que o usuário informe o que precisa ser corrigido."""
    return (
        "Entendi que há algo incorreto nos dados.\n\n"
        "Por favor, me diga o que precisa ser corrigido. Por exemplo:\n"
        "- 'O endereço está errado'\n"
        "- 'Quero mudar meu nome'\n"
        "- 'O email está incorreto'\n"
        "- 'Quero adicionar ponto de referência'\n\n"
        "Ou se você não deseja mais criar a solicitação, diga 'cancelar'.\n\n"
        "O que você gostaria de fazer?"
    )


def dados_corrigidos_solicitar_campo(campo: str) -> str:
    """Mensagem solicitando o novo valor para um campo específico."""
    mensagens = {
        "endereco": "Por favor, informe o endereço correto:",
        "nome": "Por favor, informe seu nome completo correto:",
        "cpf": "Por favor, informe o CPF correto (ou deixe vazio para pular):",
        "email": "Por favor, informe o email correto (ou deixe vazio para pular):",
        "ponto_referencia": "Por favor, informe o ponto de referência correto (ou deixe vazio para remover):",
    }
    return mensagens.get(campo, f"Por favor, informe o valor correto para {campo}:")
