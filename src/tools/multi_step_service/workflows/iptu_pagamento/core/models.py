"""
Modelos Pydantic para validação do workflow IPTU Ano Vigente
"""

from typing import Literal, Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field, field_validator, ConfigDict
import re


class InscricaoImobiliariaPayload(BaseModel):
    """Payload para coleta da inscrição imobiliária."""

    inscricao_imobiliaria: str = Field(
        ...,
        description="Inscrição imobiliária do imóvel.",
        min_length=1,
        max_length=15,
    )

    @field_validator(
        "inscricao_imobiliaria",
        mode="before",
    )
    @classmethod
    def validate_inscricao(cls, v: str) -> str:
        """
        Valida e sanitiza a inscrição imobiliária.

        Remove caracteres não numéricos e valida comprimento.
        """
        # Remove todos os caracteres não numéricos
        clean_inscricao = re.sub(r"[^0-9]", "", v)

        if len(clean_inscricao) < 8:
            clean_inscricao = clean_inscricao.zfill(8)

        if len(clean_inscricao) > 15:
            raise ValueError("Inscrição imobiliária não pode ter mais de 15 dígitos")

        return clean_inscricao


class EscolhaAnoPayload(BaseModel):
    """Payload para escolha do ano de exercício."""

    ano_exercicio: Union[int, str] = Field(
        ..., description="Ano de exercício para consulta do IPTU"
    )

    @field_validator("ano_exercicio", mode="before")
    @classmethod
    def validate_ano_exercicio(cls, v: Union[int, str]) -> int:
        """Valida o ano de exercício."""
        ano_clean = int(v) if isinstance(v, str) else v
        if ano_clean < 2000 or ano_clean > 2100:
            raise ValueError("Ano de exercício inválido")
        return ano_clean


class EscolhaGuiasIPTUPayload(BaseModel):
    """Payload para escolher qual guia de IPTU o usuário quer pagar."""

    guia_escolhida: str = Field(
        ..., description="Número da guia escolhida para pagamento (ex: '00', '01')"
    )


class EscolhaCotasParceladasPayload(BaseModel):
    """Payload para escolher quais cotas parceladas pagar."""

    cotas_escolhidas: List[str] = Field(
        ..., description="Lista das cotas escolhidas para pagamento"
    )


class ConfirmacaoDadosPayload(BaseModel):
    """Payload para confirmação dos dados coletados."""

    confirmacao: bool = Field(..., description="Confirmação se os dados estão corretos")


# Modelos de dados para estruturas internas


class Guia(BaseModel):
    """Dados completos de uma guia conforme retornado pela API ConsultarGuias."""

    # Campos retornados pela API
    situacao: Dict = Field(
        alias="Situacao"
    )  # {codigo: "01|02", descricao: "EM ABERTO|QUITADA"}
    inscricao: str = Field(alias="Inscricao")
    exercicio: str = Field(alias="Exercicio")
    numero_guia: str = Field(alias="NGuia")
    tipo: str = Field(alias="Tipo")  # "ORDINÁRIA" ou "EXTRAORDINÁRIA"
    valor_iptu_original_guia: str = Field(
        alias="ValorIPTUOriginalGuia"
    )  # Formato brasileiro "2.878,00"
    data_vencto_desc_cota_unica: str = Field(
        alias="DataVenctoDescCotaUnica"
    )  # Formato "07/02/2024" ou ""
    quant_dias_em_atraso: str = Field(alias="QuantDiasEmAtraso")  # "1390"
    percentual_desc_cota_unica: str = Field(alias="PercentualDescCotaUnica")  # "00007"
    valor_iptu_desconto_avista: str = Field(
        alias="ValorIPTUDescontoAvista"
    )  # Formato brasileiro "0,00"
    valor_parcelas: str = Field(alias="ValorParcelas")  # Formato brasileiro "86,00"
    credito_nota_carioca: str = Field(
        alias="CreditoNotaCarioca"
    )  # Formato brasileiro "0,00"
    credito_decad: str = Field(alias="CreditoDECAD")  # Formato brasileiro "0,00"
    credito_isencao: str = Field(alias="CreditoIsencao")  # Formato brasileiro "0,00"
    credito_cota_unica: str = Field(
        alias="CreditoCotaUnica"
    )  # Formato brasileiro "201,46"
    valor_quitado: str = Field(alias="ValorQuitado")  # Formato brasileiro "2.676,54"
    data_quitacao: str = Field(alias="DataQuitacao")  # Formato "28/01/2021" ou ""
    deposito: str = Field(alias="Deposito")  # "N" ou "S"

    # Campos calculados/processados localmente
    valor_numerico: Optional[float] = None
    valor_desconto_numerico: Optional[float] = None
    valor_parcelas_numerico: Optional[float] = None
    esta_quitada: Optional[bool] = None
    esta_em_aberto: Optional[bool] = None

    model_config = ConfigDict(validate_by_name=True)


