"""Testes da configuração dos modelos da busca (CHATR-298).

`GEMINI_SEARCH_MODELS` ausente ou vazia sobe com a configuração de produção;
preenchida, vale o que estiver nela; preenchida e inválida, o boot cai com a lista
de problemas, em vez de seguir com um default que ninguém pediu.
"""

import json

import pytest
from pydantic import ValidationError

from src.config import gemini_search as gs


def parse(itens):
    return gs.parse_search_config(json.dumps(itens))


def erros(raw):
    with pytest.raises(gs.SearchConfigError) as excinfo:
        gs.parse_search_config(raw)
    return excinfo.value.errors


@pytest.mark.parametrize("raw", [None, "", "   "], ids=["ausente", "vazia", "espacos"])
def test_sem_valor_vale_a_configuracao_de_producao(raw):
    config, origem = gs.parse_search_config(raw)

    assert config is gs.DEFAULT_CONFIG
    assert origem == "default"


def test_configuracao_de_producao():
    """2.5 com raciocínio de 1024 e 3.5 com 1 consulta (bateria de 26/10/2026)."""
    principal, fallback = gs.DEFAULT_CONFIG.models

    assert principal.model_dump() == {
        "model": "gemini-2.5-flash-lite",
        "timeout_s": 45.0,
        "attempts": 2,
        "thinking_budget": 1024,
        "thinking_level": None,
        "max_output_tokens": 12288,
        "temperature": 0.0,
        "max_search_queries": None,
    }
    assert fallback.model_dump() == {
        "model": "gemini-3.5-flash-lite",
        "timeout_s": 20.0,
        "attempts": 2,
        "thinking_budget": None,
        "thinking_level": "MINIMAL",
        "max_output_tokens": 12288,
        "temperature": 0.0,
        "max_search_queries": 1,
    }
    # 45 + 20 + 10 de resolução de URLs.
    assert gs.DEFAULT_CONFIG.deadline_s == 75.0


def test_campos_omitidos_valem_o_default_do_campo():
    config, origem = parse([{"model": "gemini-3.5-flash-lite"}])

    assert origem == "env"
    [modelo] = config.models
    assert modelo.timeout_s == 45.0
    assert modelo.attempts == 2
    assert modelo.thinking_level == "MINIMAL"
    assert modelo.max_output_tokens == 12288
    assert config.deadline_s == 55.0


def test_raciocinio_default_segue_a_geracao():
    config, _ = parse([{"model": "gemini-2.5-flash"}, {"model": "gemini-3.1-flash"}])

    dois, tres = config.models
    assert (dois.thinking_budget, dois.thinking_level) == (1024, None)
    assert (tres.thinking_budget, tres.thinking_level) == (None, "MINIMAL")
    # O teto de saída padrão comporta o budget padrão: 12288 ≥ 1024 + 2048.
    assert dois.max_output_tokens == 12288


def test_todos_os_campos_preenchidos():
    config, _ = parse(
        [
            {
                "model": "gemini-2.5-flash-lite",
                "timeout_s": 30,
                "attempts": 3,
                "thinking_budget": 0,
                "max_output_tokens": 4096,
                "temperature": 0.2,
            },
            {
                "model": "gemini-3.5-flash-lite",
                "timeout_s": 15.5,
                "attempts": 1,
                "thinking_level": "LOW",
                "max_output_tokens": 8192,
                "temperature": 1,
            },
        ]
    )

    dois, tres = config.models
    assert (dois.timeout_s, dois.attempts, dois.thinking_budget) == (30.0, 3, 0)
    assert (dois.max_output_tokens, dois.temperature) == (4096, 0.2)
    assert (tres.timeout_s, tres.attempts, tres.thinking_level) == (15.5, 1, "LOW")
    assert config.deadline_s == 55.5


@pytest.mark.parametrize("valor", [1, 3])
def test_limite_de_consultas_aceita_inteiro_positivo(valor):
    config, _ = parse([{"model": "gemini-3.5-flash-lite", "max_search_queries": valor}])

    assert config.models[0].max_search_queries == valor


