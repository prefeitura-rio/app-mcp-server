"""
Modelos Pydantic para validação do workflow Dívida Ativa.
"""

from typing import Literal

from pydantic import BaseModel, Field


class EntradaPayload(BaseModel):
    """Payload para coleta do documento/identificador do contribuinte."""

    entrada: str = Field(
        ...,
        description=(
            "CPF (11 dígitos), CNPJ (14 dígitos), inscrição imobiliária (7 dígitos), "
            "certidão de dívida ativa, execução fiscal ou auto de infração."
        ),
        min_length=1,
    )


class CpfCnpjPayload(BaseModel):
    """Payload para consulta de dívida ativa por CPF ou CNPJ."""

    cpf_cnpj: str = Field(
        ...,
        title="CPF/CNPJ",
        description="CPF ou CNPJ do contribuinte, somente números ou com máscara.",
        min_length=11,
        max_length=18,
    )


class InscricaoImobiliariaPayload(BaseModel):
    """Payload para consulta de dívida ativa por inscrição imobiliária."""

    inscricao_imobiliaria: str = Field(
        ...,
        title="Inscrição Imobiliária",
        description="Número da inscrição imobiliária do imóvel.",
        min_length=1,
    )


class AutoInfracaoPayload(BaseModel):
    """Payload para consulta de dívida ativa por auto de infração."""

    ano_auto_infracao: str = Field(
        ...,
        title="Ano do Auto de Infração",
        description="Ano do auto de infração.",
        min_length=4,
        max_length=4,
    )
    numero_auto_infracao: str = Field(
        ...,
        title="Número do Auto de Infração",
        description="Número do auto de infração.",
        min_length=1,
    )


class CdaPayload(BaseModel):
    """Payload para consulta de dívida ativa por CDA."""

    cda: str = Field(
        ...,
        title="CDA",
        description="Número da Certidão de Dívida Ativa.",
        min_length=1,
    )


class ExecucaoFiscalPayload(BaseModel):
    """Payload para consulta de dívida ativa por execução fiscal."""

    execucao_fiscal: str = Field(
        ...,
        title="Execução Fiscal",
        description="Número da execução fiscal.",
        min_length=1,
    )


class AcaoResultadoConsultaPayload(BaseModel):
    """Payload para ação após exibir o resultado da consulta."""

    acao_resultado: Literal[
        "pagar_agora",
        "consultar_outro_debito",
    ] = Field(
        ...,
        title="Próxima ação",
        description="Escolha como quer continuar.",
        json_schema_extra={
            "options": [
                {
                    "value": "pagar_agora",
                    "label": "Pagar agora",
                    "description": "Ver opções disponíveis para pagamento.",
                },
                {
                    "value": "consultar_outro_debito",
                    "label": "Consultar outro débito",
                    "description": "Limpar a consulta atual e buscar outro débito.",
                },
            ],
            "x-render": "buttons",
        },
    )


class MenuPagamentoCompletoPayload(BaseModel):
    """WhatsApp Flow para dívida com débitos não parcelados e parcelados."""

    opcao_menu: Literal[
        "pagar_a_vista",
        "parcelar_debitos",
        "regularizar_debitos",
        "liquidar_parcelamento",
        "emitir_2_via",
        "voltar",
    ] = Field(
        ...,
        title="Opções de pagamento",
        description="Escolha uma opção",
        json_schema_extra={
            "options": [
                {
                    "value": "pagar_a_vista",
                    "label": "Pagar à vista",
                    "description": "Emitir guia para pagamento integral",
                },
                {
                    "value": "parcelar_debitos",
                    "label": "Parcelar débitos",
                    "description": "Simular e aderir ao parcelamento",
                },
                {
                    "value": "regularizar_debitos",
                    "label": "Regularizar débitos",
                    "description": "Ver alternativas para ficar em dia",
                },
                {
                    "value": "liquidar_parcelamento",
                    "label": "Liquidar parcelamento",
                    "description": "Quitar o que falta do parcelamento",
                },
                {
                    "value": "emitir_2_via",
                    "label": "Emitir 2ª via",
                    "description": "Gerar segunda via de guia/parcela",
                },
                {
                    "value": "voltar",
                    "label": "Voltar",
                    "description": "Retornar ao menu Tipos de consulta",
                },
            ],
            "x-render": "whatsapp_flow",
        },
    )


class MenuPagamentoParceladoPayload(BaseModel):
    """WhatsApp Flow para dívida apenas com guias parceladas."""

    opcao_menu: Literal[
        "parcelar_debitos",
        "regularizar_debitos",
        "liquidar_parcelamento",
        "emitir_2_via",
        "voltar",
    ] = Field(
        ...,
        title="Opções de pagamento",
        description="Escolha uma opção",
        json_schema_extra={
            "options": [
                {
                    "value": "parcelar_debitos",
                    "label": "Parcelar débitos",
                    "description": "Simular e aderir ao parcelamento",
                },
                {
                    "value": "regularizar_debitos",
                    "label": "Regularizar débitos",
                    "description": "Ver alternativas para ficar em dia",
                },
                {
                    "value": "liquidar_parcelamento",
                    "label": "Liquidar parcelamento",
                    "description": "Quitar o que falta do parcelamento",
                },
                {
                    "value": "emitir_2_via",
                    "label": "Emitir 2ª via",
                    "description": "Gerar segunda via de guia/parcela",
                },
                {
                    "value": "voltar",
                    "label": "Voltar",
                    "description": "Retornar ao menu Tipos de consulta",
                },
            ],
            "x-render": "whatsapp_flow",
        },
    )


