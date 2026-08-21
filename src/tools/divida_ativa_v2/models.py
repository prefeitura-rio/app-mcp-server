"""
Modelos Pydantic v2 para emissão de guias de dívida ativa (v2).

A v1 (`src/tools/divida_ativa.py`) faz parsing frágil com `ast.literal_eval` e
`float()` direto sobre o payload cru. Aqui a entrada é validada antes de
qualquer uso, aceitando tanto os tipos nativos quanto o formato legado em que
todo campo chega como string JSON escapada.
"""

import ast
import json
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Placeholder de template não renderizado pelo SFMC, ex.:
# "{{Event.DEAudience-abc.\"itens_informados\"}}"
PLACEHOLDER_PATTERN = re.compile(r"\{\{.*?\}\}", re.DOTALL)

# As chaves são dobradas porque a mensagem passa por str.format().
PLACEHOLDER_ERRO = (
    "Campo '{campo}' contém placeholder de template não renderizado "
    "({{{{...}}}}). Verifique a configuração do SFMC — o valor não foi "
    "substituído antes do envio."
)


def _contem_placeholder(valor: Any) -> bool:
    """Detecta placeholder de template em strings, listas e dicionários."""
    if isinstance(valor, str):
        return bool(PLACEHOLDER_PATTERN.search(valor))
    if isinstance(valor, dict):
        return any(
            _contem_placeholder(chave) or _contem_placeholder(item)
            for chave, item in valor.items()
        )
    if isinstance(valor, (list, tuple)):
        return any(_contem_placeholder(item) for item in valor)
    return False


def _checar_placeholder(valor: Any, campo: str) -> Any:
    """Levanta erro acionável se o valor carregar placeholder não renderizado."""
    if _contem_placeholder(valor):
        raise ValueError(PLACEHOLDER_ERRO.format(campo=campo))
    return valor


def _parse_estrutura(valor: Any, campo: str, vazio: Any) -> Any:
    """
    Converte o valor para estrutura Python.

    Aceita o formato nativo (dict/list) e o legado (string JSON escapada).
    String vazia ou em branco vira a coleção vazia correspondente.
    """
    _checar_placeholder(valor, campo)

    if valor is None:
        return vazio

    if isinstance(valor, str):
        texto = valor.strip()
        if not texto:
            return vazio
        try:
            return json.loads(texto)
        except ValueError:  # inclui json.JSONDecodeError
            # Fallback para o formato com aspas simples que a v1 tolerava
            try:
                return ast.literal_eval(texto)
            except (ValueError, SyntaxError) as exc:
                raise ValueError(
                    f"Campo '{campo}' não é um JSON válido: {texto!r}"
                ) from exc

    return valor


def _normalizar_sequencial(valor: Any) -> str:
    """
    Normaliza um sequencial para string, sem `float()` cru.

    Aceita '1', 1, 1.0 e True. Identificadores não numéricos (ex.: uma CDA
    '01/225716/2024-00') são preservados como estão.
    """
    if isinstance(valor, bool):
        return str(int(valor))
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, float):
        return str(int(valor))

    texto = str(valor).strip()
    # "1.0" -> "1", mas "01/225716/2024-00" permanece intacto
    try:
        return str(int(float(texto)))
    except (TypeError, ValueError):
        return texto


