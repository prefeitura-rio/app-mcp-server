import re
import unicodedata
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from src.tools.multi_step_service.workflows.sgrc_components.models import (
    AddressConfirmationPayload,
    AddressData,
    AddressPayload,
    AddressValidationState,
    CPFPayload,
    EmailPayload,
    NomePayload,
    TicketDataConfirmationPayload,
    parse_affirmation,
)

__all__ = [
    "AddressConfirmationPayload",
    "AddressData",
    "AddressPayload",
    "AddressValidationState",
    "ConfirmacaoServicoPayload",
    "CPFPayload",
    "EmailPayload",
    "NomePayload",
    "TicketDataConfirmationPayload",
]


def _normalize_text(value: object) -> str:
    text = str(value or "").strip().lower()
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", text)


class ConfirmacaoServicoPayload(BaseModel):
    """Payload para confirmação se o usuário deseja prosseguir com o serviço."""

    confirmacao_servico: bool = Field(
        ...,
        description=(
            "IMPORTANT: Interpret user's response and convert to boolean. "
            "Set to true for ANY affirmative response (sim, yes, isso, quero, ok, 👍, ✅). "
            "Set to false for ANY negative response (não, nao, no, errado, 👎, ❌)."
        ),
    )


class LuminariaDefeitoPayload(BaseModel):
    luminaria_defeito: str = Field(
        ...,
        description=(
            "O modelo deve interpretar a fala do usuario e enviar somente um valor fechado: "
            "Apagada, Piscando, Acesa de dia, Pendurada, Danificada ou Com ruído."
        ),
    )

    @field_validator("luminaria_defeito", mode="before")
    @classmethod
    def validate_defeito(cls, value: str) -> str:
        if value is None:
            raise ValueError("Informe o defeito da luminaria")

        normalized = _normalize_text(value)
        mapping = {
            "1": "Apagada",
            "apagada": "Apagada",
            "2": "Piscando",
            "piscando": "Piscando",
            "3": "Acesa de dia",
            "acesa de dia": "Acesa de dia",
            "acesa durante o dia": "Acesa de dia",
            "4": "Pendurada",
            "pendurada": "Pendurada",
            "5": "Danificada",
            "danificada": "Danificada",
            "6": "Com ruído",
            "com ruido": "Com ruído",
            "ruido": "Com ruído",
        }

        if normalized in mapping:
            return mapping[normalized]

        raise ValueError("Defeito de luminaria invalido")


class LuminariaQuantidadePayload(BaseModel):
    luminaria_quantidade: str = Field(
        ...,
        description=(
            "O modelo deve interpretar a fala do usuario e enviar somente um valor fechado: "
            "uma ou grupo."
        ),
    )

    @field_validator("luminaria_quantidade", mode="before")
    @classmethod
    def validate_quantidade(cls, value: str) -> str:
        if value is None:
            raise ValueError("Informe a quantidade de luminarias")

        normalized = _normalize_text(value)
        if normalized in {"1", "uma"}:
            return "uma"
        if normalized in {"2", "grupo"}:
            return "grupo"
        raise ValueError("Quantidade de luminarias invalida")


class LuminariaIntercaladasBlocoPayload(BaseModel):
    luminaria_intercaladas_bloco: str = Field(
        ..., description="1 para bloco, 2 para intercaladas"
    )

    @field_validator("luminaria_intercaladas_bloco", mode="before")
    @classmethod
    def validate_intercaladas_bloco(cls, value: str) -> str:
        if value is None:
            raise ValueError("Escolha entre bloco ou intercaladas")

        normalized = _normalize_text(value)
        if normalized in {"1", "1.0", "bloco", "juntas", "sequencia", "sequência"}:
            return "bloco"
        if normalized in {"2", "2.0", "intercaladas", "intervaladas"}:
            return "intercaladas"
        raise ValueError("Opcao invalida")


class LuminariaLocalizacaoPayload(BaseModel):
    luminaria_localizacao: Optional[str] = Field(
        None,
        description=(
            "O modelo deve interpretar a fala do usuario e enviar somente um valor fechado: "
            "Calçada, Fachada, Monumento, Parque, Praça, Quadra de esportes, Rua ou null."
        ),
    )

    @field_validator("luminaria_localizacao", mode="before")
    @classmethod
    def validate_localizacao(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            raise ValueError("Informe a localizacao da luminaria")

        normalized = _normalize_text(value)
        mapping = {
            "1": "Calçada",
            "calcada": "Calçada",
            "2": "Fachada",
            "fachada": "Fachada",
            "3": "Monumento",
            "monumento": "Monumento",
            "4": "Parque",
            "parque": "Parque",
            "5": "Praça",
            "praca": "Praça",
            "6": "Quadra de esportes",
            "quadra": "Quadra de esportes",
            "quadra de esportes": "Quadra de esportes",
            "7": "Rua",
            "rua": "Rua",
            "nao sei": None,
        }

        if normalized in mapping:
            return mapping[normalized]

        raise ValueError("Localizacao invalida")


class QuadraEsportesPayload(BaseModel):
    reparo_luminaria_quadra_esportes: bool = Field(
        ..., description="True se o defeito esta dentro de uma quadra de esportes"
    )

    @field_validator("reparo_luminaria_quadra_esportes", mode="before")
    @classmethod
    def parse_bool_from_text(cls, v: object) -> bool:
        result = parse_affirmation(v)
        if result is None:
            raise ValueError(f"Resposta ambígua: {v!r}. Use 'sim', 'não', 👍, etc.")
        return result
