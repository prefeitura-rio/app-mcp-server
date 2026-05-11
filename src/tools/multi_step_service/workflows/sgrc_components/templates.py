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
