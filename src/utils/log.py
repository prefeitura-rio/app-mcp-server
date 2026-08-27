"""
Configuração global de log (CHATR-167).

Este módulo é a barreira: toda linha de `logger.*` do processo passa por
redação antes de chegar a qualquer sink. Até o CHATR-167 ele só ajustava o
nível de `httpx`/`httpcore` -- não instalava sink, patcher nem filtro --, e o
resultado é que **nenhuma** linha de log era redigida: CPF, nome, endereço e
segredo de OAuth2 iam íntegros para o SigNoz.

São duas camadas, e cada uma pega o que a outra não alcança:

1. `patcher` -- roda antes de todo sink, inclusive um que um teste adicione, e
   é a única camada que alcança `record["extra"]`, que não passa pela
   formatação e chega inteiro a um sink que serializa o record.
2. `sink` -- redige o texto já formatado, que é onde aparecem `str(exception)`,
   o traceback e os argumentos interpolados depois do patcher.

O sink próprio substitui o handler default do loguru, que roda com
`diagnose=True` e imprime o **valor das variáveis locais** em todo traceback.
Esse vazamento não é nenhum call site específico: atinge a árvore de chamada
inteira, e só some trocando o handler.

Os módulos que fazem `from loguru import logger` direto continuam cobertos: o
`logger` do loguru é um singleton e esta configuração é global. Basta que este
módulo seja importado uma vez, o mais cedo possível na subida (ver
`src/main.py`).
"""

import logging
import re
import sys
from typing import Any, Dict, Optional

from loguru import logger

from src.config.settings import Settings
from src.utils.pii import redigir_estrutura, redigir_texto


logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


logger.disable("httpx")
logger.disable("httpcore")


# Mesmo formato do handler default do loguru. As tags de cor são inertes em
# sink de função (`colorize` é False), e ficam aqui para o caso de alguém
# reapontar a saída para um stream colorido.
FORMATO = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

# Teto de frames no traceback. `backtrace=False` já tirou o encadeamento
# estendido do loguru, mas o traceback do próprio Python continua inteiro, e uma
# pilha de FastAPI + httpx + langgraph passa fácil de 40 frames. O custo aparece
# duas vezes: volume no SigNoz e CPU, porque o traceback é a única coisa que o
# sink redige de novo -- 3 KB custam ~0,5 ms de redação, e traceback acontece em
# tempestade de erro, exatamente quando não há CPU sobrando.
#
# Os frames do topo dizem por onde a requisição entrou e os de baixo dizem o que
# estourou; o miolo é framework repetido. Cortar o meio preserva os dois lados.
MAX_FRAMES_TRACEBACK = 10
_FRAMES_DO_TOPO = 3

# Início de frame no traceback do Python: `  File "<caminho>", line N, in <f>`.
# Linhas na coluna 0 (`Traceback (most recent call last):`, `During handling of
# ...`, a linha final da exceção) nunca pertencem a um frame e são preservadas
# sempre -- é o que mantém legível o encadeamento de exceções.
_INICIO_DE_FRAME = re.compile(r'^\s+File "')

_MARCADOR_FRAMES = "  [... {n} frames intermediários omitidos ...]"


def _limitar_frames(texto: str, maximo: int = MAX_FRAMES_TRACEBACK) -> str:
    """
    Corta os frames do miolo do traceback, mantendo topo e base.

    Opera sobre o texto já formatado porque é lá que o traceback existe -- o
    loguru o anexa depois do `{message}`. Texto sem traceback sai intacto no
    primeiro `if`.
    """
    if "\n" not in texto:
        return texto
    linhas = texto.split("\n")
    indices = [i for i, linha in enumerate(linhas) if _INICIO_DE_FRAME.match(linha)]
    if len(indices) <= maximo:
        return texto

    corta_de = indices[_FRAMES_DO_TOPO]
    corta_ate = indices[len(indices) - (maximo - _FRAMES_DO_TOPO)]
    omitidos = len(indices) - maximo

    mantidas = linhas[:corta_de]
    mantidas.append(_MARCADOR_FRAMES.format(n=omitidos))
    # Linha na coluna 0 no meio do trecho cortado é separador de exceção
    # encadeada, não frame: sem ela o traceback vira duas pilhas coladas.
    mantidas.extend(
        linha
        for linha in linhas[corta_de:corta_ate]
        if linha and not linha[0].isspace()
    )
    mantidas.extend(linhas[corta_ate:])
    return "\n".join(mantidas)


# Substitui a linha quando a própria redação falha. Fail-closed: ver
# `_redigir_record`.
_MARCADOR_FALHA = "[REDACAO-FALHOU:{tipo}] conteúdo suprimido"

_redacao_instalada = False


def _nivel_de_log() -> str:
    """
    `LOG_LEVEL` normalizado, com INFO no lugar de valor que o loguru não aceita.

    O loguru exige o nome em maiúsculas e levanta `ValueError` no resto -- e
    `LOG_LEVEL=info` ou vazio é o que um ConfigMap produz sem esforço nenhum.
    Como este módulo é importado na **primeira** linha de `src/main.py`, o
    estouro aconteceria antes do preflight: em vez do relatório consolidado de
    variáveis, o operador receberia um traceback de dentro do loguru, em
    CrashLoopBackOff. Nível de log errado é motivo para logar mais, nunca para
    não subir.

    Até o CHATR-167 o valor era lido e nunca aplicado, então qualquer conteúdo
    era inofensivo; passar a aplicá-lo é o que cria o modo de falha.
    """
    configurado = str(Settings.LOG_LEVEL or "").strip().upper() or "INFO"
    try:
        logger.level(configurado)
    except ValueError:
        logger.warning(
            "LOG_LEVEL={} não é um nível conhecido; usando INFO.",
            Settings.LOG_LEVEL,
        )
        return "INFO"
    return configurado