class EmitirGuiaRequest(BaseModel):
    """
    Payload de entrada para emissão de guia (à vista ou regularização).

    Aceita tanto tipos nativos quanto o formato legado enviado pelo SFMC, em
    que todos os campos chegam como string:

        {
            "dicionario_itens": "{\\"1\\": \\"01/225716/2024-00\\"}",
            "lista_cdas": "[\\"01/225716/2024-00\\"]",
            "lista_efs": "",
            "lista_guias": "",
            "apenas_um_item": "1"
        }
    """

    # O SFMC pode enviar campos extras de controle; ignorar em vez de quebrar.
    model_config = ConfigDict(extra="ignore")

    dicionario_itens: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Mapa de sequencial do item para o identificador do débito, "
            "ex.: {'1': '01/225716/2024-00'}."
        ),
    )
    lista_cdas: List[str] = Field(
        default_factory=list,
        description="Identificadores de CDA elegíveis para pagamento à vista.",
    )
    lista_efs: List[str] = Field(
        default_factory=list,
        description="Identificadores de execução fiscal elegíveis para pagamento à vista.",
    )
    lista_guias: List[str] = Field(
        default_factory=list,
        description="Identificadores de guia elegíveis para regularização.",
    )
    itens_informados: List[str] = Field(
        default_factory=list,
        description=(
            "Sequenciais escolhidos pelo contribuinte. Quando ausente, "
            "'apenas_um_item' é usado como único sequencial."
        ),
    )
    apenas_um_item: Optional[str] = Field(
        default=None,
        description=(
            "Sequencial único escolhido, usado quando 'itens_informados' "
            "não é informado. Aceita '1', 1 ou 1.0."
        ),
    )

    @field_validator("dicionario_itens", mode="before")
    @classmethod
    def _coagir_dicionario_itens(cls, valor: Any) -> Any:
        estrutura = _parse_estrutura(valor, "dicionario_itens", {})
        if not isinstance(estrutura, dict):
            raise ValueError(
                f"Campo 'dicionario_itens' deve ser um objeto/dicionário, "
                f"recebido {type(estrutura).__name__}."
            )
        # As chaves passam pela mesma normalização de 'itens_informados' e
        # 'apenas_um_item'; caso contrário {'01': ...} nunca casaria com o
        # sequencial '01', que é normalizado para '1', e o item seria
        # descartado em silêncio na montagem do payload.
        normalizado: Dict[str, str] = {}
        for chave, item in estrutura.items():
            sequencial = _normalizar_sequencial(chave)
            if sequencial in normalizado:
                raise ValueError(
                    f"Campo 'dicionario_itens' tem chaves duplicadas após "
                    f"normalização: {chave!r} colide com um sequencial já informado."
                )
            normalizado[sequencial] = str(item)
        return normalizado

    @field_validator(
        "lista_cdas",
        "lista_efs",
        "lista_guias",
        "itens_informados",
        mode="before",
    )
    @classmethod
    def _coagir_lista(cls, valor: Any, info) -> Any:
        campo = info.field_name
        estrutura = _parse_estrutura(valor, campo, [])

        # Um valor escalar isolado é tratado como lista de um elemento.
        if isinstance(estrutura, (str, int, float, bool)):
            estrutura = [estrutura]

        if isinstance(estrutura, tuple):
            estrutura = list(estrutura)

        if not isinstance(estrutura, list):
            raise ValueError(
                f"Campo '{campo}' deve ser uma lista, "
                f"recebido {type(estrutura).__name__}."
            )

        # Só 'itens_informados' carrega sequenciais numéricos; as demais listas
        # carregam identificadores (ex.: '01/225716/2024-00'), preservados como estão.
        if campo == "itens_informados":
            return [_normalizar_sequencial(item) for item in estrutura]
        return [str(item) for item in estrutura]

    @field_validator("apenas_um_item", mode="before")
    @classmethod
    def _coagir_apenas_um_item(cls, valor: Any) -> Optional[str]:
        _checar_placeholder(valor, "apenas_um_item")

        if valor is None:
            return None
        if isinstance(valor, str) and not valor.strip():
            return None

        return _normalizar_sequencial(valor)

    def sequenciais_escolhidos(self) -> List[str]:
        """
        Sequenciais a processar.

        Usa 'itens_informados' quando presente; senão cai para
        'apenas_um_item'. Nunca levanta exceção — os valores já foram
        validados na construção do modelo.
        """
        if self.itens_informados:
            return self.itens_informados
        if self.apenas_um_item is not None:
            return [self.apenas_um_item]
        return []


