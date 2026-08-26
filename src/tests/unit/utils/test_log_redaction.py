"""
Testes da barreira global de redação de log (CHATR-167).

O bug era estrutural: `src/utils/log.py` não instalava sink, patcher nem filtro,
então **nenhuma** linha de `logger.*` passava por redação -- e ia íntegra para o
SigNoz. Estes testes fixam as duas camadas e a divisão de trabalho entre elas:

- **patcher** -- roda antes de todo sink, inclusive um adicionado por um teste, e
  enxerga a mensagem ainda estruturada;
- **sink** -- redige o texto final, que é onde aparece o traceback; um sink de
  terceiro não passa por essa segunda camada, e é por isso que os testes de
  traceback leem o stderr e não uma lista.
"""

import pytest
from loguru import logger

import src.utils.log as log_mod


@pytest.fixture
def capturado():
    """Sink de teste: recebe a mensagem já formatada, depois do patcher."""
    registros = []
    sink_id = logger.add(registros.append, level="DEBUG", format="{message}")
    try:
        yield registros
    finally:
        logger.remove(sink_id)


# ---------------------------------------------------------------------------
# Camada 1: patcher (vale para todo sink)
# ---------------------------------------------------------------------------


def test_redige_pii_na_mensagem(capturado):
    logger.info("pgm_api - Resposta para [123.456.789-01]: ok")

    assert "123.456.789-01" not in capturado[0]
    assert "[REDACTED-CPF]" in capturado[0]


def test_redige_dict_logado_pela_chave(capturado):
    """
    O caso do B1/B2 do ticket: `logger.info({... "parameters": parameters})`.
    A mensagem chega ao patcher com o dict de pé, que é a única janela em que dá
    para redigir `nome` -- que não tem padrão em texto livre.
    """
    logger.info(
        {
            "event": "da_emitir_guia_started",
            "parameters": {"cpf": "12345678901", "nome": "Fulano de Tal"},
        }
    )

    assert "Fulano de Tal" not in capturado[0]
    assert "12345678901" not in capturado[0]
    assert "da_emitir_guia_started" in capturado[0]


def test_redige_argumento_interpolado(capturado):
    """O loguru formata os args antes do patcher, então o valor já chega junto."""
    logger.info("cpf do solicitante: {}", "12345678901")

    assert "12345678901" not in capturado[0]


def test_redige_campo_extra(capturado):
    logger.bind(user_id="5521999998888").info("chamada de tool")

    assert "5521999998888" not in str(capturado)


def test_preserva_identificador_util(capturado):
    logger.info("inscricao 12345678 processada em 1756213800000")

    assert "12345678" in capturado[0]
    assert "1756213800000" in capturado[0]


def test_patcher_nao_derruba_a_chamada(capturado, monkeypatch):
    """
    Uma falha na redação não pode virar exceção no `logger.*` de quem chamou --
    a linha de log vale menos do que a chamada que a produziu.
    """

    def explode(_texto):
        raise RuntimeError("falha na redação")

    monkeypatch.setattr(log_mod, "redigir_texto", explode)

    logger.info("segue o baile")

    assert capturado  # a linha saiu mesmo com a redação quebrada


# ---------------------------------------------------------------------------
# Camada 2: sink de produção (stderr)
# ---------------------------------------------------------------------------


def test_sink_redige_o_traceback(capsys):
    """
    `str(exception)` e o traceback só existem depois da formatação -- fora do
    alcance do patcher. É o que cobre o `HTTPStatusError` com a URL inteira e o
    `ValueError` levantado com o dado do cidadão dentro.
    """
    try:
        raise ValueError("token=SEGREDO cpf 123.456.789-01")
    except ValueError:
        logger.exception("falha ao consultar")

    err = capsys.readouterr().err

    assert "SEGREDO" not in err
    assert "123.456.789-01" not in err
    assert "falha ao consultar" in err


def _consultar_na_pgm(cpf):
    """Levanta com o CPF no frame, que é o que o `diagnose` do loguru anota."""
    raise ValueError("api fora do ar")


def test_sink_nao_despeja_variaveis_locais(capsys):
    """
    O handler default do loguru roda com `diagnose=True`, que anota o valor das
    variáveis de cada frame do traceback:

        consultar(cpf_do_cidadao, chave)
        │         │               └ 'SEGREDO-OAUTH'
        │         └ '98765432100'

    É um vazamento que independe do call site -- atinge a árvore de chamada
    inteira -- e só some trocando o handler.
    """
    cpf_do_cidadao = "98765432100"

    try:
        _consultar_na_pgm(cpf_do_cidadao)
    except ValueError:
        logger.exception("falhou")

    assert "98765432100" not in capsys.readouterr().err


def test_debug_nao_sai_com_log_level_padrao(capsys):
    """
    `LOG_LEVEL` era lido em `settings.py` e nunca aplicado: o handler default do
    loguru é DEBUG, então `logger.debug(dados_imovel)` estava indo para o SigNoz.
    Isso é defesa em profundidade -- o controle é a redação, não o nível.
    """
    logger.debug("dados do imóvel em detalhe")
    logger.info("consulta concluída")

    err = capsys.readouterr().err

    assert "dados do imóvel em detalhe" not in err
    assert "consulta concluída" in err


def test_instalar_redacao_e_idempotente(capsys):
    """Chamar de novo não pode duplicar o sink (linha de log em dobro)."""
    log_mod.instalar_redacao()
    logger.info("uma vez só")

    assert capsys.readouterr().err.count("uma vez só") == 1
