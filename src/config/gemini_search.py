"""Configuração dos modelos da busca web via Gemini (CHATR-298).

Uma variável só, `GEMINI_SEARCH_MODELS`, com uma lista JSON de modelos em ordem de
prioridade. Cada item traz os parâmetros de invocação daquele modelo:

    [{"model": "gemini-2.5-flash-lite", "timeout_s": 45, "thinking_budget": 1024},
     {"model": "gemini-3.5-flash-lite", "timeout_s": 20, "thinking_level": "MINIMAL",
      "max_search_queries": 1}]

- Variável ausente ou vazia: vale `DEFAULT_CONFIG`, a configuração de produção.
- Variável preenchida: cada campo omitido de um item vale o default do campo.
- Variável preenchida e inválida: erro com a lista completa de problemas. Quem
  escreveu um valor quis mudar algo; cair no default em silêncio esconderia isso.

O prazo total da busca não é configurável: é a soma dos `timeout_s` mais a reserva
da resolução de URLs, e precisa caber no teto do Salesforce.

Este módulo não importa `src.config.env`: o preflight (`src/health/preflight.py`)
valida a mesma variável antes da aplicação subir.
"""

from __future__ import annotations

import json
from typing import Any, List, Literal, Optional, Tuple

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    model_validator,
)

ENV_NAME = "GEMINI_SEARCH_MODELS"

# Tempo reservado no fim da busca para resolver as URLs das fontes. Também é o teto
# dessa etapa (ver `_resolver_urls_no_prazo`).
URL_RESOLUTION_RESERVE_SECONDS = 10.0
# Abaixo disto uma chamada ao Gemini não chega a responder: não vale tentar.
MIN_ATTEMPT_SECONDS = 5.0
# Teto do prazo total. O limite real do Salesforce é o de 120 s de callout por
# transação Apex; o Apex vê uns 6 s a mais que o prazo daqui, e a transação ainda
# faz `initialize` antes do `tools/call`. O `setTimeout` de 60 s do Apex não
# dispara: os pings SSE a cada 15 s zeram o timeout de leitura.
MAX_SEARCH_DEADLINE_SECONDS = 105.0

# Raciocínio dos 2.x (CHATR-234), em tokens; `0` desliga. Era 8192 (p99,9 das
# chamadas normais do flash-lite, 6.686). Na bateria de 26/10/2026 (CHATR-314),
# 1024 levou o p95 do 2.5 de 23,5 s para 12,9 s com qualidade dentro do ruído; 0
# perde qualidade (10 de 60 respostas insuficientes, contra 2).
DEFAULT_THINKING_BUDGET = 1024
# Os 3.x usam nível em vez de budget; no `MINIMAL` quase não gastam raciocínio.
DEFAULT_THINKING_LEVEL = "MINIMAL"
# Nos 2.5 o teto de saída limita raciocínio + resposta em cada etapa de geração:
# precisa caber o budget inteiro mais a resposta (p99,9 de 2.189 tokens).
MIN_ANSWER_TOKENS = 2048

ThinkingLevel = Literal["MINIMAL", "LOW", "MEDIUM", "HIGH"]


def is_gemini_2(model: str) -> bool:
    """Os 2.x configuram raciocínio por budget de tokens; os demais, por nível."""
    return model.startswith("gemini-2.")


