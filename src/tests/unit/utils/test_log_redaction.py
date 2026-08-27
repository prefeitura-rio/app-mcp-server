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


@pytest.fixture
def records():
    """
    Sink de teste que guarda o record inteiro, e não só o texto formatado.

    `extra` não aparece na linha formatada -- nem aqui nem no `FORMATO` de
    produção, que não imprime `{extra}`. Um teste que olhe só a mensagem passa
    com a redação de `extra` desligada, sem testar nada. Quem recebe `extra` de
    verdade é um sink com `serialize=True` ou um exportador OTLP, e é o record
    que eles leem.
    """
    vistos = []
    sink_id = logger.add(
        lambda mensagem: vistos.append(mensagem.record),
        level="DEBUG",
        format="{message}",
    )
    try:
        yield vistos
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


def test_redige_campo_extra(records):
    logger.bind(user_id="5521999998888").info("chamada de tool")

    assert records[0]["extra"]["user_id"] == "[REDACTED]"


def test_preserva_identificador_util(capturado):
    logger.info("inscricao 12345678 processada em 1756213800000")

    assert "12345678" in capturado[0]
    assert "1756213800000" in capturado[0]


def test_falha_na_redacao_nao_derruba_a_chamada_e_falha_fechado(capturado, monkeypatch):
    """
    Duas garantias distintas, e as duas importam.

    **Não levantar**, porque um patcher que estoura derruba o `logger.*` de quem
    chamou -- a linha de log vale menos do que a chamada que a produziu.

    **Falhar fechado**, porque devolver o texto original quando a redação quebra
    é pior do que não ter barreira: o vazamento é silencioso e ninguém procura
    por ele. Perder a linha custa um diagnóstico; deixá-la passar custa o CPF.
    """

    def explode(_texto):
        raise RuntimeError("falha na redação")

    monkeypatch.setattr(log_mod, "redigir_texto", explode)

    logger.info("cpf do cidadão 123.456.789-01")

    assert capturado  # a chamada de quem logou sobreviveu
    assert "123.456.789-01" not in capturado[0]  # e o dado não passou em claro
    assert "[REDACAO-FALHOU:RuntimeError]" in capturado[0]


def test_falha_na_redacao_do_extra_nao_vaza_o_extra(records, monkeypatch):
    """
    `message` e `extra` têm `try` separados: uma falha em um não pode devolver o
    outro em claro.
    """

    def explode(_obj, **_kwargs):
        raise RuntimeError("falha na redação")

    monkeypatch.setattr(log_mod, "redigir_estrutura", explode)

    logger.bind(user_id="5521999998888").info("chamada de tool")

    assert "5521999998888" not in str(records[0]["extra"])
    assert "[REDACAO-FALHOU" in str(records[0]["extra"])


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


def test_sink_falha_fechado_e_nao_escreve_o_traceback_cru(capsys, monkeypatch):
    """
    Quando a redação do texto final quebra, o traceback -- que é onde está o
    valor do cidadão -- não pode ir para o stderr como veio.
    """

    def explode(_texto):
        raise RuntimeError("falha na redação")

    monkeypatch.setattr(log_mod, "redigir_texto", explode)

    try:
        raise ValueError("cpf 123.456.789-01")
    except ValueError:
        logger.exception("falhou")

    err = capsys.readouterr().err

    assert "123.456.789-01" not in err
    assert "[REDACAO-FALHOU:RuntimeError]" in err


def test_segunda_passada_so_acontece_quando_ha_excecao(monkeypatch, capsys):
    """
    O que o sink acrescenta é `str(exception)` e o traceback, que só existem
    depois da formatação. Sem exceção, tudo que varia na linha é `{message}` --
    já redigido pelo patcher --, e repassar a redação dobraria o custo do
    caminho quente, que roda no thread do event loop.
    """
    chamadas = []
    original = log_mod.redigir_texto
    monkeypatch.setattr(
        log_mod, "redigir_texto", lambda t: chamadas.append(t) or original(t)
    )

    logger.info("linha comum, sem exceção")
    sem_excecao = len(chamadas)

    chamadas.clear()
    try:
        raise ValueError("estourou")
    except ValueError:
        logger.exception("com traceback")
    com_excecao = len(chamadas)

    capsys.readouterr()

    assert sem_excecao == 1, "linha comum não precisa da segunda passada"
    assert com_excecao == 2, "linha com traceback precisa das duas"


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


