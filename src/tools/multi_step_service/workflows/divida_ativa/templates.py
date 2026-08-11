"""
Templates de mensagens para o workflow Dívida Ativa.
"""


class DividaAtivaTemplates:
    """Templates de mensagens ao usuário para o workflow de Dívida Ativa."""

    TIPO_CONSULTA_LABELS = {
        "cpf_cnpj": "CPF/CNPJ",
        "inscricao_imobiliaria": "Inscrição Imobiliária",
        "auto_infracao": "Nº e Ano do Auto de Infração",
        "cda": "Certidão de Dívida Ativa",
        "execucao_fiscal": "Número de Execução Fiscal",
    }
    OPCAO_MENU_LABELS = {
        "pagar_a_vista": "Pagar à vista",
        "parcelar_debitos": "Parcelar débitos",
        "regularizar_debitos": "Regularizar débitos",
        "liquidar_parcelamento": "Liquidar parcelamento",
        "emitir_2_via": "Emitir 2ª via",
        "voltar": "Voltar",
    }

    @staticmethod
    def solicitar_tipo_consulta() -> str:
        return (
            "Como você quer consultar seus débitos? Vou te mostrar os "
            "*tipos de consulta* na lista a seguir."
        )

    @staticmethod
    def consultar_cpf_cnpj() -> str:
        return "Fala pra mim o *número* do *CPF/CNPJ* do contribuinte"

    @staticmethod
    def consultar_inscricao_imobiliaria() -> str:
        return "Fala pra mim o *número* da *Inscrição Imobiliária*"

    @staticmethod
    def consultar_auto_infracao() -> str:
        return "Fala pra mim o *ano* e o *número* do *Auto de Infração*"

    @staticmethod
    def consultar_cda() -> str:
        return "Fala pra mim o *número* da *CDA* (Certidão de Dívida Ativa)"

    @staticmethod
    def consultar_execucao_fiscal() -> str:
        return "Fala pra mim o *número* da *EF* (Execução Fiscal)"

    @staticmethod
    def consulta_sem_debitos() -> str:
        return "Não encontrei débitos de Dívida Ativa para os dados informados."

    @staticmethod
    def consulta_realizada() -> str:
        return "Consulta de Dívida Ativa realizada com sucesso."

    @staticmethod
    def escolher_opcao_pagamento() -> str:
        return "Como você quer seguir com o pagamento?"

    @staticmethod
    def opcao_menu_indisponivel() -> str:
        return (
            "Essa opção não está disponível. Escolha uma das opções da lista "
            "para continuar."
        )

    @staticmethod
    def opcao_botao_indisponivel() -> str:
        return "Essa opção não existe. Escolha um dos botões para continuar."

    @staticmethod
    def input_inesperado() -> str:
        return "Não consegui entender esse input. Responda no formato esperado para continuar."

    @staticmethod
    def pagar_a_vista() -> str:
        return (
            "Para pagamento à vista, você pode optar por pagar tudo ou "
            "escolher os débitos que deseja pagar.\n\n"
            "O que prefere fazer?"
        )

    @staticmethod
    def parcelar_debitos() -> str:
        return (
            "Para parcelar seus débitos, entre no site:\n"
            "https://carioca.rio/servicos/parcelamento-em-divida-ativa/\n\n"
            "Depois, clique em ACESSAR O SERVIÇO e siga as instruções."
        )

    @staticmethod
    def regularizar_debitos() -> str:
        return "Para regularizar todas as parcelas em atraso, digite *TODAS*."

    @staticmethod
    def liquidar_parcelamento() -> str:
        return (
            "Para liquidar suas guias de parcelamento, entre no site:\n"
            "https://daminternet.rio.rj.gov.br/GuiaPagamento/Liquidacao"
        )

    @staticmethod
    def emitir_2_via() -> str:
        return (
            "Para Emitir 2ª via de cotas da Guia de Parcelamento, entre no "
            "site:\n"
            "https://daminternet.rio.rj.gov.br/GuiaPagamento/EmitirSegundaVia"
        )

    @staticmethod
    def escolher_debitos_a_vista(total_debitos: int) -> str:
        return (
            "Entendi.\n\n"
            f"Os débitos foram numerados de 1 a {total_debitos}. Para escolher "
            "os débitos que deseja pagar, informe os números associados "
            "separados por vírgula.\n\n"
            "Exemplo: 1, 2, 4"
        )

    @staticmethod
    def debitos_escolhidos_invalidos(total_debitos: int) -> str:
        return (
            "Não consegui identificar esses débitos. Informe os números "
            f"associados de 1 a {total_debitos}, separados por vírgula.\n\n"
            "Exemplo: 1, 2, 4"
        )

    @staticmethod
    def confirmar_pagamento_a_vista(debitos: list[str]) -> str:
        debitos_formatados = "\n".join(debitos) if debitos else "N/A"
        return (
            "Combinado.\n\n"
            "Os débitos escolhidos foram:\n"
            f"{debitos_formatados}\n\n"
            "Deseja seguir para o pagamento?"
        )

    @staticmethod
    def pagamento_a_vista_confirmado() -> str:
        return (
            "Certo.\n\n"
            "Agora escolha uma das três opções para fazer o pagamento:"
        )

    @staticmethod
    def pagamento_a_vista_recusado() -> str:
        return (
            "Tudo bem. Vamos tentar novamente.\n\n"
            "Você pode escolher os débitos que deseja pagar à vista, ver as "
            "opções de pagamento (à vista, parcelar, liquidar parcelas, "
            "regularizar, emitir 2ª via) ou encerrar o atendimento.\n\n"
            "O que deseja fazer?"
        )

    @staticmethod
    def forma_pagamento_a_vista_selecionada() -> str:
        return "Beleza."

    @staticmethod
    def boleto_bancario_a_vista(link: str) -> str:
        return (
            "Beleza.\n\n"
            "Clique no link para o pagamento por *boleto bancário*:\n"
            f"{link}"
        )

    @staticmethod
    def codigo_barras_a_vista(codigo: str) -> str:
        return (
            "Beleza.\n\n"
            "Faça o pagamento por *código de barras* usando o código:\n\n"
            f"{codigo}"
        )

    @staticmethod
    def pix_copia_e_cola_a_vista(pix: str) -> str:
        return (
            "Beleza.\n\n"
            "Copie o código Pix e cole no aplicativo do seu banco para fazer "
            "o pagamento:\n\n"
            f"{pix}"
        )

    @staticmethod
    def guia_a_vista_nao_emitida() -> str:
        return "Não consegui emitir a guia à vista para os débitos selecionados."

    @staticmethod
    def atendimento_encerrado() -> str:
        return (
            "Tudo bem! A *Prefeitura do Rio* agradece a sua confiança.\n"
            "Seu atendimento será finalizado. Obrigada!"
        )

    @classmethod
    def formatar_resultado_consulta(
        cls,
        tipo_consulta: str,
        valor_consulta: str,
        divida_info,
    ) -> str:
        linhas = [
            "Tipo de consulta:",
            f"{cls.TIPO_CONSULTA_LABELS.get(tipo_consulta, tipo_consulta)}: "
            f"{valor_consulta}",
        ]

        if tipo_consulta == "inscricao_imobiliaria" and divida_info.endereco_imovel:
            linhas.extend(
                [
                    "",
                    "*Endereço do imóvel:*",
                    divida_info.endereco_imovel,
                ]
            )

        if divida_info.cdas:
            linhas.extend(["", "*Certidões de Dívida Ativa não parceladas:*"])
            for indice, cda in enumerate(divida_info.cdas, start=1):
                cda_id = cda.cda_id or cda.numero or "N/A"
                valor = cda.valor_saldo_total or cda.valor_original or "N/A"
                linhas.append(f"{indice}. {cda_id}")
                linhas.append(f"Valor: {valor}")

        if divida_info.efs:
            linhas.extend(["", "*Execuções Fiscais não parceladas:*"])
            for indice, ef in enumerate(divida_info.efs, start=1):
                numero = ef.numero_execucao_fiscal or ef.numero_ef or "N/A"
                valor = (
                    ef.saldo_execucao_fiscal_nao_parcelada
                    or ef.valor_original
                    or "N/A"
                )
                linhas.append(f"{indice}. {numero}")
                linhas.append(f"Valor {valor}")

        if divida_info.parcelamentos:
            linhas.extend(["", "*Guias de parcelamento encontradas:*"])
            for indice, guia in enumerate(divida_info.parcelamentos, start=1):
                numero = guia.numero or "N/A"
                data_ultimo_pagamento = guia.data_ultimo_pagamento or "N/A"
                linhas.append(
                    f"{indice}. Guia nº {numero} - Data do Último Pagamento: "
                    f"{data_ultimo_pagamento}"
                )

        if divida_info.cdas or divida_info.efs:
            saldo = divida_info.saldo_total_nao_parcelado or "N/A"
            linhas.extend(["", "*Débitos não parcelados:*", "Valor Total da Dívida:"])
            linhas.append(saldo)

        linhas.extend(
            [
                "",
                f"Data de Vencimento: {divida_info.data_vencimento or 'N/A'}",
            ]
        )

        return "\n".join(linhas)
