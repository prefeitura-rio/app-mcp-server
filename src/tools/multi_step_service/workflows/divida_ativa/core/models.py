"""
Modelos Pydantic para validação do workflow Dívida Ativa.
"""

import re
from re import Pattern
from typing import ClassVar
from typing import Literal

from pydantic import BaseModel, Field, field_validator


_MASCARA_CPF = re.compile(r"^\d{3}\.?\d{3}\.?\d{3}-?\d{2}$")
_MASCARA_CNPJ = re.compile(r"^\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}$")
_IDENTIFICADOR_GENERICO = re.compile(r"^[0-9./-]+$")
_ANO = re.compile(r"^\d{4}$")
_NUMERICO = re.compile(r"^\d+$")

_OPTION_REGISTRY = {
    "pagar_agora": {
        "label": "Pagar agora",
        "description": "Ver opções disponíveis para pagamento.",
    },
    "consultar_outro_debito": {
        "label": "Consultar outro débito",
        "description": "Limpar a consulta atual e buscar outro débito.",
    },
    "pagar_a_vista": {
        "label": "Pagar à vista",
        "description": "Emitir guia para pagamento integral",
    },
    "parcelar_debitos": {
        "label": "Parcelar débitos",
        "description": "Simular e aderir ao parcelamento",
    },
    "regularizar_debitos": {
        "label": "Regularizar débitos",
        "description": "Ver alternativas para ficar em dia",
    },
    "liquidar_parcelamento": {
        "label": "Liquidar parcelamento",
        "description": "Quitar o que falta do parcelamento",
    },
    "emitir_2_via": {
        "label": "Emitir 2ª via",
        "description": "Gerar segunda via de guia/parcela",
    },
    "voltar": {
        "label": "Voltar",
        "description": "Retornar ao menu Tipos de consulta",
    },
    "pagar_tudo": {
        "label": "Pagar tudo",
        "description": "Emitir guia com todos os débitos disponíveis.",
    },
    "escolher_debitos": {
        "label": "Escolher os débitos",
        "description": "Selecionar quais débitos deseja pagar.",
    },
    "sim": {
        "label": "Sim",
        "description": "Seguir para o pagamento.",
    },
    "nao": {
        "label": "Não",
        "description": "Voltar para as opções de pagamento.",
    },
    "boleto_bancario": {
        "label": "Boleto bancário",
        "description": "Gerar boleto bancário.",
    },
    "codigo_barras": {
        "label": "Código de barras",
        "description": "Receber o código de barras.",
    },
    "pix_copia_e_cola": {
        "label": "Pix copia e cola",
        "description": "Receber o Pix copia e cola.",
    },
    "opcoes_pagamento": {
        "label": "Opções de pagamento",
        "description": "Voltar para as opções de pagamento.",
    },
    "encerrar_atendimento": {
        "label": "Encerrar atendimento",
        "description": "Finalizar o atendimento.",
    },
    "cpf_cnpj": {
        "label": "CPF/CNPJ",
        "description": "Consulte por pessoa física ou jurídica.",
    },
    "inscricao_imobiliaria": {
        "label": "Inscrição Imobiliária",
        "description": "Consulte pelo número da inscrição do imóvel.",
    },
    "auto_infracao": {
        "label": "Auto de infração",
        "description": "Consulte pelo ano e número do auto de infração.",
    },
    "cda": {
        "label": "CDA",
        "description": "Consulte pelo número da Certidão de Dívida Ativa (CDA).",
    },
    "execucao_fiscal": {
        "label": "Execução Fiscal",
        "description": "Consulte processos em execução fiscal (EF).",
    },
}


def _build_options(
    values: list[str],
    overrides: dict[str, dict[str, str]] | None = None,
    include_description: bool = True,
) -> list[dict[str, str]]:
    overrides = overrides or {}
    options = []
    for value in values:
        option = {
            "value": value,
            "label": _OPTION_REGISTRY[value]["label"],
            **overrides.get(value, {}),
        }
        if include_description:
            option["description"] = overrides.get(value, {}).get(
                "description",
                _OPTION_REGISTRY[value]["description"],
            )
        options.append(option)
    return options


def _validate_pattern(
    value: str, patterns: tuple[Pattern[str], ...], message: str
) -> str:
    if any(pattern.fullmatch(value) for pattern in patterns):
        return value
    raise ValueError(message)


class EntradaPayload(BaseModel):
    """Payload para coleta do documento/identificador do contribuinte."""

    _MASCARAS_DOCUMENTO: ClassVar[tuple[Pattern[str], ...]] = (
        _MASCARA_CPF,
        _MASCARA_CNPJ,
        _IDENTIFICADOR_GENERICO,
    )

    entrada: str = Field(
        ...,
        description=(
            "CPF (11 dígitos), CNPJ (14 dígitos), inscrição imobiliária (7 dígitos), "
            "certidão de dívida ativa, execução fiscal ou auto de infração."
        ),
        min_length=1,
    )

    @field_validator("entrada")
    @classmethod
    def validar_entrada(cls, value: str) -> str:
        value = value.strip()
        if not any(char.isdigit() for char in value):
            raise ValueError("Informe um identificador com ao menos um número.")
        return _validate_pattern(
            value,
            cls._MASCARAS_DOCUMENTO,
            "Informe um CPF, CNPJ ou identificador válido.",
        )