def _redigir_record(record: Dict[str, Any]) -> None:
    """
    Redige a mensagem e os campos extras antes de qualquer sink.

    `record["message"]` já é `str` quando o patcher a vê: o loguru monta o record
    com `str(message)` (`_logger.py:2024`) e só depois chama o patcher
    (`_logger.py:2060`). Um `logger.info({...})` chega aqui stringificado, e quem
    cobre esse caso é `redigir_chaves_sensiveis`, que reconhece a chave dentro do
    texto -- não existe janela para redigir a estrutura de pé. Se essa cobertura
    por chave sair de `redigir_texto`, nome e endereço voltam a vazar.

    `extra` é outra história: não passa pela formatação, então continua sendo
    redigido como estrutura. O `FORMATO` daqui não o imprime, mas um sink com
    `serialize=True` ou um exportador OTLP recebe o dicionário inteiro
    (`_handler.py:270`).

    Nunca levanta, e falha **fechado**. São duas garantias diferentes e as duas
    importam. Não levantar, porque um patcher que estoura derruba o `logger.*`
    de quem chamou -- a linha de log vale menos do que a chamada que a produziu.
    Falhar fechado, porque uma barreira de PII que devolve o texto original
    quando quebra é pior do que não existir: o vazamento é silencioso e ninguém
    vai procurar por ele. Perder a linha custa um diagnóstico; deixá-la passar
    custa o CPF.

    Mensagem e `extra` têm `try` separados de propósito: uma falha em um não
    pode devolver o outro em claro.

    O marcador carrega o **tipo** da exceção, que é o que permite achar a causa,
    e não a mensagem dela -- essa costuma repetir justamente o valor que estourou
    a redação. `[REDACAO-FALHOU` é o termo a alertar no SigNoz.
    """
    try:
        record["message"] = redigir_texto(str(record["message"]))
    except Exception as erro:
        record["message"] = _MARCADOR_FALHA.format(tipo=type(erro).__name__)

    try:
        extra = record.get("extra")
        if extra:
            record["extra"] = redigir_estrutura(extra)
    except Exception as erro:
        record["extra"] = {"redacao": _MARCADOR_FALHA.format(tipo=type(erro).__name__)}


def _linha_de_falha(record: Optional[Dict[str, Any]], erro: Exception) -> str:
    """
    Linha de substituição para quando a redação do texto final falha.

    Só campos que não podem conter PII -- nível, origem no código e o tipo do
    erro. É pouco, mas localiza a chamada, e não é a linha original: essa é
    exatamente a que não pode sair.
    """
    nivel, origem = "ERROR", "?"
    try:
        nivel = record["level"].name
        origem = f"{record['name']}:{record['function']}:{record['line']}"
    except Exception:
        pass
    return (
        f"{nivel: <8} | {origem} - {_MARCADOR_FALHA.format(tipo=type(erro).__name__)}\n"
    )


def _sink_redigido(mensagem: Any) -> None:
    """
    Escreve a linha final, redigindo o que o patcher não alcançou.

    A segunda passada é condicional. O que ela acrescenta é `str(exception)` e o
    traceback, que só existem depois da formatação; sem exceção, tudo que varia
    na linha é `{message}` -- já redigido pelo patcher -- mais timestamp, nível e
    origem, que não são PII. Redigir de novo dobraria o custo do caminho quente
    sem cobrir nada novo, e esse custo roda no thread do event loop.

    O corte de frames vem **antes** da redação, e não depois: o que for cortado
    não precisa ser varrido, e é o traceback que domina o custo do caminho.

    Falha fechado, pelo mesmo motivo de `_redigir_record`.
    """
    record = getattr(mensagem, "record", None)
    try:
        texto = str(mensagem)
        if record is None or record.get("exception") is not None:
            texto = redigir_texto(_limitar_frames(texto))
    except Exception as erro:
        texto = _linha_de_falha(record, erro)
    sys.stderr.write(texto)


def instalar_redacao() -> None:
    """
    Instala a barreira. Idempotente -- chamar de novo não duplica o sink.

    A idempotência é por **objeto de módulo**, não por processo. Se o corpo
    deste arquivo executar duas vezes -- reimport com `sys.modules` manipulado,
    `importlib.reload`, ou duas identidades de módulo para o mesmo arquivo --, o
    segundo objeto entra com `_redacao_instalada = False`, dá `logger.remove()`
    no sink do primeiro e instala os seus. A barreira continua de pé (a nova
    substitui a antiga), mas quem guardou referência às funções antigas fica com
    referência morta.

    Em produção isso não acontece: `src/main.py` importa uma vez só. Na suíte
    acontece, e é por isso que `test_log_redaction.py` reinstala a barreira a
    partir do próprio objeto de módulo antes de cada teste -- sem isso os testes
    de fail-closed passam isolados e falham na suíte completa.

    `LOG_LEVEL` (default INFO) passa a valer de fato: o handler default do
    loguru é DEBUG, então tudo que era `logger.debug` estava saindo em staging e
    produção. Isso é defesa em profundidade, não o controle -- nível de log é
    configurável em runtime e nunca foi controle de acesso. O controle é a
    redação.
    """
    global _redacao_instalada
    if _redacao_instalada:
        return

    logger.configure(patcher=_redigir_record)
    # Resolvido antes do `remove()`: se o nível for inválido, o aviso ainda tem
    # o handler default para sair -- e já sai redigido, porque o patcher está de
    # pé desde a linha acima.
    nivel = _nivel_de_log()
    logger.remove()
    logger.add(
        _sink_redigido,
        level=nivel,
        format=FORMATO,
        backtrace=False,
        diagnose=False,
    )
    _redacao_instalada = True


instalar_redacao()
