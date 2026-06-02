"""
Formatadores para exibição de dados sensíveis de forma mascarada.

Usa o símbolo • (bullet point) ao invés de * para evitar formatação markdown no WhatsApp.
"""


def mask_cpf(cpf: str) -> str:
    """
    Mascara CPF mantendo apenas os 3 primeiros dígitos e os 2 últimos.

    Exemplos:
        12345678901 -> 123.•••.•••-01
        123.456.789-01 -> 123.•••.•••-01

    Args:
        cpf: CPF com 11 dígitos (com ou sem formatação)

    Returns:
        CPF mascarado no formato 123.•••.•••-01
    """
    # Remove tudo que não é dígito
    cpf_numeros = "".join(c for c in cpf if c.isdigit())

    if len(cpf_numeros) != 11:
        return cpf  # Retorna original se não tiver 11 dígitos

    # Formata: 123.•••.•••-01
    return f"{cpf_numeros[:3]}.•••.•••-{cpf_numeros[9:]}"


def mask_email(email: str) -> str:
    """
    Mascara email mantendo apenas os 2 primeiros caracteres antes do @.

    Exemplos:
        joao@example.com -> jo•••@example.com
        maria.silva@gmail.com -> ma•••@gmail.com
        a@test.com -> a•••@test.com

    Args:
        email: Email válido

    Returns:
        Email mascarado
    """
    if "@" not in email:
        return email  # Retorna original se não for email válido

    local, domain = email.split("@", 1)

    if len(local) <= 2:
        # Se local tem 1 ou 2 caracteres, mostra só o primeiro
        masked_local = local[0] + "•••"
    else:
        # Mostra os 2 primeiros caracteres
        masked_local = local[:2] + "•••"

    return f"{masked_local}@{domain}"


def mask_phone(phone: str) -> str:
    """
    Mascara telefone mantendo apenas DDD e último dígito.

    Exemplos:
        (21) 98888-7777 -> (21) 9••••-7777
        21988887777 -> (21) 9••••-7777
        988887777 -> 9••••-7777

    Args:
        phone: Telefone com ou sem formatação

    Returns:
        Telefone mascarado
    """
    # Remove tudo que não é dígito
    phone_numeros = "".join(c for c in phone if c.isdigit())

    if len(phone_numeros) == 11:
        # Celular com DDD: (21) 9••••-7777
        ddd = phone_numeros[:2]
        primeiro_digito = phone_numeros[2]
        ultimos = phone_numeros[-4:]
        return f"({ddd}) {primeiro_digito}••••-{ultimos}"

    elif len(phone_numeros) == 10:
        # 10 dígitos pode ser fixo com DDD ou celular antigo
        ddd = phone_numeros[:2]
        terceiro_digito = phone_numeros[2]
        ultimos = phone_numeros[-4:]

        # Se começa com 9, é celular antigo (antes do 9º dígito): (21) 9••••-7777
        # Se não, é fixo: (21) ••••-7777
        if terceiro_digito == "9":
            return f"({ddd}) {terceiro_digito}••••-{ultimos}"
        else:
            return f"({ddd}) ••••-{ultimos}"

    elif len(phone_numeros) == 9:
        # Celular sem DDD: 9••••-7777
        primeiro_digito = phone_numeros[0]
        ultimos = phone_numeros[-4:]
        return f"{primeiro_digito}••••-{ultimos}"

    elif len(phone_numeros) == 8:
        # Fixo sem DDD: ••••-7777
        ultimos = phone_numeros[-4:]
        return f"••••-{ultimos}"

    else:
        return phone  # Retorna original se não reconhecer o formato