class CpfCnpjPayload(BaseModel):
    """Payload para consulta de dívida ativa por CPF ou CNPJ."""

    _MASCARAS_CPF_CNPJ: ClassVar[tuple[Pattern[str], ...]] = (
        _MASCARA_CPF,
        _MASCARA_CNPJ,
    )

    cpf_cnpj: str = Field(
        ...,
        title="CPF/CNPJ",
        description="CPF ou CNPJ do contribuinte, somente números ou com máscara.",
        min_length=11,
        max_length=18,
    )

    @field_validator("cpf_cnpj")
    @classmethod
    def validar_cpf_cnpj(cls, value: str) -> str:
        return _validate_pattern(
            value.strip(),
            cls._MASCARAS_CPF_CNPJ,
            "Informe um CPF ou CNPJ válido, somente números ou com máscara.",
        )


class InscricaoImobiliariaPayload(BaseModel):
    """Payload para consulta de dívida ativa por inscrição imobiliária."""

    inscricao_imobiliaria: str = Field(
        ...,
        title="Inscrição Imobiliária",
        description="Número da inscrição imobiliária do imóvel.",
        min_length=1,
    )

    @field_validator("inscricao_imobiliaria")
    @classmethod
    def validar_inscricao_imobiliaria(cls, value: str) -> str:
        return _validate_pattern(
            value.strip(),
            (_NUMERICO,),
            "Informe uma inscrição imobiliária somente com números.",
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

    @field_validator("ano_auto_infracao")
    @classmethod
    def validar_ano_auto_infracao(cls, value: str) -> str:
        return _validate_pattern(value.strip(), (_ANO,), "Informe um ano válido.")

    @field_validator("numero_auto_infracao")
    @classmethod
    def validar_numero_auto_infracao(cls, value: str) -> str:
        return _validate_pattern(
            value.strip(),
            (_NUMERICO,),
            "Informe o número do auto de infração somente com números.",
        )


class CdaPayload(BaseModel):
    """Payload para consulta de dívida ativa por CDA."""

    cda: str = Field(
        ...,
        title="CDA",
        description="Número da Certidão de Dívida Ativa.",
        min_length=1,
    )

    @field_validator("cda")
    @classmethod
    def validar_cda(cls, value: str) -> str:
        return _validate_pattern(
            value.strip(),
            (_IDENTIFICADOR_GENERICO,),
            "Informe uma CDA válida.",
        )


class ExecucaoFiscalPayload(BaseModel):
    """Payload para consulta de dívida ativa por execução fiscal."""

    execucao_fiscal: str = Field(
        ...,
        title="Execução Fiscal",
        description="Número da execução fiscal.",
        min_length=1,
    )

    @field_validator("execucao_fiscal")
    @classmethod
    def validar_execucao_fiscal(cls, value: str) -> str:
        return _validate_pattern(
            value.strip(),
            (_IDENTIFICADOR_GENERICO,),
            "Informe uma execução fiscal válida.",
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
            "options": _build_options(
                ["pagar_agora", "consultar_outro_debito"],
                include_description=False,
            ),
            "x-render": "buttons",
        },
    )


class MenuPagamentoCompletoPayload(BaseModel):
    """Lista para dívida com débitos não parcelados e parcelados."""

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
            "options": _build_options(
                [
                    "pagar_a_vista",
                    "parcelar_debitos",
                    "regularizar_debitos",
                    "liquidar_parcelamento",
                    "emitir_2_via",
                    "voltar",
                ],
                include_description=False,
            ),
            "x-render": "list",
        },
    )


class MenuPagamentoParceladoPayload(BaseModel):
    """Lista para dívida apenas com guias parceladas."""

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
            "options": _build_options(
                [
                    "parcelar_debitos",
                    "regularizar_debitos",
                    "liquidar_parcelamento",
                    "emitir_2_via",
                    "voltar",
                ],
                include_description=False,
            ),
            "x-render": "list",
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
            "options": _build_options(
                ["pagar_tudo", "escolher_debitos"],
                include_description=False,
            ),
            "x-render": "buttons",
        },
    )


class DebitosEscolhidosPayload(BaseModel):
    """Payload para seleção manual de débitos no pagamento à vista."""

    debitos_escolhidos: str = Field(
        ...,
        title="Débitos escolhidos",
        description=(
            "Números dos débitos escolhidos, separados por vírgula. Exemplo: 1, 2, 4."
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
            "options": _build_options(
                ["sim", "nao"],
                include_description=False,
            ),
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
            "options": _build_options(
                ["boleto_bancario", "codigo_barras", "pix_copia_e_cola"],
                include_description=False,
            ),
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
            "options": _build_options(
                ["escolher_debitos", "opcoes_pagamento", "encerrar_atendimento"],
                include_description=False,
            ),
            "x-render": "buttons",
        },
    )


class TipoConsultaPayload(BaseModel):
    """Payload para escolha do tipo de consulta em lista."""

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
            "options": _build_options(
                [
                    "cpf_cnpj",
                    "inscricao_imobiliaria",
                    "auto_infracao",
                    "cda",
                    "execucao_fiscal",
                ],
                include_description=False,
            ),
            "x-render": "list",
        },
    )