class GuiaEmitida(BaseModel):
    """
    Uma guia efetivamente emitida pela PGM.

    O EPGM emite uma guia por natureza de débito, então uma única solicitação
    com N identificadores pode devolver N guias, cada uma com seu próprio PIX,
    código de barras, PDF e vencimento.
    """

    # A PGM pode passar a devolver campos novos no registro da guia; ignorar
    # em vez de quebrar uma emissão que já aconteceu.
    model_config = ConfigDict(extra="ignore")

    # `str = ""` e não `Optional[str]`: `de_registro` — o único construtor —
    # normaliza campo ausente para string vazia, como a v1 sempre fez. Declarar
    # Optional induziria o consumidor a testar `is None`, ramo que nunca roda.
    codigo_de_barras: str = Field(default="", description="Código de barras da guia.")
    link: str = Field(default="", description="Link para o PDF da guia.")
    data_vencimento: str = Field(default="", description="Data de vencimento da guia.")
    pix: str = Field(default="", description="Código QR EMV do PIX da guia.")

    @classmethod
    def de_registro(cls, registro: Dict[str, Any]) -> "GuiaEmitida":
        """Constrói a guia a partir do registro cru da PGM."""
        return cls(
            codigo_de_barras=registro.get("codigoDeBarras") or "",
            link=registro.get("pdf") or "",
            data_vencimento=registro.get("dataVencimento") or "",
            pix=registro.get("codigoQrEMVPix") or "",
        )


class EmitirGuiaResponse(BaseModel):
    """
    Resposta da emissão de guia.

    Mantém o mesmo contrato da v1 para não quebrar os consumidores atuais:
    erro é sinalizado por 'api_resposta_sucesso: false' com
    'api_descricao_erro' preenchido, e não por status HTTP.
    """

    model_config = ConfigDict(extra="forbid")

    api_resposta_sucesso: bool = Field(
        ..., description="Indica se a emissão foi concluída com sucesso."
    )
    api_descricao_erro: Optional[str] = Field(
        default=None, description="Descrição do erro quando a emissão falha."
    )
    origem_solicitação: Optional[int] = Field(
        default=None, description="Origem da solicitação enviada à PGM."
    )
    cdas: Optional[List[str]] = Field(
        default=None, description="CDAs enviadas para emissão (pagamento à vista)."
    )
    efs: Optional[List[str]] = Field(
        default=None,
        description="Execuções fiscais enviadas para emissão (pagamento à vista).",
    )
    guias: Optional[List[str]] = Field(
        default=None, description="Guias enviadas para emissão (regularização)."
    )
    guias_emitidas: Optional[List[GuiaEmitida]] = Field(
        default=None,
        description=(
            "Todas as guias emitidas. O EPGM emite uma por natureza de "
            "débito, então uma solicitação com N identificadores pode "
            "devolver N guias."
        ),
    )
    total_guias: Optional[int] = Field(
        default=None, description="Quantidade de guias emitidas."
    )
    # Legado: mantidos para os consumidores que ainda leem uma guia só. Quando
    # há mais de uma, refletem a PRIMEIRA — quem precisa de todas deve ler
    # 'guias_emitidas'. Até CHATR-164 refletiam a última, sem critério.
    codigo_de_barras: Optional[str] = Field(
        default=None,
        description="Código de barras da primeira guia emitida (legado).",
    )
    link: Optional[str] = Field(
        default=None, description="Link para o PDF da primeira guia emitida (legado)."
    )
    data_vencimento: Optional[str] = Field(
        default=None,
        description="Data de vencimento da primeira guia emitida (legado).",
    )
    pix: Optional[str] = Field(
        default=None,
        description="Código QR EMV do PIX da primeira guia emitida (legado).",
    )

    @classmethod
    def de_erro(cls, descricao: str) -> "EmitirGuiaResponse":
        """Constrói uma resposta de falha com a descrição informada."""
        return cls(api_resposta_sucesso=False, api_descricao_erro=descricao)

    def para_dict(self) -> Dict[str, Any]:
        """Serializa omitindo os campos não preenchidos."""
        return self.model_dump(exclude_none=True)