class DadosGuias(BaseModel):
    """Dados das guias consultadas."""

    inscricao_imobiliaria: str
    exercicio: str
    guias: List[Guia] = []
    total_guias: int = 0


class Cota(BaseModel):
    """Dados completos de uma cota conforme retornado pela API ConsultarCotas."""

    # Campos retornados pela API
    situacao: Dict = Field(
        alias="Situacao"
    )  # {codigo: "01|02|03", descricao: "PAGA|EM ABERTO|VENCIDA"}
    numero_cota: str = Field(alias="NCota")
    valor_cota: str = Field(alias="ValorCota")  # Formato brasileiro "89,44"
    data_vencimento: str = Field(alias="DataVencimento")  # Formato "07/11/2024"
    valor_pago: str = Field(alias="ValorPago")  # Formato brasileiro "0,00"
    data_pagamento: str = Field(alias="DataPagamento")  # Pode estar vazio ""
    quantidade_dias_atraso: str = Field(alias="QuantDiasEmAtraso")

    # Campos calculados/processados localmente
    valor_numerico: Optional[float] = None
    valor_pago_numerico: Optional[float] = None
    dias_atraso_numerico: Optional[int] = None
    esta_paga: Optional[bool] = None
    esta_vencida: Optional[bool] = None
    codigo_barras: Optional[str] = None
    linha_digitavel: Optional[str] = None
    darf_data: Optional[dict] = None

    model_config = ConfigDict(validate_by_name=True)


class DadosCotas(BaseModel):
    """Dados das cotas disponíveis para uma guia específica."""

    inscricao_imobiliaria: str
    exercicio: str
    numero_guia: str
    tipo_guia: str
    cotas: List[Cota] = []
    total_cotas: int = 0
    valor_total: float = 0.0


class CotaDarm(BaseModel):
    """Cota dentro do DARM."""

    ncota: str = Field(alias="ncota")
    valor: str = Field(alias="valor")  # Formato brasileiro "89,44"

    model_config = ConfigDict(validate_by_name=True)


class Darm(BaseModel):
    """Dados completos de um DARM conforme retornado pela API ConsultarDARM."""

    # Campos retornados pela API
    cotas: List[CotaDarm] = Field(alias="Cotas")
    inscricao: str = Field(alias="Inscricao")
    exercicio: str = Field(alias="Exercicio")
    numero_guia: str = Field(alias="NGuia")
    tipo: str = Field(alias="Tipo")  # "ORDINÁRIA" ou "EXTRAORDINÁRIA"
    data_vencimento: str = Field(alias="DataVencimento")  # Formato "29/11/2024"
    valor_iptu_original: str = Field(
        alias="ValorIPTUOriginal"
    )  # Formato brasileiro "860,00"
    valor_darm: str = Field(alias="ValorDARM")  # Formato brasileiro "261,44"
    valor_desc_cota_unica: str = Field(
        alias="ValorDescCotaUnica"
    )  # Formato brasileiro "0,00"
    credito_nota_carioca: str = Field(
        alias="CreditoNotaCarioca"
    )  # Formato brasileiro "0,00"
    credito_decad: str = Field(alias="CreditoDECAD")  # Formato brasileiro "0,00"
    credito_isencao: str = Field(alias="CreditoIsencao")  # Formato brasileiro "0,00"
    credito_emissao: str = Field(alias="CreditoEmissao")  # Formato brasileiro "0,00"
    valor_a_pagar: str = Field(alias="ValorAPagar")  # Formato brasileiro "261,44"
    sequencia_numerica: str = Field(alias="SequenciaNumerica")  # Linha digitável
    descricao_darm: str = Field(
        alias="DescricaoDARM"
    )  # "DARM por cota ref.cotas 01,02,03"
    cod_receita: str = Field(alias="CodReceita")  # "310-7"
    des_receita: str = Field(alias="DesReceita")  # "RECEITA DE PAGAMENTO"
    endereco: Optional[str] = Field(alias="Endereco")  # Pode ser null
    nome: Optional[str] = Field(alias="Nome")  # Pode ser null

    # Campos calculados/processados localmente
    valor_numerico: Optional[float] = None
    codigo_barras: Optional[str] = None  # Derivado da sequencia_numerica

    model_config = ConfigDict(validate_by_name=True)