class SearchModelConfig(BaseModel):
    """Parâmetros de invocação de um modelo da busca."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    model: str = Field(min_length=1)
    # Prazo do modelo, somando as tentativas e o backoff entre elas. 45 s cobre o
    # p99 das chamadas bem-sucedidas do 2.5 (27,6 s em out/2026).
    timeout_s: float = Field(
        default=45.0,
        ge=MIN_ATTEMPT_SECONDS,
        le=MAX_SEARCH_DEADLINE_SECONDS - URL_RESOLUTION_RESERVE_SECONDS,
    )
    # Chamadas no mesmo modelo em erro transitório (503, 429, 5xx): 1 + 1 retry.
    attempts: int = Field(default=2, ge=1, le=4)
    # Exatamente um dos dois, conforme a geração; omitidos, valem os defaults dela.
    # Budget negativo (o `-1` é "automático" no SDK) foi o que deixou o Gemini
    # gerar em loop por 120–160 s (CHATR-234).
    thinking_budget: Optional[int] = Field(default=None, ge=0)
    thinking_level: Optional[ThinkingLevel] = None
    max_output_tokens: int = Field(default=12288, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    # Consultas de grounding pedidas no prompt (CHATR-314). Ausente, o prompt pede
    # "ao menos 5"; nos 3.x o grounding é cobrado por consulta, e com 1 a qualidade
    # ficou dentro do ruído da produção na bateria de 26/10/2026.
    max_search_queries: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _raciocinio_default_da_geracao(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and isinstance(data.get("model"), str)
            and data.get("thinking_budget") is None
            and data.get("thinking_level") is None
        ):
            data = dict(data)
            if is_gemini_2(data["model"].strip()):
                data["thinking_budget"] = DEFAULT_THINKING_BUDGET
            else:
                data["thinking_level"] = DEFAULT_THINKING_LEVEL
        return data

    @model_validator(mode="after")
    def _raciocinio_coerente_com_a_geracao(self) -> SearchModelConfig:
        if self.thinking_budget is not None and self.thinking_level is not None:
            raise ValueError("use thinking_budget ou thinking_level, não os dois")
        if is_gemini_2(self.model):
            if self.thinking_level is not None:
                raise ValueError(
                    "os modelos gemini-2.* usam thinking_budget, não thinking_level"
                )
            if self.max_output_tokens < self.thinking_budget + MIN_ANSWER_TOKENS:
                raise ValueError(
                    f"max_output_tokens ({self.max_output_tokens}) precisa ser ao "
                    f"menos thinking_budget + {MIN_ANSWER_TOKENS} "
                    f"({self.thinking_budget + MIN_ANSWER_TOKENS}), senão a resposta "
                    "sai cortada em MAX_TOKENS"
                )
        elif self.thinking_budget is not None:
            raise ValueError(
                "só os modelos gemini-2.* usam thinking_budget; use thinking_level"
            )
        return self

    def resumo(self) -> str:
        raciocinio = (
            f"budget={self.thinking_budget}"
            if self.thinking_budget is not None
            else f"level={self.thinking_level}"
        )
        consultas = (
            f", queries={self.max_search_queries}"
            if self.max_search_queries is not None
            else ""
        )
        return (
            f"{self.model}({self.timeout_s:g}s, {self.attempts}x, {raciocinio}, "
            f"max_output={self.max_output_tokens}, temp={self.temperature:g}"
            f"{consultas})"
        )


class GeminiSearchConfig(RootModel[Tuple[SearchModelConfig, ...]]):
    """Lista de modelos da busca, em ordem de prioridade."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def _lista_coerente(self) -> GeminiSearchConfig:
        # Aqui, e não num `min_length`: o pydantic aplicaria o mínimo também à
        # lista parcial quando um item é inválido, e o erro sairia em dobro.
        if not self.root:
            raise ValueError("a lista precisa de ao menos um modelo")
        nomes = [m.model for m in self.root]
        repetidos = sorted({nome for nome in nomes if nomes.count(nome) > 1})
        if repetidos:
            raise ValueError(f"modelo repetido na lista: {', '.join(repetidos)}")
        if self.deadline_s > MAX_SEARCH_DEADLINE_SECONDS:
            soma = self.deadline_s - URL_RESOLUTION_RESERVE_SECONDS
            raise ValueError(
                f"a soma dos timeout_s ({soma:g}s) mais "
                f"{URL_RESOLUTION_RESERVE_SECONDS:g}s da resolução de URLs dá "
                f"{self.deadline_s:g}s, acima do teto de "
                f"{MAX_SEARCH_DEADLINE_SECONDS:g}s (o Salesforce corta em 120s). "
                "Reduza o timeout_s dos modelos"
            )
        return self

    @property
    def models(self) -> Tuple[SearchModelConfig, ...]:
        return self.root

    @property
    def deadline_s(self) -> float:
        """Prazo total da busca: todos os modelos e a resolução de URLs."""
        return search_deadline_seconds(self.root)

    def resumo(self) -> str:
        modelos = " → ".join(m.resumo() for m in self.root)
        return f"{modelos}; prazo total {self.deadline_s:g}s"


def search_deadline_seconds(
    models: Tuple[SearchModelConfig, ...] | List[SearchModelConfig],
    url_reserve_seconds: float = URL_RESOLUTION_RESERVE_SECONDS,
) -> float:
    return sum(m.timeout_s for m in models) + url_reserve_seconds


