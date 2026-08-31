"""Todo argumento de texto de tool declara um teto de tamanho.

Teste de regressão do achado B-06. Nenhum dos schemas declarava restrição, e a
consequência não é só memória: `google_search` e `dharma_search_tool` disparam
uma chamada paga por invocação, e o teto no schema faz o próprio cliente MCP
recusar a chamada malformada antes de ela custar dinheiro.

Como em `test_http_auth_coverage.py`, o teste não confere uma lista fixa: ele
**enumera o JSON Schema de cada tool registrada** e falha nomeando o argumento
que não declarar `maxLength`. Uma tool nova, escrita por quem não conhecer esta
história, cai aqui sozinha.

O `maxLength` é lido do schema publicado, e não da assinatura em Python, porque
é o schema que o cliente enxerga — é ele que precisa estar certo.
"""

import asyncio

import pytest

from src.app import mcp


def _catalogo():
    """Tools registradas, indexadas por nome.

    Pela API pública: o acessor privado que servia para isto foi removido no
    fastmcp 4. `list_tools()` e corrotina, e este modulo e sincrono.
    """
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


# Argumentos sem teto no schema, cada um com o motivo. Como a lista de legado do
# middleware de auth, existe para encolher: acrescentar algo aqui é dizer "este
# argumento aceita entrada de tamanho arbitrário", e que seja decisão revisada.
SEM_TETO_JUSTIFICADO = {
    # Declarado como `dict` na assinatura da tool. Trocá-lo pelo modelo
    # `MemoryBank` poria os tetos no schema, mas mudaria o JSON Schema que os
    # clientes MCP já consomem — contrato, não hardening. Os campos são
    # validados no servidor por `MemoryBank` (ver src/tools/memory.py), e o
    # corpo inteiro segue limitado por `MAX_REQUEST_BODY_BYTES`.
    ("upsert_user_memory", "memory_bank"),
}


def _tipos(esquema: dict) -> set[str]:
    """Tipos JSON que o argumento aceita, atravessando o `anyOf` que o
    `Optional[...]` do Pydantic gera."""
    if "anyOf" in esquema:
        tipos = set()
        for ramo in esquema["anyOf"]:
            tipos |= _tipos(ramo)
        return tipos
    tipo = esquema.get("type")
    return {tipo} if tipo else set()


def _tem_teto(esquema: dict) -> bool:
    """`maxLength` para string, `maxItems` para array — no ramo que aceita o
    valor, não no envelope `anyOf`."""
    if "anyOf" in esquema:
        return all(
            _tem_teto(ramo) for ramo in esquema["anyOf"] if _tipos(ramo) != {"null"}
        )
    if esquema.get("type") == "string":
        return "maxLength" in esquema
    if esquema.get("type") == "array":
        return "maxItems" in esquema
    return True


def _argumentos():
    for nome_tool, tool in sorted(_catalogo().items()):
        propriedades = (tool.parameters or {}).get("properties", {})
        for nome_arg, esquema in propriedades.items():
            yield nome_tool, nome_arg, esquema


def test_ha_tools_registradas():
    """Sanidade: com o catálogo vazio o teste passaria por vacuidade."""
    assert _catalogo()


@pytest.mark.parametrize(
    "nome_tool,nome_arg,esquema",
    [pytest.param(*a, id=f"{a[0]}.{a[1]}") for a in _argumentos()],
)
def test_argumento_de_texto_declara_teto(nome_tool, nome_arg, esquema):
    if (nome_tool, nome_arg) in SEM_TETO_JUSTIFICADO:
        pytest.skip("sem teto por decisão registrada em SEM_TETO_JUSTIFICADO")

    if not ({"string", "array"} & _tipos(esquema)):
        pytest.skip("não é texto nem lista")

    assert _tem_teto(esquema), (
        f"`{nome_tool}.{nome_arg}` aceita entrada de tamanho arbitrário. "
        f"Anote com `Annotated[str, Field(max_length=...)]` usando um dos "
        f"LIMITE_* de src/app.py, ou registre a exceção com o motivo em "
        f"SEM_TETO_JUSTIFICADO. Schema atual: {esquema}"
    )


def test_a_lista_de_excecoes_nao_cresceu():
    """Isenção é dívida, e dívida precisa aparecer no balanço."""
    assert SEM_TETO_JUSTIFICADO == {("upsert_user_memory", "memory_bank")}


def test_as_excecoes_correspondem_a_argumentos_que_existem():
    """Entrada obsoleta é isenção fantasma: some da revisão porque ninguém liga
    o nome a um argumento real."""
    reais = {(t, a) for t, a, _ in _argumentos()}
    orfas = SEM_TETO_JUSTIFICADO - reais
    assert not orfas, f"isentos mas inexistentes: {sorted(orfas)}"


# ===== A outra metade do B-06: o que o schema não alcança =====
# `memory_bank` é isento acima porque é `dict` na assinatura da tool. O teto
# existe, só que no servidor. Estes testes cobrem essa fronteira — sem eles, a
# isenção registrada acima viraria carta branca.


def _banco_valido(**sobrescreve):
    base = {
        "memory_name": "nome",
        "description": "Nome do usuário",
        "memory_type": "base",
        "relevance": "high",
        "value": "João da Silva",
    }
    base.update(sobrescreve)
    return base


def test_memory_bank_recusa_nome_e_descricao_longos():
    from pydantic import ValidationError

    from src.tools.memory import MemoryBank

    for campo, teto in (("memory_name", 128), ("description", 512)):
        MemoryBank(**_banco_valido(**{campo: "x" * teto}))  # no teto, passa
        with pytest.raises(ValidationError):
            MemoryBank(**_banco_valido(**{campo: "x" * (teto + 1)}))


def test_memory_bank_nao_limita_o_value():
    """Deliberado, e testado para que continue deliberado.

    Um teto aqui recusaria upsert que hoje funciona, e não há como dimensioná-lo
    sem olhar o que já está gravado no RMI. Se alguém acrescentar um teto, que
    seja apagando este teste — com a distribuição real na mão.
    """
    from src.tools.memory import MemoryBank

    assert MemoryBank(**_banco_valido(value="x" * 100_000)).value