class OpcaoPagarAVistaPayload(BaseModel):
    """Botões para escolher como pagar débitos à vista."""

    opcao_pagar_a_vista: Literal[
        "pagar_tudo",
        "escolher_debitos",
    ] = Field(
        ...,
        title="Pagamento à vista",
        description="Escolha como quer pagar à vista.",
        json_schema_extra={
            "options": [
                {
                    "value": "pagar_tudo",
                    "label": "Pagar tudo",
                    "description": "Emitir guia com todos os débitos disponíveis.",
                },
                {
                    "value": "escolher_debitos",
                    "label": "Escolher os débitos",
                    "description": "Selecionar quais débitos deseja pagar.",
                },
            ],
            "x-render": "buttons",
        },
    )


class DebitosEscolhidosPayload(BaseModel):
    """Payload para seleção manual de débitos no pagamento à vista."""

    debitos_escolhidos: str = Field(
        ...,
        title="Débitos escolhidos",
        description=(
            "Números dos débitos escolhidos, separados por vírgula. "
            "Exemplo: 1, 2, 4."
        ),
        min_length=1,
    )


class ConfirmacaoPagamentoAVistaPayload(BaseModel):
    """Botões para confirmar pagamento à vista dos débitos escolhidos."""

    confirmar_pagamento_a_vista: Literal[
        "sim",
        "nao",
    ] = Field(
        ...,
        title="Confirmar pagamento",
        description="Confirme se deseja seguir para o pagamento.",
        json_schema_extra={
            "options": [
                {
                    "value": "sim",
                    "label": "Sim",
                    "description": "Seguir para o pagamento.",
                },
                {
                    "value": "nao",
                    "label": "Não",
                    "description": "Voltar para as opções de pagamento.",
                },
            ],
            "x-render": "buttons",
        },
    )


class FormaPagamentoAVistaPayload(BaseModel):
    """Botões para escolher a forma de pagamento à vista."""

    forma_pagamento_a_vista: Literal[
        "boleto_bancario",
        "codigo_barras",
        "pix_copia_e_cola",
    ] = Field(
        ...,
        title="Forma de pagamento",
        description="Escolha uma forma de pagamento à vista.",
        json_schema_extra={
            "options": [
                {
                    "value": "boleto_bancario",
                    "label": "Boleto bancário",
                    "description": "Gerar boleto bancário.",
                },
                {
                    "value": "codigo_barras",
                    "label": "Código de barras",
                    "description": "Receber o código de barras.",
                },
                {
                    "value": "pix_copia_e_cola",
                    "label": "Pix copia e cola",
                    "description": "Receber o Pix copia e cola.",
                },
            ],
            "x-render": "buttons",
        },
    )


class AcaoPagamentoRecusadoPayload(BaseModel):
    """Botões exibidos quando o usuário não confirma o pagamento à vista."""

    acao_pagamento_recusado: Literal[
        "escolher_debitos",
        "opcoes_pagamento",
        "encerrar_atendimento",
    ] = Field(
        ...,
        title="Próxima ação",
        description="Escolha como deseja continuar.",
        json_schema_extra={
            "options": [
                {
                    "value": "escolher_debitos",
                    "label": "Escolher os débitos",
                    "description": "Selecionar novamente os débitos à vista.",
                },
                {
                    "value": "opcoes_pagamento",
                    "label": "Opções de pagamento",
                    "description": "Voltar para as opções de pagamento.",
                },
                {
                    "value": "encerrar_atendimento",
                    "label": "Encerrar atendimento",
                    "description": "Finalizar o atendimento.",
                },
            ],
            "x-render": "buttons",
        },
    )


class TipoConsultaPayload(BaseModel):
    """Payload para escolha do tipo de consulta no WhatsApp Flow."""

    tipo_consulta: Literal[
        "cpf_cnpj",
        "inscricao_imobiliaria",
        "auto_infracao",
        "cda",
        "execucao_fiscal",
    ] = Field(
        ...,
        title="Tipos de consulta",
        description="Escolha uma opção",
        json_schema_extra={
            "options": [
                {
                    "value": "cpf_cnpj",
                    "label": "CPF/CNPJ",
                    "description": "Consulte por pessoa física ou jurídica.",
                },
                {
                    "value": "inscricao_imobiliaria",
                    "label": "Inscrição Imobiliária",
                    "description": "Consulte pelo número da inscrição do imóvel.",
                },
                {
                    "value": "auto_infracao",
                    "label": "Auto de infração",
                    "description": "Consulte pelo ano e número do auto de infração.",
                },
                {
                    "value": "cda",
                    "label": "CDA",
                    "description": "Consulte pelo número da Certidão de Dívida Ativa (CDA).",
                },
                {
                    "value": "execucao_fiscal",
                    "label": "Execução Fiscal",
                    "description": "Consulte processos em execução fiscal (EF).",
                },
            ]
        },
    )
