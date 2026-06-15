"""
Testes para o handler do WhatsApp Flow de opções de dívida ativa.

Valida:
- _build_opcoes retorna subset correto para cada combinação de flags
- build_opcoes_flow_token encoda > 0 como bool no token
- _handle_init decodifica token e retorna opcoes filtradas
- _handle_init sem token usa defaults (True/True → todas as opções)
"""

from src.flows._token import encode_flow_token
from src.flows.divida_ativa.opcoes.handler import (
    _build_opcoes,
    build_opcoes_flow_token,
    _handle_init,
)


# ─────────────────────────────────────────────────────────────────────
# _build_opcoes
# ─────────────────────────────────────────────────────────────────────


def _ids(opcoes: list[dict]) -> set[str]:
    return {o["id"] for o in opcoes}


def test_build_opcoes_ambos_parcelado_e_nao_parcelado():
    opcoes = _build_opcoes(tem_nao_parcelado=True, tem_parcelado=True)
    ids = _ids(opcoes)
    assert ids == {
        "pagar_vista",
        "parcelar",
        "regularizar",
        "liquidar",
        "segunda_via",
        "voltar",
    }


def test_build_opcoes_apenas_nao_parcelado():
    opcoes = _build_opcoes(tem_nao_parcelado=True, tem_parcelado=False)
    ids = _ids(opcoes)
    assert ids == {"pagar_vista", "parcelar", "voltar"}
    assert "liquidar" not in ids
    assert "segunda_via" not in ids


def test_build_opcoes_apenas_parcelado():
    opcoes = _build_opcoes(tem_nao_parcelado=False, tem_parcelado=True)
    ids = _ids(opcoes)
    assert ids == {"regularizar", "liquidar", "segunda_via", "voltar"}
    assert "pagar_vista" not in ids
    assert "parcelar" not in ids


def test_build_opcoes_nenhum():
    """Nenhum débito: mesmo branch que 'só parcelado' (sem nao_parcelado)."""
    opcoes = _build_opcoes(tem_nao_parcelado=False, tem_parcelado=False)
    ids = _ids(opcoes)
    assert "voltar" in ids
    assert "pagar_vista" not in ids


def test_build_opcoes_preserva_ordem_da_lista_mestre():
    """Opções devem sair na ordem definida em _TODAS, não em ordem de set."""
    opcoes = _build_opcoes(tem_nao_parcelado=True, tem_parcelado=True)
    ids_em_ordem = [o["id"] for o in opcoes]
    assert ids_em_ordem == [
        "pagar_vista",
        "parcelar",
        "regularizar",
        "liquidar",
        "segunda_via",
        "voltar",
    ]


def test_build_opcoes_tem_campos_obrigatorios():
    opcoes = _build_opcoes(tem_nao_parcelado=True, tem_parcelado=False)
    for o in opcoes:
        assert "id" in o
        assert "title" in o
        assert "description" in o


# ─────────────────────────────────────────────────────────────────────
# build_opcoes_flow_token
# ─────────────────────────────────────────────────────────────────────


def test_build_opcoes_flow_token_positivos_vira_true():
    from src.flows._token import decode_flow_token

    token = build_opcoes_flow_token(
        "sessao-xyz", total_nao_parcelado=5000, total_parcelado=3000
    )
    data = decode_flow_token(token)
    assert data["tem_nao_parcelado"] is True
    assert data["tem_parcelado"] is True


def test_build_opcoes_flow_token_zeros_vira_false():
    from src.flows._token import decode_flow_token

    token = build_opcoes_flow_token(
        "sessao-abc", total_nao_parcelado=0, total_parcelado=0
    )
    data = decode_flow_token(token)
    assert data["tem_nao_parcelado"] is False
    assert data["tem_parcelado"] is False


def test_build_opcoes_flow_token_mixed():
    from src.flows._token import decode_flow_token

    token = build_opcoes_flow_token(
        "sessao-111", total_nao_parcelado=1, total_parcelado=0
    )
    data = decode_flow_token(token)
    assert data["tem_nao_parcelado"] is True
    assert data["tem_parcelado"] is False


# ─────────────────────────────────────────────────────────────────────
# _handle_init
# ─────────────────────────────────────────────────────────────────────


def test_handle_init_sem_token_retorna_todas_opcoes():
    """Sem token → defaults True/True → todas as 6 opções."""
    r = _handle_init(flow_token=None)
    assert r["version"] == "3.0"
    assert r["screen"] == "OPCOES"
    ids = _ids(r["data"]["opcoes"])
    assert len(ids) == 6


def test_handle_init_com_token_ambos():
    token = encode_flow_token("s1", {"tem_nao_parcelado": True, "tem_parcelado": True})
    r = _handle_init(flow_token=token)
    ids = _ids(r["data"]["opcoes"])
    assert len(ids) == 6


def test_handle_init_com_token_so_nao_parcelado():
    token = encode_flow_token("s2", {"tem_nao_parcelado": True, "tem_parcelado": False})
    r = _handle_init(flow_token=token)
    ids = _ids(r["data"]["opcoes"])
    assert ids == {"pagar_vista", "parcelar", "voltar"}


def test_handle_init_com_token_so_parcelado():
    token = encode_flow_token("s3", {"tem_nao_parcelado": False, "tem_parcelado": True})
    r = _handle_init(flow_token=token)
    ids = _ids(r["data"]["opcoes"])
    assert ids == {"regularizar", "liquidar", "segunda_via", "voltar"}


def test_handle_init_token_opaco_usa_defaults():
    """Token sem flags (opaco/legado) deve cair no default True/True."""
    r = _handle_init(flow_token="token-opaco-sem-v1-prefix")
    ids = _ids(r["data"]["opcoes"])
    assert len(ids) == 6


def test_handle_init_retorna_estrutura_correta():
    r = _handle_init(flow_token=None)
    assert "version" in r
    assert "screen" in r
    assert "data" in r
    assert "opcoes" in r["data"]
    assert isinstance(r["data"]["opcoes"], list)