class DadosDarm(BaseModel):
    """Dados do DARM consultado."""

    inscricao_imobiliaria: str
    exercicio: str
    numero_guia: str
    cotas_selecionadas: List[str]
    darm: Optional[Darm] = None
    pdf_base64: Optional[str] = None


class EscolhaGuiaMesmoImovelPayload(BaseModel):
    """Payload para pergunta sobre gerar mais guias para o mesmo imóvel."""

    mesma_guia: bool = Field(
        ..., description="Se deseja emitir mais guias para o mesmo imóvel"
    )


class EscolhaFormatoDarmPayload(BaseModel):
    """Payload para escolha do formato de geração de boletos (único ou separado)."""

    darm_separado: bool = Field(
        ...,
        description="True para gerar um boleto para cada cota, False para boleto único com todas as cotas",
    )


class DadosConsulta(BaseModel):
    """Dados completos da consulta de IPTU."""

    dados_guias: DadosGuias
    guia_escolhida: Optional[str] = None
    dados_cotas: Optional[DadosCotas] = None
    dados_darm: Optional[DadosDarm] = None
    tipo_cobranca_escolhido: Optional[str] = None
    formato_pagamento_escolhido: Optional[str] = None


class CDA(BaseModel):
    """Certidão de Dívida Ativa."""

    # Campos antigos (mantidos para compatibilidade)
    numero: Optional[str] = None
    exercicio: Optional[str] = None
    valor_original: Optional[str] = None
    data_inscricao: Optional[str] = None
    situacao: Optional[str] = None

    # Campos novos da API
    cda_id: Optional[str] = None
    num_exercicio: Optional[str] = None
    valor_saldo_total: Optional[str] = None
    valor_saldo_principal: Optional[str] = None
    situacao_principal: Optional[str] = None
    natureza_divida: Optional[str] = None
    fase_cobranca: Optional[str] = None
    nome_contribuinte: Optional[str] = None
    cpf_cnpj: Optional[str] = None
    guia: Optional[str] = None


class EF(BaseModel):
    """Execução Fiscal."""

    # Campos antigos (mantidos para compatibilidade)
    numero_ef: Optional[str] = None
    numero_processo: Optional[str] = None
    valor_original: Optional[str] = None
    data_ajuizamento: Optional[str] = None
    situacao: Optional[str] = None

    # Campos novos da API
    numero_execucao_fiscal: Optional[str] = None
    saldo_execucao_fiscal_nao_parcelada: Optional[str] = None


class Parcelamento(BaseModel):
    """Parcelamento de dívida ativa."""

    numero: Optional[str] = None
    qtde_parcelas: Optional[str] = None
    qtd_pagas: Optional[str] = None
    data_ultimo_pagamento: Optional[str] = None
    nome_requerente: Optional[str] = None
    descricao_tipo_pagamento: Optional[str] = None
    descricao_situacao_guia: Optional[str] = None
    valor_total_guia: Optional[str] = None


