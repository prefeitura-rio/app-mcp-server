"""Trava as constantes de módulo congeladas em CHATR-153 (decisão D7).

Por que este teste existe, e não só o comentário no código:

O congelamento tem um custo silencioso. `frozenset` e `MappingProxyType` não
são serializáveis por `json.dumps`, e `mappingproxy` também não sobrevive a
`deepcopy`/`pickle`. Hoje nada no repositório faz isso com estas constantes —
foi verificado antes de congelar — mas o dia em que alguém puser uma delas
numa resposta de tool ou em state do langgraph, o erro aparece em runtime, e
o conserto mais rápido é reverter para `list`/`dict`.

É exatamente essa reversão que este teste pega. Sem ele, o caminho de menor
resistência desfaz a decisão sem que ninguém perceba — e duas das constantes
são controle de segurança: `SENSITIVE_KEYS` decide o que é redigido antes de
ir ao error interceptor, e `ALLOWED_NEIGHBORHOODS_PONTOS_APOIO` é uma
whitelist. Mutáveis, ambas podiam ser esvaziadas em runtime.

O critério de aceite do CHATR-153 em forma executável.
"""

import ast
import re
from pathlib import Path

import pytest

from src.tools.cor_alert_tools import (
    NEIGHBORHOOD_ALIASES,
    VALID_ALERT_TYPES,
    VALID_SEVERITIES,
)
from src.tools.equipments_tools import ALLOWED_NEIGHBORHOODS_PONTOS_APOIO
from src.tools.multi_step_service.workflows.divida_ativa.core.models import (
    _OPTION_REGISTRY,
)
from src.utils.http_client import DEFAULT_ERROR_STATUS_CODES, SENSITIVE_KEYS

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Tipos mutáveis que nenhuma destas constantes pode voltar a ser.
TIPOS_MUTAVEIS = (list, dict, set)

CONSTANTES = {
    "ALLOWED_NEIGHBORHOODS_PONTOS_APOIO": ALLOWED_NEIGHBORHOODS_PONTOS_APOIO,
    "VALID_ALERT_TYPES": VALID_ALERT_TYPES,
    "VALID_SEVERITIES": VALID_SEVERITIES,
    "NEIGHBORHOOD_ALIASES": NEIGHBORHOOD_ALIASES,
    "DEFAULT_ERROR_STATUS_CODES": DEFAULT_ERROR_STATUS_CODES,
    "SENSITIVE_KEYS": SENSITIVE_KEYS,
    "_OPTION_REGISTRY": _OPTION_REGISTRY,
}


@pytest.mark.parametrize("nome", CONSTANTES)
def test_constante_nao_e_tipo_mutavel(nome):
    valor = CONSTANTES[nome]
    assert not isinstance(valor, TIPOS_MUTAVEIS), (
        f"{nome} voltou a ser {type(valor).__name__}. Congelada em CHATR-153: "
        f"use tuple, frozenset ou MappingProxyType. Se o motivo da reversão foi "
        f"um erro de serialização, converta na borda "
        f"(list(...) / dict(...)), não na definição."
    )


@pytest.mark.parametrize("nome", CONSTANTES)
def test_constante_rejeita_mutacao(nome):
    """Não basta o tipo: a tentativa de mutar tem que falhar de fato."""
    valor = CONSTANTES[nome]

    if isinstance(valor, tuple):
        with pytest.raises(AttributeError):
            valor.append("x")
        return

    if isinstance(valor, frozenset):
        with pytest.raises(AttributeError):
            valor.add("x")
        return

    # MappingProxyType
    with pytest.raises(TypeError):
        valor["chave_nova"] = "x"
    with pytest.raises(AttributeError):
        valor.update({"chave_nova": "x"})


def test_allowed_neighborhoods_tem_definicao_unica():
    """A desduplicação é o item do D7 que corrige risco de bug, não só forma.

    Antes de CHATR-153 o mesmo literal existia em `equipments_tools.py` e em
    `equipments_workflow.py`. Editar só um passaria em todos os testes e a
    tool e o workflow aceitariam listas de bairros diferentes — em silêncio,
    numa whitelist.
    """
    padrao = re.compile(r"^ALLOWED_NEIGHBORHOODS_PONTOS_APOIO\s*[:=]", re.MULTILINE)
    definicoes = [
        caminho.relative_to(PROJECT_ROOT)
        for caminho in (PROJECT_ROOT / "src").rglob("*.py")
        if "tests" not in caminho.parts
        and padrao.search(caminho.read_text(encoding="utf-8"))
    ]
    assert len(definicoes) == 1, (
        f"esperava uma única definição, encontrei {len(definicoes)}: {definicoes}"
    )


def test_labels_de_opcao_nao_tem_registro_paralelo():
    """`_OPTION_REGISTRY` é a origem única dos rótulos que viram botão/lista.

    Até CHATR-153 `templates.py` mantinha `OPCAO_MENU_LABELS`, um segundo mapa
    com seis rótulos idênticos aos do registry, usado só pelo branch de
    fallback do menu de pagamento. Os seis eram iguais — verificados um a um —
    mas nada obrigava: mudar o texto de um botão num arquivo e não no outro
    entregaria rótulos diferentes para o mesmo `value`, dependendo de o
    cidadão ter débito parcelado ou não.

    O critério é reproduzir o rótulo, não usar a chave. Mapas indexados pelos
    mesmos slugs para outro fim são legítimos e não entram aqui: roteamento
    (`tipo_consulta_steps`), configuração de API (`CONSULTA_CONFIGS`) e
    `TIPO_CONSULTA_LABELS`, que alimenta prosa e traz de propósito textos mais
    longos ("CDA" no botão, "Certidão de Dívida Ativa" na mensagem).
    """
    rotulos = {
        chave: opcao["label"]
        for chave, opcao in _OPTION_REGISTRY.items()
        if "label" in opcao
    }
    origem = Path("src/tools/multi_step_service/workflows/divida_ativa/core/models.py")

    paralelos = []
    for caminho in (PROJECT_ROOT / "src").rglob("*.py"):
        relativo = caminho.relative_to(PROJECT_ROOT)
        if "tests" in relativo.parts or relativo == origem:
            continue
        try:
            arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensivo
            continue
        for no in ast.walk(arvore):
            if not isinstance(no, ast.Dict):
                continue
            pares = [
                (chave.value, valor.value)
                for chave, valor in zip(no.keys, no.values)
                if isinstance(chave, ast.Constant)
                and isinstance(valor, ast.Constant)
                and chave.value in rotulos
            ]
            # Espelho puro: todo slug do dicionário reproduz o rótulo do
            # registry. Dois textos coincidirem não basta — `TIPO_CONSULTA_LABELS`
            # compartilha dois e diverge em três, de propósito.
            if len(pares) >= 2 and all(rotulos[k] == v for k, v in pares):
                paralelos.append(
                    f"{relativo}:{no.lineno} {sorted(k for k, _ in pares)}"
                )

    assert not paralelos, (
        f"registro paralelo de rótulos encontrado em {paralelos}. "
        f"A origem única é {origem}; importe `build_options` em vez de "
        f"redeclarar os rótulos."
    )
