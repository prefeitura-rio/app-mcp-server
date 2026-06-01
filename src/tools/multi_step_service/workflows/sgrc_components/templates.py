def solicitar_metodo_identificacao() -> str:
    """Prompt for identification method selection (CPF or Gov.br)."""
    return (
        "Como você gostaria de se identificar?\n\n"
        "📋 *CPF* - Digite seu CPF para buscar seus dados\n"
        "🔐 *Gov.br* - Autentique com sua conta gov.br (mais rápido e seguro)\n\n"
        "Escolha uma opção."
    )


def metodo_identificacao_invalido(tentativa: int, max_tentativas: int = 3) -> str:
    """Error message for invalid identification method."""
    return (
        f"Opção inválida. Tentativa {tentativa}/{max_tentativas}.\n\n"
        f"Por favor, escolha:\n"
        f"- *CPF* para identificar via CPF\n"
        f"- *Gov.br* para autenticar com gov.br"
    )


def govbr_autenticacao_iniciada() -> str:
    """Message after sending gov.br auth button."""
    return (
        "✅ Link de autenticação gov.br enviado!\n\n"
        "Por favor, clique no botão acima para fazer login com sua conta gov.br.\n\n"
        "Quando terminar, me envie uma mensagem para continuar."
    )


def govbr_autenticacao_pendente() -> str:
    """Message when waiting for gov.br authentication to complete."""
    return (
        "⏳ Ainda estou aguardando você completar a autenticação gov.br.\n\n"
        "Se você já autenticou, aguarde alguns segundos e me envie uma mensagem.\n\n"
        "Se não recebeu o link, diga 'tentar novamente'."
    )


def govbr_autenticacao_erro() -> str:
    """Error message when gov.br auth fails."""
    return (
        "❌ Não consegui completar a autenticação gov.br.\n\n"
        "Você pode:\n"
        "- Dizer 'tentar novamente' para receber um novo link\n"
        "- Dizer 'usar CPF' para se identificar via CPF"
    )


def govbr_dados_coletados(nome: str) -> str:
    """Confirmation message after successful gov.br auth."""
    return f"✅ Autenticação gov.br concluída! Olá, {nome}!"


def solicitar_cpf(required: bool = False) -> str:
    base = "Por favor, informe seu CPF para buscarmos seus dados cadastrais."
    if required:
        return base + " Este serviço exige identificação para continuar."
    return base + " Se preferir não se identificar, diga que quer continuar sem CPF."


def cpf_invalido(
    tentativa: int, max_tentativas: int = 3, required: bool = False
) -> str:
    if required:
        complemento = "Informe um CPF válido para continuar."
    else:
        complemento = "Informe um CPF válido ou diga que quer continuar sem CPF."
    return f"CPF inválido. Tentativa {tentativa}/{max_tentativas}. {complemento}"


def maximo_tentativas_excedido(required: bool = False) -> str:
    if required:
        return "Número máximo de tentativas excedido. Não foi possível continuar sem CPF válido."
    return "Número máximo de tentativas excedido. Continuando sem identificação."


def solicitar_email(required: bool = False) -> str:
    base = "Por favor, informe seu email."
    if required:
        return base + " Este serviço exige email para continuar."
    return base + " Caso não queira informar, diga que quer continuar sem email."


def email_invalido(
    tentativa: int, max_tentativas: int = 3, required: bool = False
) -> str:
    if required:
        complemento = "Informe um email válido para continuar."
    else:
        complemento = "Informe um email válido ou diga que quer continuar sem email."
    return f"Email inválido. Tentativa {tentativa}/{max_tentativas}. {complemento}"


def email_maximo_tentativas(required: bool = False) -> str:
    if required:
        return "Número máximo de tentativas excedido. Não foi possível continuar sem email válido."
    return "Número máximo de tentativas excedido. Continuando sem email."


def solicitar_nome(required: bool = False) -> str:
    base = "Por favor, informe seu nome completo."
    if required:
        return base + " Este serviço exige nome completo para continuar."
    return base + " Caso não queira informar, diga que quer continuar sem nome."


def nome_invalido(
    tentativa: int, max_tentativas: int = 3, required: bool = False
) -> str:
    if required:
        complemento = "Informe nome e sobrenome para continuar."
    else:
        complemento = "Informe nome e sobrenome ou diga que quer continuar sem nome."
    return f"Nome inválido. Tentativa {tentativa}/{max_tentativas}. {complemento}"


def nome_maximo_tentativas(required: bool = False) -> str:
    if required:
        return "Número máximo de tentativas excedido. Não foi possível continuar sem nome válido."
    return "Número máximo de tentativas excedido. Continuando sem nome."


def confirmar_dados_salvos(dados_mascarados: list) -> str:
    message = "Você tem os seguintes dados salvos:\n\n"
    message += "\n".join(dados_mascarados)
    message += "\n\nDeseja usar esses dados?"
    return message
