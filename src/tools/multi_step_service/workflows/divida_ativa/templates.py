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
            "Para requerer o parcelamento de seus débitos, acesse o serviço "
            "“Parcelamento em dívida ativa” no link abaixo:\n\n"
            "https://carioca.rio/servicos/parcelamento-em-divida-ativa/"
        )

    @staticmethod
    def link_liquidacao() -> str:
        return (
            "Para liquidar suas guias de parcelamento, acesse o link:\n\n"
            "https://daminternet.rio.rj.gov.br/GuiaPagamento/liquidacao"
        )

    @staticmethod
    def link_segunda_via() -> str:
        return (
            "Para emitir 2ª via de suas guias de pagamento, acesse o link:\n\n"
            "https://daminternet.rio.rj.gov.br/GuiaPagamento/EmitirSegundaVia"
        )

    @staticmethod
    def solicitar_itens(acao: str) -> str:
        if acao == "pagar_a_vista":
            return (
                "Se deseja liquidar todas as dívidas não parceladas, responda TODAS.\n"
                "Se deseja pagar à vista algum débito específico, informe os "
                "sequenciais associados à certidão ou à execução fiscal, separados "
                "por vírgula.\n"
                "Exemplo: 1, 2, 4."
            )
        return (
            "Se deseja regularizar todas as parcelas em atraso, responda TODAS.\n"
            "Se deseja regularizar algum parcelamento específico, informe o "
            "sequencial relacionado à parcela para a qual deseja emitir a guia de "
            "regularização, separando por vírgula."
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
    def guia_emitida(resultado: dict[str, Any]) -> str:
        texto = "Guia gerada com sucesso."
        if resultado.get("link"):
            texto += f"\n\nLink: {resultado['link']}"
        if resultado.get("codigo_de_barras"):
            texto += f"\nCódigo de barras: {resultado['codigo_de_barras']}"
        if resultado.get("pix"):
            texto += f"\nPIX: {resultado['pix']}"
        if resultado.get("data_vencimento"):
            texto += f"\nData de vencimento: {resultado['data_vencimento']}"
        return texto
