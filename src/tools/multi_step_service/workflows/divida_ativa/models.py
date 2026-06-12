from typing import Any, Literal, Optional
import re
from difflib import SequenceMatcher

from pydantic import BaseModel, Field, field_validator


TipoConsulta = Literal[
    "inscricaoImobiliaria",
    "cda",
    "cpfCnpj",
    "numeroExecucaoFiscal",
    "numeroAutoInfracao",
]

AcaoDebitos = Literal[
    "pagar_a_vista",
    "parcelar_debitos",
    "regularizar_debitos",
    "liquidar_parcelamento",
    "emitir_segunda_via",
]


def calcular_similaridade(texto1: str, texto2: str) -> float:
    """Calcula similaridade entre dois textos (0.0 a 1.0)."""
    return SequenceMatcher(None, texto1.lower(), texto2.lower()).ratio()


def encontrar_mais_similar(entrada: str, opcoes: dict[str, list[str]]) -> str:
    """
    Encontra a opção mais similar baseado em descrições.

    Args:
        entrada: Texto do usuário
        opcoes: Dict com {valor_retorno: [descrições/sinônimos]}

    Returns:
        A chave mais similar
    """
    entrada_lower = entrada.lower().strip()
    melhor_score = 0.0
    melhor_opcao = None

    for opcao, descricoes in opcoes.items():
        for descricao in descricoes:
            score = calcular_similaridade(entrada_lower, descricao)
            if score > melhor_score:
                melhor_score = score
                melhor_opcao = opcao

    # Se a similaridade for muito baixa, retorna a entrada original
    # para o Pydantic validar
    return melhor_opcao if melhor_score > 0.3 else entrada


class TipoConsultaPayload(BaseModel):
    consulta_debitos: TipoConsulta = Field(
        ...,
        description=(
            "Tipo de consulta: inscricaoImobiliaria, cda, cpfCnpj, "
            "numeroExecucaoFiscal ou numeroAutoInfracao."
        ),
    )

    @field_validator("consulta_debitos", mode="before")
    @classmethod
    def normalizar_tipo_consulta(cls, value: Any) -> str:
        raw = str(value or "").strip()

        # Descrições semânticas de cada tipo de consulta
        opcoes = {
            "inscricaoImobiliaria": [
                "1",
                "inscricao imobiliaria",
                "codigo do imovel",
                "inscricao",
                "codigo",
                "imovel",
                "imobiliaria",
                "pelo codigo",
                "codigo de inscricao",
            ],
            "cda": [
                "2",
                "cda",
                "certidao",
                "certidao de divida ativa",
                "divida ativa",
                "numero da certidao",
                "certidao da divida",
                "pelo numero da certidao",
            ],
            "cpfCnpj": [
                "3",
                "cpf",
                "cnpj",
                "cpf cnpj",
                "cpf/cnpj",
                "documento",
                "contribuinte",
                "pelo meu cpf",
                "pelo cnpj",
                "documento do contribuinte",
            ],
            "numeroExecucaoFiscal": [
                "4",
                "execucao fiscal",
                "execucao",
                "ef",
                "numero da execucao",
                "pela execucao fiscal",
                "numero de execucao",
            ],
            "numeroAutoInfracao": [
                "5",
                "auto de infracao",
                "auto",
                "infracao",
                "multa",
                "auto infracao",
                "numero do auto",
                "pelo auto de infracao",
            ],
        }

        return encontrar_mais_similar(raw, opcoes)


class AnoAutoInfracaoPayload(BaseModel):
    anoAutoInfracao: str = Field(..., description="Ano do Auto de Infração.")

    @field_validator("anoAutoInfracao", mode="before")
    @classmethod
    def validar_ano(cls, value: Any) -> str:
        ano = re.sub(r"\D", "", str(value or ""))
        if len(ano) != 4:
            raise ValueError("Informe o ano do Auto de Infração com 4 dígitos.")
        return ano