def test_limite_de_consultas_ausente_e_sem_limite():
    config, _ = parse([{"model": "gemini-3.5-flash-lite"}])

    assert config.models[0].max_search_queries is None


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        pytest.param(0, "precisa ser ≥ 1 (recebido: 0)", id="zero"),
        pytest.param(-2, "precisa ser ≥ 1 (recebido: -2)", id="negativo"),
        pytest.param(
            '"um"', 'precisa ser um número inteiro (recebido: "um")', id="texto"
        ),
        pytest.param(1.5, "precisa ser um número inteiro (recebido: 1.5)", id="frac"),
    ],
)
def test_limite_de_consultas_invalido(valor, esperado):
    [erro] = erros(
        f'[{{"model": "gemini-3.5-flash-lite", "max_search_queries": {valor}}}]'
    )

    assert erro == (
        "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite).max_search_queries: "
        + esperado
    )


def test_resumo_mostra_o_limite_de_consultas_so_quando_existe():
    config, _ = parse(
        [
            {"model": "gemini-2.5-flash-lite", "timeout_s": 35},
            {"model": "gemini-3.5-flash-lite", "max_search_queries": 1},
        ]
    )

    dois, tres = config.models
    assert "queries" not in dois.resumo()
    assert tres.resumo() == (
        "gemini-3.5-flash-lite(45s, 2x, level=MINIMAL, max_output=12288, temp=0, "
        "queries=1)"
    )


def test_configuracao_e_imutavel():
    with pytest.raises(ValidationError):
        gs.DEFAULT_CONFIG.models[0].timeout_s = 90


