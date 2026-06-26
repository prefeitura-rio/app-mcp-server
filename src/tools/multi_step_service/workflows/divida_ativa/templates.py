from typing import Any


class DividaAtivaTemplates:
    @staticmethod
    def escolher_tipo_consulta() -> str:
        return (
            "Escolha como deseja fazer sua consulta:\n\n"
            "1. Pelo código da Inscrição Imobiliária\n"
            "2. Pelo número da Certidão da Dívida Ativa (CDA)\n"
            "3. Pelo CPF/CNPJ do contribuinte\n"
            "4. Pelo número da Execução Fiscal\n"
            "5. Pelo número e ano do Auto de Infração"
        )

    @staticmethod
    def solicitar_ano_auto() -> str:
        return "Informe apenas o *ano* do Auto de Infração."

    @staticmethod
    def solicitar_valor(tipo_consulta: str) -> str:
        mensagens = {
            "inscricaoImobiliaria": "Informe o Código de Inscrição Imobiliária.",
            "cda": "Informe o Código da Certidão de Dívida Ativa.",
            "cpfCnpj": "Informe o CPF/CNPJ do contribuinte.",
            "numeroExecucaoFiscal": "Informe o Nº da Execução Fiscal.",
            "numeroAutoInfracao": "Informe o Nº do Auto de Infração.",
        }
        return mensagens.get(tipo_consulta, "Informe o valor para consulta.")

    @staticmethod
    def escolher_acao_interactive(
        total_nao_parcelado: int, total_parcelado: int
    ) -> dict:
        """Retorna kwargs `sections` + `button_label` para build_list_envelope."""
        if total_nao_parcelado > 0 and total_parcelado > 0:
            rows = [
                {"id": "pagar_a_vista", "title": "Pagar à vista"},
                {"id": "parcelar_debitos", "title": "Parcelar débitos"},
                {"id": "regularizar_debitos", "title": "Regularizar débitos"},
                {"id": "liquidar_parcelamento", "title": "Liquidar parcelamento"},
                {"id": "emitir_segunda_via", "title": "Emitir 2ª via"},
            ]
        elif total_nao_parcelado > 0:
            rows = [
                {"id": "pagar_a_vista", "title": "Pagar à vista"},
                {"id": "parcelar_debitos", "title": "Parcelar débitos"},
            ]
        else:
            rows = [
                {"id": "regularizar_debitos", "title": "Regularizar débitos"},
                {"id": "liquidar_parcelamento", "title": "Liquidar parcelamento"},
                {"id": "emitir_segunda_via", "title": "Emitir 2ª via"},
            ]
        return {
            "sections": [{"title": "Opções disponíveis", "rows": rows}],
            "button_label": "Ver opções",
        }

    @staticmethod
    def escolher_acao(total_nao_parcelado: int, total_parcelado: int) -> str:
        if total_nao_parcelado > 0 and total_parcelado > 0:
            return (
                "O que deseja fazer?\n\n"
                "1. Pagar à vista\n"
                "2. Parcelar débitos\n"
                "3. Regularizar débitos\n"
                "4. Liquidar parcelamento\n"
                "5. Emitir 2ª via"
            )
        if total_nao_parcelado > 0:
            return "O que deseja fazer?\n\n1. Pagar à vista\n2. Parcelar débitos"
        return (
            "O que deseja fazer?\n\n"
            "1. Regularizar débitos\n"
            "2. Liquidar parcelamento\n"
            "3. Emitir 2ª via"
        )

    @staticmethod
    def link_parcelamento() -> str:
        return (
            "Para *parcelar seus débitos*, entre no site:\n"
            "https://carioca.rio/servicos/parcelamento-em-divida-ativa/\n"
            "Depois, clique em ACESSAR O SERVIÇO e siga as instruções."
        )

    @staticmethod
    def link_liquidacao() -> str:
        return (
            "Para *liquidar* suas guias de *parcelamento*, entre no site:\n"
            "https://daminternet.rio.rj.gov.br/GuiaPagamento/liquidacao"
        )

    @staticmethod
    def link_segunda_via() -> str:
        return (
            "Para *emitir 2ª via* das suas guias de pagamento, entre no site:\n"
            "https://daminternet.rio.rj.gov.br/GuiaPagamento/EmitirSegundaVia"
        )

    @staticmethod
    def solicitar_itens(acao: str) -> str:
        if acao == "pagar_a_vista":
            return (
                "Quais débitos deseja pagar?\n\n"
                "💡 Você pode responder *TODOS* para pagar tudo, ou informar os números específicos "
                "separados por vírgula (exemplo: 1, 2, 4)."
            )
        return (
            "Quais débitos deseja regularizar?\n\n"
            "💡 Você pode responder *TODOS* para regularizar tudo, ou informar os números específicos "
            "separados por vírgula (exemplo: 1, 2, 4)."
        )

    @staticmethod
    def opcao_invalida() -> str:
        return (
            "As opções informadas foram inválidas. Por favor, informe uma opção válida."
        )

    @staticmethod
    def nenhuma_divida(mensagem_consulta: str) -> str:
        if mensagem_consulta:
            return mensagem_consulta
        return "Não foram encontrados débitos para os dados informados."

    @staticmethod
    def confirmar_debitos_selecionados(debitos: list[dict[str, Any]], acao: str) -> str:
        """Mostra os débitos selecionados e pede confirmação."""
        if acao == "pagar_a_vista":
            texto = "Os débitos escolhidos foram:\n\n"
        else:
            texto = "As parcelas escolhidas foram:\n\n"

        for i, debito in enumerate(debitos, 1):
            if "cda" in debito:
                texto += f"{i}. CDA nº {debito['cda']} - Valor: {debito['valor']}\n"
            elif "ef" in debito:
                texto += f"{i}. EF nº {debito['ef']} - Valor: {debito['valor']}\n"
            elif "guia" in debito:
                texto += f"{i}. Guia nº {debito['guia']} - Data do Último Pagamento: {debito.get('data_ultimo_pagamento', 'N/A')}\n"

        texto += "\nDeseja seguir para o pagamento?"
        return texto

    @staticmethod
    def guia_emitida_escolher_forma(resultado: dict[str, Any]) -> tuple[str, dict]:
        """
        Retorna (texto_descricao, kwargs_interactive) para perguntar
        qual forma de pagamento o cidadão quer receber.
        Inclui apenas as opções realmente disponíveis na resposta da API.
        """
        vencimento = resultado.get("data_vencimento", "")
        texto = "Guia de pagamento gerada com sucesso!"
        if vencimento:
            texto += f" A data de vencimento é {vencimento}."
        texto += "\n\nEscolha uma das opções para fazer o pagamento:"

        botoes = []
        if resultado.get("link"):
            botoes.append({"id": "link", "title": "Boleto bancário (PDF)"})
        if resultado.get("codigo_de_barras"):
            botoes.append({"id": "codigo_de_barras", "title": "Código de barras"})
        if resultado.get("pix"):
            botoes.append({"id": "pix", "title": "Pix copia-e-cola"})

        interactive = {
            "body": texto,
            "field": "opcao_pagamento",
            "buttons": botoes,
        }
        return texto, interactive

    @staticmethod
    def detalhe_pagamento(resultado: dict[str, Any], opcao: str) -> str:
        """Retorna a mensagem final com o dado de pagamento escolhido."""
        vencimento = resultado.get("data_vencimento", "")
        sufixo = f"\n\nData de vencimento: {vencimento}" if vencimento else ""

        if opcao == "link":
            link = resultado.get("link", "")
            return f"Você pode acessar o documento para pagamento neste link:\n{link}{sufixo}"
        if opcao == "codigo_de_barras":
            codigo = resultado.get("codigo_de_barras", "")
            return f"Código de barras para pagamento:\n{codigo}{sufixo}"
        if opcao == "pix":
            pix = resultado.get("pix", "")
            return f"Código PIX copia-e-cola para pagamento:\n{pix}{sufixo}"
        return "Opção não reconhecida."
