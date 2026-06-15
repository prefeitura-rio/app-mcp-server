import re
import unicodedata
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


class NomePayload(BaseModel):
    name: Optional[str] = Field(
        None,
        min_length=3,
        max_length=100,
        description="Nome e sobrenome do usuário",
    )

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if not v or not v.strip():
            return None

        v = " ".join(v.split())

        if len(v.split()) < 2:
            raise ValueError("Por favor, informe nome e sobrenome")

        if not re.match(r"^[a-zA-ZÀ-ÿ\s'-]+$", v):
            raise ValueError("Nome deve conter apenas letras")

        if any(len(element) < 2 for element in v.split()):
            raise ValueError("Cada parte do nome deve ter pelo menos 2 caracteres")

        return " ".join(word.capitalize() for word in v.split())


class EmailPayload(BaseModel):
    email: Optional[str] = Field(None, description="Email válido do usuário")

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if not v or not v.strip():
            return None

        v = v.strip().lower()
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_pattern, v):
            raise ValueError("Email inválido. Use o formato: exemplo@dominio.com")

        return v


class CPFPayload(BaseModel):
    cpf: Optional[str] = Field(
        None,
        description=(
            "CPF com 11 dígitos. Use null quando o usuário não quiser "
            "informar CPF ou quiser continuar sem identificação."
        ),
    )

    @field_validator("cpf", mode="before")
    @classmethod
    def validate_cpf(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None

        cpf = re.sub(r"\D", "", str(v))

        if not cpf:
            return None

        if len(cpf) != 11 or len(set(cpf)) == 1:
            raise ValueError("CPF inválido")

        soma = sum(int(cpf[i]) * (10 - i) for i in range(9))
        digito1 = (soma * 10 % 11) % 10

        if int(cpf[9]) != digito1:
            raise ValueError("CPF inválido - dígito verificador incorreto")

        soma = sum(int(cpf[i]) * (11 - i) for i in range(10))
        digito2 = (soma * 10 % 11) % 10

        if int(cpf[10]) != digito2:
            raise ValueError("CPF inválido - dígito verificador incorreto")

        return cpf


class AddressData(BaseModel):
    logradouro: str = Field(..., description="Rua, avenida, etc")
    numero: Optional[str] = Field("", description="Número do endereço")
    complemento: Optional[str] = Field(None, description="Apartamento, bloco, etc")
    ponto_referencia: Optional[str] = Field(None, description="Ponto de referência")
    bairro: str = Field(..., description="Bairro")
    cep: Optional[str] = Field(None, description="CEP do endereço")
    cidade: str = Field(default="Rio de Janeiro", description="Cidade")
    estado: str = Field(default="RJ", description="Estado (sigla)")
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    logradouro_id_ipp: Optional[Union[int, str]] = None
    logradouro_nome_ipp: Optional[str] = None
    bairro_id_ipp: Optional[Union[int, str]] = None
    bairro_nome_ipp: Optional[str] = None
    formatted_address: Optional[str] = None
    original_text: Optional[str] = None

    @field_validator("cep", mode="before")
    @classmethod
    def validar_cep(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None

        cep = re.sub(r"\D", "", str(v))
        if len(cep) != 8:
            return None

        return cep


class AddressPayload(BaseModel):
    address: str = Field(..., description="Endereço completo")


class AddressConfirmationPayload(BaseModel):
    confirmacao: bool = Field(
        ...,
        description=(
            "Interprete a resposta do usuário e converta para boolean. "
            "True para qualquer resposta afirmativa (sim, claro, pode, ok, confirmo, beleza, 👍, etc). "
            "False para qualquer resposta negativa (não, nao, cancela, volta, errado, 👎, etc)."
        ),
    )

    @field_validator("confirmacao", mode="before")
    @classmethod
    def parse_bool_from_text(cls, v: object) -> bool:
        result = parse_affirmation(v)
        if result is None:
            raise ValueError(f"Resposta ambígua: {v!r}. Use 'sim', 'não', 👍, etc.")
        return result


class IdentificationMethodPayload(BaseModel):
    """Payload for choosing identification method (CPF or Gov.br)."""

    identification_method: Literal["cpf", "govbr"] = Field(
        ...,
        description="Método de identificação: 'cpf' para CPF manual ou 'govbr' para autenticação gov.br",
    )

    @field_validator("identification_method", mode="before")
    @classmethod
    def normalize_method(cls, value: Optional[str]) -> str:
        """Normalize user input to valid method choice."""
        if not value:
            raise ValueError("Método de identificação é obrigatório")

        normalized = str(value).lower().strip()

        mapping = {
            "cpf": "cpf",
            "gov.br": "govbr",
            "govbr": "govbr",
            "gov br": "govbr",
            "gov": "govbr",
            "governo": "govbr",
            "1": "cpf",
            "2": "govbr",
        }

        if normalized in mapping:
            return mapping[normalized]

        raise ValueError(f"Método inválido: '{value}'. Escolha 'CPF' ou 'Gov.br'")


class AddressValidationState(BaseModel):
    attempts: int = 0
    max_attempts: int = 3
    last_error: Optional[str] = None
    validated: bool = False


class PontoReferenciaPayload(BaseModel):
    ponto_referencia: Optional[str] = Field(
        None,
        description="Ponto de referência próximo ao local",
    )


class TicketDataConfirmationPayload(BaseModel):
    confirmacao: Optional[bool] = Field(
        None,
        description=(
            "Interprete a resposta do usuário e converta para boolean. "
            "True para qualquer resposta afirmativa (sim, claro, pode, ok, confirmo, beleza, 👍, etc). "
            "False para qualquer resposta negativa (não, nao, cancela, volta, errado, 👎, etc). "
            "Use null se o usuário não respondeu sobre confirmação."
        ),
    )
    correcao: Optional[str] = Field(
        None,
        description="Descrição do que precisa ser corrigido",
    )

    @field_validator("confirmacao", mode="before")
    @classmethod
    def parse_optional_bool(cls, v: object) -> Optional[bool]:
        if v is None:
            return None
        return parse_affirmation(v)


# ─── parse_affirmation ────────────────────────────────────────────────────────
# Removida do commit 33e5b5c por engano — app.py e testes dependem dela.


def _normalize_confirmation_text(value: object) -> str:
    text = str(value if value is not None else "").strip().lower()
    text = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )
    return re.sub(r"\s+", " ", text)


_AFFIRMATIVE_TOKENS = {
    "sim",
    "s",
    "yes",
    "y",
    "yeah",
    "yep",
    "ok",
    "okay",
    "okey",
    "oke",
    "isso",
    "isso mesmo",
    "isso ai",
    "exato",
    "exatamente",
    "correto",
    "ta correto",
    "esta correto",
    "certo",
    "ta certo",
    "confere",
    "confirmo",
    "confirmado",
    "confirma",
    "claro",
    "positivo",
    "afirmativo",
    "com certeza",
    "perfeito",
    "pode",
    "pode sim",
    "pode ser",
    "pode confirmar",
    "quero",
    "concordo",
    "aceito",
    "blz",
    "beleza",
    "uhum",
    "aham",
}
_NEGATIVE_TOKENS = {
    "nao",
    "n",
    "no",
    "nope",
    "errado",
    "incorreto",
    "negativo",
    "nao esta correto",
    "esta errado",
    "ta errado",
    "nao confere",
    "nao quero",
    "nao concordo",
    "discordo",
}
_THUMBS_UP = ("👍", "👌", "✅", "🆗", "✔")
_THUMBS_DOWN = ("👎", "❌", "🚫")
_NEGATION_WORDS = {
    "nao",
    "no",
    "nope",
    "nunca",
    "jamais",
    "nem",
    "errado",
    "incorreto",
    "negativo",
    "discordo",
}
_AFFIRMATION_WORDS = {
    "sim",
    "yes",
    "yeah",
    "yep",
    "isso",
    "correto",
    "certo",
    "ok",
    "okay",
    "claro",
    "confirmo",
    "confirmado",
    "positivo",
    "quero",
    "concordo",
    "exato",
}


def parse_affirmation(value: object) -> Optional[bool]:
    """Interpreta resposta como confirmação afirmativa/negativa/ambígua (None)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None

    raw = str(value)
    text = _normalize_confirmation_text(value)
    words = set(text.split())
    neg_in_text = bool(words & _NEGATION_WORDS) or text in _NEGATIVE_TOKENS
    aff_in_text = bool(words & _AFFIRMATION_WORDS) or text in _AFFIRMATIVE_TOKENS

    has_up = any(emoji in raw for emoji in _THUMBS_UP)
    has_down = any(emoji in raw for emoji in _THUMBS_DOWN)
    if has_up and not has_down and not neg_in_text:
        return True
    if has_down and not has_up and not aff_in_text:
        return False

    if not text:
        return None
    if text in _AFFIRMATIVE_TOKENS:
        return True
    if text in _NEGATIVE_TOKENS:
        return False
    return None