@pytest.mark.parametrize(
    ("raw", "esperado"),
    [
        pytest.param("nope", "GEMINI_SEARCH_MODELS: JSON inválido", id="json_invalido"),
        pytest.param(
            "[]",
            "GEMINI_SEARCH_MODELS: a lista precisa de ao menos um modelo",
            id="lista_vazia",
        ),
        pytest.param(
            '{"model": "gemini-2.5-flash-lite"}',
            "GEMINI_SEARCH_MODELS: precisa ser uma lista JSON",
            id="objeto_em_vez_de_lista",
        ),
        pytest.param(
            '["gemini-2.5-flash-lite"]',
            "GEMINI_SEARCH_MODELS[0]: cada modelo precisa ser um objeto JSON",
            id="formato_antigo_por_virgula",
        ),
        pytest.param(
            '[{"timeout_s": 20}]',
            "GEMINI_SEARCH_MODELS[0].model: campo obrigatório ausente",
            id="sem_model",
        ),
        pytest.param(
            '[{"model": " "}]',
            "GEMINI_SEARCH_MODELS[0].model: não pode ser vazio",
            id="model_vazio",
        ),
        pytest.param(
            '[{"model": "gemini-3.5-flash-lite", "timout_s": 20}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite).timout_s: campo "
            "desconhecido (recebido: 20)",
            id="campo_com_erro_de_digitacao",
        ),
        pytest.param(
            '[{"model": "gemini-3.5-flash-lite", "timeout_s": 4}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite).timeout_s: precisa ser "
            "≥ 5.0 (recebido: 4)",
            id="timeout_abaixo_do_piso",
        ),
        pytest.param(
            '[{"model": "gemini-3.5-flash-lite", "timeout_s": 450}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite).timeout_s: precisa ser "
            "≤ 95.0 (recebido: 450)",
            id="timeout_acima_do_teto",
        ),
        pytest.param(
            '[{"model": "gemini-3.5-flash-lite", "timeout_s": "x"}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite).timeout_s: precisa ser "
            'um número (recebido: "x")',
            id="timeout_nao_numerico",
        ),
        pytest.param(
            '[{"model": "gemini-3.5-flash-lite", "attempts": 0}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite).attempts: precisa ser "
            "≥ 1 (recebido: 0)",
            id="attempts_zero",
        ),
        pytest.param(
            '[{"model": "gemini-2.5-flash-lite", "thinking_budget": -1}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-2.5-flash-lite).thinking_budget: "
            "precisa ser ≥ 0 (recebido: -1)",
            id="budget_automatico_do_sdk",
        ),
        pytest.param(
            '[{"model": "gemini-3.5-flash-lite", "thinking_level": "ULTRA"}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite).thinking_level: "
            "precisa ser um de: 'MINIMAL', 'LOW', 'MEDIUM', 'HIGH' "
            '(recebido: "ULTRA")',
            id="nivel_desconhecido",
        ),
        pytest.param(
            '[{"model": "gemini-3.5-flash-lite", "thinking_budget": 512, '
            '"thinking_level": "LOW"}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite): use thinking_budget "
            "ou thinking_level, não os dois",
            id="budget_e_nivel_juntos",
        ),
        pytest.param(
            '[{"model": "gemini-2.5-flash-lite", "thinking_level": "LOW"}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-2.5-flash-lite): os modelos gemini-2.* "
            "usam thinking_budget, não thinking_level",
            id="nivel_no_2x",
        ),
        pytest.param(
            '[{"model": "gemini-3.5-flash-lite", "thinking_budget": 512}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite): só os modelos "
            "gemini-2.* usam thinking_budget; use thinking_level",
            id="budget_no_3x",
        ),
        pytest.param(
            '[{"model": "gemini-2.5-flash-lite", "thinking_budget": 8192, '
            '"max_output_tokens": 9000}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-2.5-flash-lite): max_output_tokens "
            "(9000) precisa ser ao menos thinking_budget + 2048 (10240)",
            id="saida_nao_cabe_o_raciocinio",
        ),
        pytest.param(
            '[{"model": "gemini-3.5-flash-lite", "temperature": 3}]',
            "GEMINI_SEARCH_MODELS[0] (gemini-3.5-flash-lite).temperature: precisa "
            "ser ≤ 2.0 (recebido: 3)",
            id="temperatura_fora_da_faixa",
        ),
        pytest.param(
            '[{"model": "gemini-2.5-flash-lite"}, {"model": "gemini-2.5-flash-lite"}]',
            "GEMINI_SEARCH_MODELS: modelo repetido na lista: gemini-2.5-flash-lite",
            id="modelo_repetido",
        ),
        pytest.param(
            '[{"model": "gemini-2.5-flash-lite", "timeout_s": 45}, '
            '{"model": "gemini-3.5-flash-lite", "timeout_s": 45}, '
            '{"model": "gemini-3.1-flash-lite", "timeout_s": 45}]',
            "GEMINI_SEARCH_MODELS: a soma dos timeout_s (135s) mais 10s da resolução "
            "de URLs dá 145s, acima do teto de 105s",
            id="soma_acima_do_teto_do_salesforce",
        ),
    ],
)
def test_valor_invalido_derruba_com_erro_especifico(raw, esperado):
    [erro] = erros(raw)

    assert erro.startswith(esperado)


def test_reporta_todos_os_problemas_de_uma_vez():
    lista = erros(
        '[{"model": "gemini-2.5-flash-lite", "timeout_s": 450, "attempts": 0}, '
        '{"model": "gemini-3.5-flash-lite", "thinking_budget": 512}]'
    )

    assert len(lista) == 3
    assert lista[0].startswith("GEMINI_SEARCH_MODELS[0] (gemini-2.5-flash-lite).")
    assert lista[2].startswith("GEMINI_SEARCH_MODELS[1] (gemini-3.5-flash-lite)")


def test_mensagem_do_erro_lista_os_problemas():
    with pytest.raises(gs.SearchConfigError) as excinfo:
        gs.parse_search_config("[]")

    assert str(excinfo.value) == (
        "GEMINI_SEARCH_MODELS inválida (1 erro(s)):\n"
        "  - GEMINI_SEARCH_MODELS: a lista precisa de ao menos um modelo"
    )


def test_resumo_para_o_log_do_boot():
    assert gs.DEFAULT_CONFIG.resumo() == (
        "gemini-2.5-flash-lite(45s, 2x, budget=1024, max_output=12288, temp=0) → "
        "gemini-3.5-flash-lite(20s, 2x, level=MINIMAL, max_output=12288, temp=0, "
        "queries=1); prazo total 75s"
    )
