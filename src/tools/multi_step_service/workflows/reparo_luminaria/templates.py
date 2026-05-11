def formatar_conhecimento_servico(service_info: dict | None = None) -> str:
    if not service_info:
        return ""

    partes = []
    if service_info.get("nome"):
        partes.append(f"**Serviço:** {service_info['nome']}")
    if service_info.get("resumo"):
        partes.append(str(service_info["resumo"]))
    if service_info.get("prazo"):
        partes.append(f"**Prazo para atendimento:** {service_info['prazo']}")
    if service_info.get("servico_nao_cobre"):
        partes.append(
            f"**O que este serviço não cobre:**\n{service_info['servico_nao_cobre']}"
        )

    return "\n\n".join(partes)


def solicitar_defeito(service_info: dict | None = None) -> str:
    intro = formatar_conhecimento_servico(service_info)
    pergunta = (
        "Para abrir a solicitação, escolha uma das opções fechadas abaixo para o defeito da luminária:\n\n"
        "1. Apagada\n"
        "2. Piscando\n"
        "3. Acesa de dia\n"
        "4. Pendurada\n"
        "5. Danificada\n"
        "6. Com ruído"
    )
    return f"{intro}\n\n{pergunta}" if intro else pergunta


def defeito_invalido() -> str:
    return "Não entendi o defeito. Escolha uma das opções de 1 a 6."


def solicitar_quantidade() -> str:
    return "Esse defeito ocorre em apenas uma luminária ou em um grupo de luminárias?"


def quantidade_invalida() -> str:
    return "Não entendi. Responda se é apenas uma luminária ou um grupo de luminárias."


def solicitar_intercaladas_bloco() -> str:
    return (
        "Escolha a opção que descreve melhor o grupo de luminárias:\n\n"
        "1. As luminárias com defeito estão juntas, uma em seguida da outra.\n"
        "2. Há luminárias funcionando entre as luminárias com defeito."
    )


def intercaladas_bloco_invalido() -> str:
    return "Não entendi. Escolha 1 para juntas/em bloco ou 2 para intercaladas."


def solicitar_localizacao() -> str:
    return (
        "Onde está localizada a luminária com defeito? Escolha uma opção:\n\n"
        "1. Calçada\n"
        "2. Fachada\n"
        "3. Monumento\n"
        "4. Parque\n"
        "5. Praça\n"
        "6. Quadra de esportes\n"
        "7. Rua\n\n"
        "Caso não saiba, digite \"não sei\"."
    )


def localizacao_invalida() -> str:
    return "Não entendi a localização. Escolha uma opção de 1 a 7 ou responda 'não sei'."


def solicitar_endereco() -> str:
    return (
        "Informe o endereço onde está localizada a luminária com defeito.\n\n"
        "Inclua nome da rua, avenida ou praça, número se souber, e bairro.\n\n"
        "Exemplo: Rua Afonso Cavalcanti, 455, Cidade Nova"
    )


def endereco_nao_localizado(tentativa: int, max_tentativas: int) -> str:
    return (
        "Endereço incorreto ou não encontrado. "
        f"Verifique e informe novamente o endereço correto (tentativa {tentativa}/{max_tentativas})."
    )


def endereco_erro_processamento(tentativa: int, max_tentativas: int) -> str:
    return f"Ocorreu um erro ao processar o endereço. Tente novamente (tentativa {tentativa}/{max_tentativas})."


def endereco_maximo_tentativas() -> str:
    return "Não foi possível validar o endereço após 3 tentativas. Seu atendimento está finalizado."


def confirmar_endereco(endereco_formatado: str) -> str:
    return f"Por favor, confirme se o endereço está correto:\n\n{endereco_formatado}\n\nO endereço está correto?"


def endereco_historico(endereco_formatado: str) -> str:
    return (
        "Vi que você tem um endereço registrado no histórico. "
        f"Gostaria de solicitar o reparo de luminária para o endereço abaixo?\n\n"
        f"{endereco_formatado}\n\nEste endereço está correto?"
    )


def confirmar_resposta_invalida() -> str:
    return "Por favor, responda com sim ou não."


def solicitar_novo_endereco(tentativa: int, max_tentativas: int) -> str:
    return f"Por favor, informe novamente o endereço correto (tentativa {tentativa}/{max_tentativas})."


def solicitar_ponto_referencia() -> str:
    return "Se houver um ponto de referência para facilitar a localização da luminária, informe agora. Se não houver, diga que não tem."


def perguntar_quadra_esportes() -> str:
    return "O defeito está dentro de uma quadra de esportes?"


def confirmar_dados_ticket(dados_formatados: str) -> str:
    return f"Por favor, confirme os dados da sua solicitação:\n\n{dados_formatados}\n\nOs dados estão corretos?"


def solicitar_correcao_dados() -> str:
    return "Me diga o que precisa ser corrigido: defeito, quantidade, localização, endereço, ponto de referência, CPF, email ou nome."


def dados_corrigidos_solicitar_campo(campo: str) -> str:
    mensagens = {
        "defeito": "Por favor, informe o defeito correto:",
        "quantidade": "Por favor, informe se é uma luminária ou um grupo:",
        "intercaladas_bloco": "Por favor, informe 1 para bloco ou 2 para intercaladas:",
        "localizacao": "Por favor, informe a localização correta:",
        "endereco": "Por favor, informe o endereço correto:",
        "ponto_referencia": "Por favor, informe o ponto de referência correto ou avance para remover:",
        "nome": "Por favor, informe seu nome completo correto:",
        "cpf": "Por favor, informe o CPF correto ou avance:",
        "email": "Por favor, informe o email correto ou avance:",
    }
    return mensagens.get(campo, f"Por favor, informe o valor correto para {campo}:")


def solicitacao_criada_sucesso(protocol_id: str) -> str:
    return (
        f"Sua solicitação foi criada com sucesso. O número do protocolo é {protocol_id}.\n\n"
        + msg_solicitacao()
    )


def solicitacao_existente(protocol_id: str) -> str:
    return f"A solicitação {protocol_id} já existe.\n\n" + msg_solicitacao()


def msg_solicitacao() -> str:
    return "Você pode acompanhar sua solicitação informando o protocolo em https://www.1746.rio/hc/pt-br/p/solicitacoes"


def erro_criar_solicitacao() -> str:
    return "Houve um erro e a solicitação não pôde ser criada. Tente novamente em alguns minutos."


def sistema_indisponivel() -> str:
    return "O sistema está indisponível no momento. Tente novamente em alguns minutos."


def erro_geral_chamado() -> str:
    return "Houve um erro ao abrir o chamado. Tente novamente mais tarde."


def reiniciar_apos_erro(error_msg: str) -> str:
    return f"{error_msg}\n\nVamos tentar novamente. Informe o endereço da luminária:"
