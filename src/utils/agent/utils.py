import random
import string
import json


def gerar_conversa_aleatoria(num_mensagens: int, tamanho_content: int) -> list:
    """
    Gera uma lista de dicionários simulando uma conversa com conteúdo e papéis aleatórios.

    Args:
        num_mensagens (int): O número total de mensagens a serem geradas na conversa.
        tamanho_content (int): O comprimento da string de conteúdo aleatório para cada mensagem.

    Returns:
        list: Uma lista de dicionários, onde cada dicionário representa uma mensagem.
    """
    conversa = []
    papeis_possiveis = ["human", "ai"]

    # Define os caracteres que podem ser usados no conteúdo (letras + dígitos + espaços)
    caracteres_aleatorios = (
        string.ascii_letters + string.digits + " " * 10
    )  # Adiciona mais espaços para parecer mais "natural"

    for _ in range(num_mensagens):
        # Escolhe um papel aleatório da lista de papéis possíveis
        papel_aleatorio = random.choice(papeis_possiveis)

        # Gera o conteúdo aleatório com o tamanho especificado
        conteudo_aleatorio = "".join(
            random.choice(caracteres_aleatorios) for _ in range(tamanho_content)
        )

        # Cria o dicionário da mensagem e adiciona à lista da conversa
        mensagem = {"content": conteudo_aleatorio, "role": papel_aleatorio}
        conversa.append(mensagem)

    return conversa