class ValorConsultaPayload(BaseModel):
    inscricaoImobiliaria: Optional[str] = Field(
        default=None, description="Código de inscrição imobiliária."
    )
    cda: Optional[str] = Field(
        default=None, description="Código da Certidão de Dívida Ativa."
    )
    cpfCnpj: Optional[str] = Field(
        default=None,
        description=(
            "CPF ou CNPJ do contribuinte. CNPJ pode conter letras, conforme a nova regra."
        ),
    )
    numeroExecucaoFiscal: Optional[str] = Field(
        default=None, description="Número da Execução Fiscal."
    )
    numeroAutoInfracao: Optional[str] = Field(
        default=None, description="Número do Auto de Infração."
    )

    @field_validator(
        "inscricaoImobiliaria",
        "cda",
        "numeroExecucaoFiscal",
        "numeroAutoInfracao",
        mode="before",
    )
    @classmethod
    def somente_digitos(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        clean = re.sub(r"\D", "", str(value))
        if not clean:
            raise ValueError("Informe um valor numérico válido.")
        return clean

    @field_validator("cpfCnpj", mode="before")
    @classmethod
    def limpar_cpf_cnpj(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        clean = re.sub(r"[^0-9A-Za-z]", "", str(value)).upper()
        if not clean:
            raise ValueError("Informe um CPF/CNPJ válido.")
        if not clean.isalnum():
            raise ValueError("CPF/CNPJ deve conter apenas letras e números.")
        return clean


class AcaoDebitosPayload(BaseModel):
    acao: AcaoDebitos = Field(
        ...,
        description=(
            "Ação desejada: pagar_a_vista, parcelar_debitos, regularizar_debitos, "
            "liquidar_parcelamento ou emitir_segunda_via."
        ),
    )

    @field_validator("acao", mode="before")
    @classmethod
    def normalizar_acao(cls, value: Any) -> str:
        raw = str(value or "").strip()

        # Descrições semânticas de cada ação
        opcoes = {
            "pagar_a_vista": [
                "1",
                "pagar a vista",
                "pagar à vista",
                "pagamento a vista",
                "pagar tudo",
                "quitar total",
                "pagar",
                "vista",
                "pagamento total",
                "quero pagar",
            ],
            "parcelar_debitos": [
                "2",
                "parcelar",
                "parcelar debitos",
                "parcelamento",
                "dividir",
                "dividir em parcelas",
                "quero parcelar",
                "fazer parcelamento",
                "parcelas",
            ],
            "regularizar_debitos": [
                "3",
                "regularizar",
                "regularizar debitos",
                "acertar",
                "atualizar",
                "regularizar situacao",
                "acertar debitos",
                "quero regularizar",
            ],
            "liquidar_parcelamento": [
                "4",
                "liquidar",
                "liquidar parcelamento",
                "quitar parcelamento",
                "liquidar parcelas",
                "quitar parcelas",
                "liquidacao",
            ],
            "emitir_segunda_via": [
                "5",
                "segunda via",
                "2 via",
                "2a via",
                "2ª via",
                "emitir boleto",
                "gerar boleto",
                "emitir guia",
                "preciso da guia",
                "gerar nova via",
                "boleto",
                "guia",
                "emitir",
            ],
        }

        return encontrar_mais_similar(raw, opcoes)


class ItensPagamentoPayload(BaseModel):
    itens_informados: Optional[list[int]] = Field(
        default=None,
        description="Sequenciais dos itens escolhidos. Exemplo: [1, 2, 4]. Ou qualquer texto sem números para selecionar TODOS.",
    )
    todos_itens_informados: bool = Field(
        default=False,
        description="True quando o usuário escolher TODOS explicitamente.",
    )

    @field_validator("itens_informados", mode="before")
    @classmethod
    def normalizar_itens(cls, value: Any) -> Optional[list[int]]:
        if value in (None, ""):
            return None

        if isinstance(value, list):
            itens = value
        else:
            raw = str(value).strip().lower()

            # Extrai números da string
            numeros = re.findall(r"\d+", raw)

            # Se não tem números, assume que é "todos" por contexto
            # Exemplos: "tds", "td", "all", "everything", "sim", "confirmo", etc.
            if not numeros:
                # Retorna None, o workflow vai interpretar como "todos"
                return None

            itens = numeros

        if not itens:
            return None

        result = [int(item) for item in itens]
        if not result:
            return None
        return result


class ConfirmacaoPayload(BaseModel):
    confirma: bool = Field(
        ...,
        description=(
            "Interprete a resposta do usuário e converta para boolean. "
            "True para qualquer resposta afirmativa (sim, claro, pode, ok, confirmo, beleza, 👍, etc). "
            "False para qualquer resposta negativa (não, nao, cancela, volta, errado, 👎, etc)."
        ),
    )
