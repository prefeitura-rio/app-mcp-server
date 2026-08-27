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

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Tipos mutáveis que nenhuma destas constantes pode voltar a ser.
TIPOS_MUTAVEIS = (list, dict, set)


def _stub(nome: str, **atributos) -> types.ModuleType:
    modulo = types.ModuleType(nome)
    for chave, valor in atributos.items():
        setattr(modulo, chave, valor)
    return modulo


def _carregar(nome: str, caminho_relativo: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(nome, PROJECT_ROOT / caminho_relativo)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def constantes():
    """Carrega os módulos reais com as dependências pesadas stubadas.

    Escopo de módulo porque a carga é o custo; os testes só leem. A restauração
    de `sys.modules` é explícita (e não via `monkeypatch`, que é de função)
    para não vazar os stubs para o resto da suíte.
    """
    # Só as dependências que impediriam o import — nenhuma delas participa da
    # definição das constantes que este teste trava.
    stubs = {
        "src.config.env": _stub(
            "src.config.env",
            ENVIRONMENT="test",
            GOOGLE_MAPS_API_URL="https://maps.local/geocode",
            GOOGLE_MAPS_API_KEY="chave",
            COR_ALERT_WRITE_DEADLINE_SECONDS=1.0,
            EQUIPMENTS_VALID_THEMES=["tema"],
        ),
        "src.utils.error_interceptor": _stub(
            "src.utils.error_interceptor",
            send_api_error=lambda **_k: None,
            interceptor=lambda *_a, **_k: lambda f: f,
            # A fixture autouse `block_real_error_interceptor` do conftest faz
            # patch deste nome em todos os testes unitários; sem ele no stub,
            # o patch levanta AttributeError antes do teste começar.
            send_error_to_interceptor=lambda **_k: None,
        ),
        "src.utils.bigquery": _stub(
            "src.utils.bigquery",
            save_cor_alert_in_bq_background=lambda **_k: None,
            save_cor_alert_to_queue_background=lambda **_k: None,
            save_response_in_bq_background=lambda **_k: None,
            get_datetime=lambda *_a, **_k: None,
        ),
        "src.utils.background": _stub(
            "src.utils.background",
            disparar_em_background=lambda *_a, **_k: None,
        ),
        "src.tools.equipments.pluscode_service": _stub(
            "src.tools.equipments.pluscode_service",
            get_category_equipments=lambda *_a, **_k: None,
            get_tematic_instructions_for_equipments=lambda *_a, **_k: None,
            get_pluscode_coords_equipments=lambda *_a, **_k: None,
        ),
    }

    carregados = [
        ("src.utils.http_client", "src/utils/http_client.py"),
        ("src.tools.cor_alert_tools", "src/tools/cor_alert_tools.py"),
        ("src.tools.equipments_tools", "src/tools/equipments_tools.py"),
        (
            "src.tools.multi_step_service.workflows.divida_ativa.core.models",
            "src/tools/multi_step_service/workflows/divida_ativa/core/models.py",
        ),
    ]

    pacotes = {
        "src": PROJECT_ROOT / "src",
        "src.config": PROJECT_ROOT / "src" / "config",
        "src.utils": PROJECT_ROOT / "src" / "utils",
        "src.tools": PROJECT_ROOT / "src" / "tools",
        "src.tools.equipments": PROJECT_ROOT / "src" / "tools" / "equipments",
    }

    tocados = set(stubs) | {nome for nome, _ in carregados} | set(pacotes)
    anterior = {nome: sys.modules.get(nome) for nome in tocados}

    try:
        for nome, caminho in pacotes.items():
            if nome not in sys.modules:
                pacote = types.ModuleType(nome)
                pacote.__path__ = [str(caminho)]
                sys.modules[nome] = pacote
        sys.modules.update(stubs)

        modulos = {nome: _carregar(nome, caminho) for nome, caminho in carregados}
        http = modulos["src.utils.http_client"]
        cor = modulos["src.tools.cor_alert_tools"]
        equip = modulos["src.tools.equipments_tools"]
        pgm = modulos["src.tools.multi_step_service.workflows.divida_ativa.core.models"]

        yield {
            "ALLOWED_NEIGHBORHOODS_PONTOS_APOIO": (
                equip.ALLOWED_NEIGHBORHOODS_PONTOS_APOIO
            ),
            "VALID_ALERT_TYPES": cor.VALID_ALERT_TYPES,
            "VALID_SEVERITIES": cor.VALID_SEVERITIES,
            "NEIGHBORHOOD_ALIASES": cor.NEIGHBORHOOD_ALIASES,
            "DEFAULT_ERROR_STATUS_CODES": http.DEFAULT_ERROR_STATUS_CODES,
            "SENSITIVE_KEYS": http.SENSITIVE_KEYS,
            "_OPTION_REGISTRY": pgm._OPTION_REGISTRY,
        }
    finally:
        for nome, modulo in anterior.items():
            if modulo is None:
                sys.modules.pop(nome, None)
            else:
                sys.modules[nome] = modulo


SIMBOLOS = (
    "ALLOWED_NEIGHBORHOODS_PONTOS_APOIO",
    "VALID_ALERT_TYPES",
    "VALID_SEVERITIES",
    "NEIGHBORHOOD_ALIASES",
    "DEFAULT_ERROR_STATUS_CODES",
    "SENSITIVE_KEYS",
    "_OPTION_REGISTRY",
)


@pytest.mark.parametrize("nome", SIMBOLOS)
def test_constante_nao_e_tipo_mutavel(constantes, nome):
    valor = constantes[nome]
    assert not isinstance(valor, TIPOS_MUTAVEIS), (
        f"{nome} voltou a ser {type(valor).__name__}. Congelada em CHATR-153: "
        f"use tuple, frozenset ou MappingProxyType. Se o motivo da reversão foi "
        f"um erro de serialização, converta na borda "
        f"(list(...) / dict(...)), não na definição."
    )


@pytest.mark.parametrize("nome", SIMBOLOS)
def test_constante_rejeita_mutacao(constantes, nome):
    """Não basta o tipo: a tentativa de mutar tem que falhar de fato."""
    valor = constantes[nome]

    if isinstance(valor, tuple):
        with pytest.raises((AttributeError, TypeError)):
            valor.append("x")  # type: ignore[attr-defined]
        return

    if isinstance(valor, frozenset):
        with pytest.raises(AttributeError):
            valor.add("x")  # type: ignore[attr-defined]
        return

    # MappingProxyType
    with pytest.raises(TypeError):
        valor["chave_nova"] = "x"
    with pytest.raises(AttributeError):
        valor.update({"chave_nova": "x"})  # type: ignore[attr-defined]


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


def test_sensitive_keys_cobre_a_signed_url_do_gcs(constantes):
    """Regressão de CHATR-176: a signed URL é uma capability.

    Quem tem a URL baixa a guia do cidadão sem autenticar. Ela é enviada ao
    encurtador no campo `destination`, e uma falha lá reportaria o payload
    inteiro ao monitoramento. Estas três chaves são o que invalida a URL para
    quem lê o log.
    """
    chaves = constantes["SENSITIVE_KEYS"]
    for obrigatoria in (
        "signature",
        "x-goog-credential",
        "x-goog-signature",
        "googleaccessid",
    ):
        assert obrigatoria in chaves