# Configuração de produção. O 2.5 vem primeiro por custo: o grounding dele é
# cobrado por prompt, com franquia diária; o do 3.x, por query de busca. O 3.5 fica
# com 20 s: na bateria do CHATR-298, p95 de 6,4 s e máximo de 9,3 s.
# Bateria de 26/10/2026 (CHATR-314; 60 perguntas, referência = produção com
# raciocínio de 8192 e ≥ 5 consultas: p50 12,3 s, p95 23,5 s, 9 sem fonte):
# - 2.5 com raciocínio de 1024: p50 9,8 s, p95 12,9 s, qualidade dentro do ruído;
# - 3.5 com 1 consulta: p50 4,6 s, p95 6,7 s, 3 sem fonte, qualidade dentro do
#   ruído, ~US$ 217/mês se respondesse todas (livre: ~US$ 848/mês).
DEFAULT_CONFIG = GeminiSearchConfig.model_validate(
    [
        {"model": "gemini-2.5-flash-lite", "timeout_s": 45, "thinking_budget": 1024},
        {
            "model": "gemini-3.5-flash-lite",
            "timeout_s": 20,
            "thinking_level": "MINIMAL",
            "max_search_queries": 1,
        },
    ]
)


class SearchConfigError(ValueError):
    """`GEMINI_SEARCH_MODELS` inválida, com um item por problema encontrado."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(
            f"{ENV_NAME} inválida ({len(errors)} erro(s)):\n"
            + "\n".join(f"  - {erro}" for erro in errors)
        )


def parse_search_config(raw: Optional[str]) -> Tuple[GeminiSearchConfig, str]:
    """Lê o valor bruto da variável e devolve a configuração e a origem dela
    (`"default"` ou `"env"`). Levanta `SearchConfigError` se o valor for inválido."""
    if raw is None or not raw.strip():
        return DEFAULT_CONFIG, "default"
    try:
        return GeminiSearchConfig.model_validate_json(raw), "env"
    except ValidationError as exc:
        raise SearchConfigError(_formatar_erros(exc, raw)) from None


# Mensagens do pydantic traduzidas para quem edita a variável no Infisical.
_MENSAGENS = {
    "json_invalid": lambda ctx: f"JSON inválido ({ctx.get('error')})",
    "tuple_type": lambda _ctx: "precisa ser uma lista JSON, com um objeto por modelo",
    "model_type": lambda _ctx: "cada modelo precisa ser um objeto JSON",
    "missing": lambda _ctx: "campo obrigatório ausente",
    "extra_forbidden": lambda _ctx: "campo desconhecido",
    "string_too_short": lambda _ctx: "não pode ser vazio",
    "greater_than_equal": lambda ctx: f"precisa ser ≥ {ctx.get('ge')}",
    "less_than_equal": lambda ctx: f"precisa ser ≤ {ctx.get('le')}",
    "int_parsing": lambda _ctx: "precisa ser um número inteiro",
    "int_type": lambda _ctx: "precisa ser um número inteiro",
    "int_from_float": lambda _ctx: "precisa ser um número inteiro",
    "float_parsing": lambda _ctx: "precisa ser um número",
    "float_type": lambda _ctx: "precisa ser um número",
    "string_type": lambda _ctx: "precisa ser um texto",
    "literal_error": lambda ctx: (
        f"precisa ser um de: {str(ctx.get('expected')).replace(' or ', ', ')}"
    ),
    "value_error": lambda ctx: str(ctx.get("error")),
}


def _formatar_erros(exc: ValidationError, raw: str) -> List[str]:
    nomes = _nomes_por_indice(raw)
    erros = []
    for erro in exc.errors(include_url=False):
        caminho = ENV_NAME
        for parte in erro["loc"]:
            if isinstance(parte, int):
                nome = nomes.get(parte)
                caminho += f"[{parte}]" + (f" ({nome})" if nome else "")
            else:
                caminho += f".{parte}"
        traduzir = _MENSAGENS.get(erro["type"])
        mensagem = traduzir(erro.get("ctx") or {}) if traduzir else erro["msg"]
        valor = erro.get("input")
        if erro["type"] not in ("json_invalid", "missing") and isinstance(
            valor, (str, int, float, bool)
        ):
            mensagem += f" (recebido: {json.dumps(valor, ensure_ascii=False)})"
        erros.append(f"{caminho}: {mensagem}")
    return erros


def _nomes_por_indice(raw: str) -> dict:
    """Nome de cada modelo da lista, para o erro dizer qual item está errado."""
    try:
        itens = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(itens, list):
        return {}
    return {
        indice: item["model"].strip()
        for indice, item in enumerate(itens)
        if isinstance(item, dict)
        and isinstance(item.get("model"), str)
        and item["model"].strip()
    }
