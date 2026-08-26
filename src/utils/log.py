"""
Configuração global de log (CHATR-167).

Este módulo é a barreira: toda linha de `logger.*` do processo passa por
redação antes de chegar a qualquer sink. Até o CHATR-167 ele só ajustava o
nível de `httpx`/`httpcore` -- não instalava sink, patcher nem filtro --, e o
resultado é que **nenhuma** linha de log era redigida: CPF, nome, endereço e
segredo de OAuth2 iam íntegros para o SigNoz.

São duas camadas, e cada uma pega o que a outra não alcança:

1. `patcher` -- roda antes de todo sink, inclusive um que um teste adicione, e
   enxerga a mensagem ainda estruturada (`logger.info({...})` chega aqui com o
   dict intacto), o que permite redigir pela chave e não só pelo formato do
   valor.
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
from typing import Any, Dict

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

_redacao_instalada = False


def _redigir_record(record: Dict[str, Any]) -> None:
    """
    Redige a mensagem e os campos extras antes de qualquer sink.

    Nunca levanta: uma linha de log mal formada vale menos do que a chamada que
    a produziu, e um patcher que estoura derruba o `logger.*` de quem chamou.
    """
    try:
        mensagem = record["message"]
        if isinstance(mensagem, str):
            record["message"] = redigir_texto(mensagem)
        else:
            # `logger.info({"parameters": ...})` chega aqui com o dict de pé --
            # é a única janela em que dá para redigir pela chave.
            record["message"] = redigir_texto(str(redigir_estrutura(mensagem)))

        extra = record.get("extra")
        if extra:
            record["extra"] = redigir_estrutura(extra)
    except Exception:
        pass


def _sink_redigido(mensagem: Any) -> None:
    """Segunda passada, sobre o texto final: pega `str(exception)` e traceback."""
    try:
        texto = redigir_texto(str(mensagem))
    except Exception:
        texto = str(mensagem)
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
    logger.remove()
    logger.add(
        _sink_redigido,
        level=Settings.LOG_LEVEL,
        format=FORMATO,
        backtrace=False,
        diagnose=False,
    )
    _redacao_instalada = True


instalar_redacao()