class DadosDividaAtiva(BaseModel):
    """Informações sobre dívida ativa retornadas pela API."""

    tem_divida_ativa: bool = False
    data_vencimento: Optional[str] = None
    saldo_total_divida: str = "R$0,00"
    saldo_total_nao_parcelado: str = "R$0,00"
    saldo_total_parcelado: str = "R$0,00"
    endereco_imovel: Optional[str] = None
    bairro_imovel: Optional[str] = None
    tem_pdf: bool = False  # Mudado de pdf (base64 string) para boolean
    url_pdf: Optional[str] = None
    cdas: List[CDA] = []
    efs: List[EF] = []
    parcelamentos: List[Parcelamento] = []

    @classmethod
    def from_api_response(cls, response: Dict[str, Any]) -> "DadosDividaAtiva":
        """
        Cria DadosDividaAtiva a partir da resposta da API.

        Args:
            response: Resposta da API de dívida ativa

        Returns:
            DadosDividaAtiva com dados processados
        """
        if not response or not response.get("success"):
            return cls(tem_divida_ativa=False)

        data = response.get("data", {})

        # Extrai débitos não parcelados
        debitos_nao_parcelados = data.get("debitosNaoParceladosComSaldoTotal", {})
        cdas_raw = debitos_nao_parcelados.get("cdasNaoAjuizadasNaoParceladas", [])
        efs_raw = debitos_nao_parcelados.get("efsNaoParceladas", [])
        saldo_nao_parcelado = debitos_nao_parcelados.get(
            "saldoTotalNaoParcelado", "R$0,00"
        )

        # Extrai parcelamentos
        guias_parceladas_info = data.get("guiasParceladasComSaldoTotal", {})
        parcelamentos_raw = guias_parceladas_info.get("guiasParceladas", [])
        saldo_parcelado = guias_parceladas_info.get("saldoTotalParcelado", "R$0,00")

        # Processa CDAs
        cdas = []
        for cda_data in cdas_raw:
            # Tenta primeiro o formato novo, depois fallback para o antigo
            cdas.append(
                CDA(
                    # Formato antigo (compatibilidade)
                    numero=cda_data.get("numero") or cda_data.get("cdaId"),
                    exercicio=cda_data.get("exercicio") or cda_data.get("numExercicio"),
                    valor_original=cda_data.get("valorOriginal")
                    or cda_data.get("valorSaldoTotal"),
                    data_inscricao=cda_data.get("dataInscricao"),
                    situacao=cda_data.get("situacao")
                    or cda_data.get("situacaoPrincipal"),
                    # Formato novo
                    cda_id=cda_data.get("cdaId"),
                    num_exercicio=cda_data.get("numExercicio"),
                    valor_saldo_total=cda_data.get("valorSaldoTotal"),
                    valor_saldo_principal=cda_data.get("valorSaldoPrincipal"),
                    situacao_principal=cda_data.get("situacaoPrincipal"),
                    natureza_divida=cda_data.get("naturezaDivida"),
                    fase_cobranca=cda_data.get("faseCobranca"),
                    nome_contribuinte=cda_data.get("nomeContribuinte"),
                    cpf_cnpj=cda_data.get("cpf_Cnpj"),
                    guia=cda_data.get("guia"),
                )
            )

        # Processa EFs
        efs = []
        for ef_data in efs_raw:
            # Tenta primeiro o formato novo, depois fallback para o antigo
            efs.append(
                EF(
                    # Formato antigo (compatibilidade)
                    numero_ef=ef_data.get("numeroEF")
                    or ef_data.get("numeroExecucaoFiscal"),
                    numero_processo=ef_data.get("numeroProcesso")
                    or ef_data.get("numeroExecucaoFiscal"),
                    valor_original=ef_data.get("valorOriginal")
                    or ef_data.get("saldoExecucaoFiscalNaoParcelada"),
                    data_ajuizamento=ef_data.get("dataAjuizamento"),
                    situacao=ef_data.get("situacao"),
                    # Formato novo
                    numero_execucao_fiscal=ef_data.get("numeroExecucaoFiscal"),
                    saldo_execucao_fiscal_nao_parcelada=ef_data.get(
                        "saldoExecucaoFiscalNaoParcelada"
                    ),
                )
            )

        # Processa Parcelamentos
        parcelamentos = []
        for parc_data in parcelamentos_raw:
            parcelamentos.append(
                Parcelamento(
                    numero=parc_data.get("numero"),
                    qtde_parcelas=parc_data.get("qtdeParcelas"),
                    qtd_pagas=parc_data.get("qtdPagas"),
                    data_ultimo_pagamento=parc_data.get("dataUltimoPagamento"),
                    nome_requerente=parc_data.get("nomeRequerente"),
                    descricao_tipo_pagamento=parc_data.get("descricaoTipoPagamento"),
                    descricao_situacao_guia=parc_data.get("descricaoSituacaoGuia"),
                    valor_total_guia=parc_data.get("valorTotalGuia"),
                )
            )

        # Verifica se tem algum débito
        tem_divida = bool(cdas or efs or parcelamentos)

        # Verifica se tem PDF disponível (boolean)
        pdf_base64 = data.get("pdf")
        tem_pdf = bool(pdf_base64 and len(pdf_base64) > 0)

        return cls(
            tem_divida_ativa=tem_divida,
            data_vencimento=data.get("dataVencimento"),
            saldo_total_divida=data.get("saldoTotalDivida", "R$0,00"),
            saldo_total_nao_parcelado=saldo_nao_parcelado,
            saldo_total_parcelado=saldo_parcelado,
            endereco_imovel=data.get("enderecoImovel"),
            bairro_imovel=data.get("bairroImovel"),
            tem_pdf=tem_pdf,
            url_pdf=data.get("urlPdf"),
            cdas=cdas,
            efs=efs,
            parcelamentos=parcelamentos,
        )
