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

    Falha fechado, pelo mesmo motivo de `_redigir_record`.
    """
    record = getattr(mensagem, "record", None)
    try:
        texto = str(mensagem)
        if record is None or record.get("exception") is not None:
            texto = redigir_texto(texto)
    except Exception as erro:
        texto = _linha_de_falha(record, erro)
    sys.stderr.write(texto)


def instalar_redacao() -> None:
    """
    Instala a barreira. Idempotente -- chamar de novo não duplica o sink.

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