@pytest.mark.parametrize(
    "configurado,esperado",
    [
        ("info", "INFO"),
        ("debug", "DEBUG"),
        ("  warning  ", "WARNING"),
        ("", "INFO"),
        (None, "INFO"),
        ("lixo", "INFO"),
        ("ERROR", "ERROR"),
    ],
)
def test_log_level_invalido_nao_derruba_a_subida(monkeypatch, configurado, esperado):
    """
    O loguru exige o nível em maiúsculas e levanta `ValueError` no resto --
    `LOG_LEVEL=info` ou vazio é o que um ConfigMap produz sem esforço. Como este
    módulo é importado na primeira linha de `src/main.py`, o estouro aconteceria
    antes do preflight: em vez do relatório consolidado de variáveis faltantes,
    um traceback de dentro do loguru e CrashLoopBackOff.
    """
    monkeypatch.setattr(log_mod.Settings, "LOG_LEVEL", configurado)

    assert log_mod._nivel_de_log() == esperado


def test_instalar_redacao_e_idempotente(capsys):
    """Chamar de novo não pode duplicar o sink (linha de log em dobro)."""
    log_mod.instalar_redacao()
    logger.info("uma vez só")

    assert capsys.readouterr().err.count("uma vez só") == 1


# ---------------------------------------------------------------------------
# Teto de frames no traceback (CHATR-169)
# ---------------------------------------------------------------------------


def _traceback_falso(frames, cabecalho="Traceback (most recent call last):"):
    corpo = "".join(
        f'  File "/app/src/m{i}.py", line {i}, in f{i}\n    chamada_{i}()\n'
        for i in range(frames)
    )
    return f"{cabecalho}\n{corpo}ValueError: deu ruim"


def _conta_frames(texto):
    return len(
        [linha for linha in texto.split("\n") if linha.strip().startswith('File "')]
    )


def test_traceback_longo_e_cortado_no_meio():
    """
    Topo e base sobrevivem: o topo diz por onde a requisição entrou, a base diz
    o que estourou. O miolo é framework repetido.
    """
    cortado = log_mod._limitar_frames(_traceback_falso(30))

    assert _conta_frames(cortado) == log_mod.MAX_FRAMES_TRACEBACK
    assert "f0" in cortado and "f29" in cortado  # topo e base
    assert "f15" not in cortado  # miolo
    assert "20 frames intermediários omitidos" in cortado
    assert "ValueError: deu ruim" in cortado


def test_traceback_curto_fica_intacto():
    curto = _traceback_falso(6)

    assert log_mod._limitar_frames(curto) == curto


def test_linha_sem_traceback_fica_intacta():
    linha = "2026-01-01 00:00:00 | INFO | m:f:1 - mensagem comum"

    assert log_mod._limitar_frames(linha) == linha


def test_excecao_encadeada_mantem_o_separador():
    """
    `During handling of the above exception` está na coluna 0, não é frame, e
    sem ele o traceback encadeado vira duas pilhas coladas sem explicação.
    """
    encadeado = (
        _traceback_falso(20)
        + "\n\nDuring handling of the above exception, another exception occurred:\n\n"
        + _traceback_falso(20)
    )

    cortado = log_mod._limitar_frames(encadeado)

    assert "During handling of the above exception" in cortado
    assert cortado.count("Traceback (most recent call last):") == 2
    assert _conta_frames(cortado) == log_mod.MAX_FRAMES_TRACEBACK


def test_corte_de_frames_roda_antes_da_redacao(monkeypatch):
    """
    O que for cortado não precisa ser varrido. Inverter a ordem faria a redação
    pagar pelo traceback inteiro -- e é o traceback que domina o custo.
    """
    vistos = []

    def espia(texto):
        vistos.append(texto)
        return texto

    monkeypatch.setattr(log_mod, "redigir_texto", espia)
    log_mod._sink_redigido(_traceback_falso(30))

    assert vistos, "a redação do sink não foi chamada"
    assert _conta_frames(vistos[0]) == log_mod.MAX_FRAMES_TRACEBACK
