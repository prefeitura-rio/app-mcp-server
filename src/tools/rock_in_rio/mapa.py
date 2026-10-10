"""Tool MCP que entrega o mapa da Cidade do Rock por link curto.

O mapa é uma imagem pública hospedada pelo próprio Rock in Rio. Não a
re-hospedamos: o que este módulo controla é a URL, não os bytes por trás dela —
o terceiro pode trocar a imagem a qualquer momento, e de fato já sobrescreveu o
arquivo pelo menos uma vez depois de publicá-lo. A alternativa (copiar para o
bucket de workflows) é decisão de marca e de licenciamento, não de código.

A URL crua é longa demais para uma conversa de WhatsApp, então ela passa pelo
encurtador da Prefeitura — o mesmo caminho que o IPTU e a Dívida Ativa já usam
para guias (ver `docs/decisions/CHATR-176-gcs-e-encurtador-compartilhados.md`).
A diferença é que aqui o link é **um só para todos os cidadãos**: nada nele
depende de quem perguntou. Por isso ele é encurtado uma vez e guardado no Redis
até o fim do festival, em vez de um link novo por atendimento.

O link morre junto com o evento, e é o mesmo instante nos dois lugares:
`expires_at` no encurtador e TTL no Redis saem ambos de `limite_do_festival()`.
Depois dele a tool responde que a edição acabou, sem link — não há link válido
para oferecer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import httpx
from opentelemetry import trace

from src.config import env

# `_juntar` é privado por convenção de nome, mas é onde mora o espaçamento
# combinado com o PO (linha em branco entre os itens, que é o que separa os
# blocos no aplicativo). Reimplementá-lo aqui criaria uma segunda regra de
# formatação para manter em sincronia com a primeira.
from src.tools.rock_in_rio.mensagem import _juntar
from src.tools.rock_in_rio.tool import (
    LOCAL_DO_EVENTO,
    NOME_DO_EVENTO,
    ULTIMO_DIA,
    limite_do_festival,
)
from src.utils.datetime_utils import get_rio_timezone
from src.utils.log import logger
from src.utils.short_url import format_expires_at, get_short_url

URL_DO_MAPA = (
    "https://rockinrio.com/rio/wp-content/uploads/2026/05/"
    "bannermateria-desktop-mapao-1905-bc-v2.jpg"
)

# Sufixo de versão pelo mesmo motivo de `CHAVE_REDIS` em `cache.py`: se o
# formato do valor mudar, pods da versão anterior não leem o novo.
CHAVE_REDIS = "rock_in_rio:mapa:v1"

# Caminho curto fixo, e não gerado pelo encurtador: o link é único e vale o
# evento inteiro, então um caminho determinístico faz com que perder o Redis
# (eviction, restart do cluster) não gere um link novo, e faz com que várias
# réplicas subindo ao mesmo tempo não criem uma penca de links para a mesma URL.
SHORT_PATH = "mapa-rock-in-rio"

TITULO = "mapa_rock_in_rio"
DESCRICAO = "Mapa do Rock In Rio"

# O encurtamento não é de ninguém em particular: o link é o mesmo para todos os
# cidadãos e acontece uma vez por evento. Atribuir a operação a quem por acaso
# causou o cache miss enganaria quem lê o SigNoz procurando o atendimento.
USER_ID = "sistema"

# Teto da confirmação de link existente. Curto de propósito: ela roda no meio
# de um atendimento, e o fallback (a URL longa) é aceitável — esperar não é.
TIMEOUT_CONFIRMACAO_S = 5.0

# `source` é do chamador, não derivado dentro do helper — CHATR-176 §3.
ERROR_SOURCE = {"source": "mcp", "tool": "rock_in_rio_mapa"}

# Tool irmã, publicada por este mesmo servidor. Citada na descrição para separar
# as duas perguntas: mapa é aqui, programação é lá.
TOOL_DO_LINEUP = "rock_in_rio_lineup"

_INSTRUCOES_DE_RESPOSTA = (
    "Responda copiando o texto de `mensagem` como está, com o link na própria "
    "linha — ele já vem pronto para o WhatsApp. Não reescreva a frase e não "
    "encurte o link de novo. NÃO descreva o que o mapa mostra: esta resposta "
    "não traz o conteúdo da imagem, e o que está desenhado nela só o cidadão "
    "consegue conferir abrindo o link."
)

_INSTRUCOES_ENCERRADO = (
    "Esta edição do festival já terminou e o link do mapa saiu do ar. Responda "
    "copiando o texto de `mensagem` e não ofereça mapa — não existe link "
    "válido para oferecer."
)


def _marcar_origem(origem: str) -> None:
    """Registra no span de onde saiu o link desta chamada.

    `redis` é o caminho normal; `encurtador` deve aparecer uma vez por evento;
    `confirmado` é o Redis perdido com o link já publicado, recuperado pelo GET
    de `_link_ja_publicado`; `url_crua` significa encurtador fora do ar, e é o
    que se filtra para saber que o cidadão recebeu a URL longa.

    Mesmo critério de `_marcar_degradacao` em `tool.py`: observabilidade nunca
    derruba o caminho principal, e `get_current_span()` sem span ativo devolve
    um objeto no-op.
    """
    try:
        span = trace.get_current_span()
        span.set_attribute("rock_in_rio.mapa.origem", origem)
    except Exception:  # noqa: BLE001
        pass


async def _ler_redis() -> Optional[str]:
    """Lê o link curto compartilhado. Qualquer falha vira `None`, nunca exceção."""
    try:
        from src.utils.bigquery import get_async_redis_client

        client = await get_async_redis_client()
        if client is None:
            return None
        return await client.get(CHAVE_REDIS) or None
    except Exception as erro:  # noqa: BLE001
        logger.warning(f"Falha ao ler o link do mapa no Redis: {erro}")
        return None


async def _gravar_redis(link: str, ttl_s: int) -> None:
    """Publica o link para as demais réplicas. Falha não interrompe nada."""
    try:
        from src.utils.bigquery import get_async_redis_client

        client = await get_async_redis_client()
        if client is None:
            return
        await client.set(CHAVE_REDIS, link, ex=ttl_s)
    except Exception as erro:  # noqa: BLE001
        logger.warning(f"Falha ao gravar o link do mapa no Redis: {erro}")


def _ttl_s(agora: datetime, limite: datetime) -> int:
    """Segundos até o fim do festival, com piso de 1s.

    O piso existe pelo mesmo motivo de `_ttl_redis_s` em `cache.py`: `SET ... EX
    0` é erro no Redis. Aqui a janela só encolheria abaixo de um segundo no
    último instante antes do limite, e o guard de `get_mapa_rock_in_rio` já
    barra o caso encerrado — mas o piso custa nada e tira o caminho de falha.
    """
    return max(1, int((limite - agora).total_seconds()))


async def _link_ja_publicado() -> Optional[str]:
    """Confirma com um GET se o link do `short_path` fixo já está no ar.

    `get_short_url` devolve `None` para dois desfechos opostos: 409 ("esse
    short_path já existe" — o link certo está publicado e é só usá-lo) e 5xx (o
    encurtador caiu — não há link nenhum, e montar a URL entregaria um link
    morto). Este GET é o que separa os dois, e sem ele perder o Redis no meio do
    festival condenaria todo mundo à URL longa até o evento acabar: a
    re-encurtagem bate no 409 para sempre.

    `HEAD` não serve — o encurtador responde 404 a HEAD mesmo para link válido.
    Em `GET`, 200 é o link no ar e 404 traz `URL not found or expired`, que é a
    mesma resposta para inexistente e para vencido; os dois pedem o fallback.
    """
    curto = f"{env.SHORT_API_URL}/link/{SHORT_PATH}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_CONFIRMACAO_S) as client:
            resposta = await client.get(curto)
    except Exception as erro:  # noqa: BLE001 - sem confirmação, cai no fallback
        logger.warning(f"Falha ao confirmar o link do mapa: {erro}")
        return None
    return curto if resposta.status_code == 200 else None


async def _obter_link(agora: datetime) -> Tuple[str, str]:
    """Devolve `(link, origem)` — o link a publicar e de onde ele veio."""
    cacheado = await _ler_redis()
    if cacheado:
        return cacheado, "redis"

    limite = limite_do_festival()
    curto = await get_short_url(
        url=URL_DO_MAPA,
        title=TITULO,
        description=DESCRICAO,
        user_id=USER_ID,
        source=ERROR_SOURCE,
        expires_at=format_expires_at(limite),
        image_url=URL_DO_MAPA,
        short_path=SHORT_PATH,
    )

    if not curto:
        # `get_short_url` engole o erro e devolve `None` sem dizer qual foi.
        # Confirmar é o que separa "já existe" (409) de "encurtador fora do ar",
        # em vez de montar a URL às cegas e arriscar entregar um link morto.
        publicado = await _link_ja_publicado()
        if publicado:
            await _gravar_redis(publicado, _ttl_s(agora, limite))
            return publicado, "confirmado"
        # Sem link no ar: a URL longa funciona, e um link morto não. Nada é
        # cacheado — a próxima chamada tenta encurtar de novo.
        return URL_DO_MAPA, "url_crua"

    await _gravar_redis(curto, _ttl_s(agora, limite))
    return curto, "encurtador"


def _bloco_do_mapa(link: str) -> str:
    """Mensagem pronta para ser copiada na conversa."""
    return _juntar(
        [
            f"Este é o mapa da Cidade do Rock, onde acontece o {NOME_DO_EVENTO}:",
            link,
        ]
    )


def _resposta_encerrada() -> Dict[str, Any]:
    """Resposta depois do fim do festival, sem link.

    O link curto expira junto com o evento, então oferecer mapa aqui seria
    oferecer um link morto.
    """
    return {
        "disponivel": False,
        "motivo": "evento_encerrado",
        "evento": {"nome": NOME_DO_EVENTO, "local": LOCAL_DO_EVENTO},
        "mensagem": (
            f"O {NOME_DO_EVENTO} terminou em {ULTIMO_DIA.strftime('%d/%m/%Y')}."
        ),
        "instrucoes_de_resposta": _INSTRUCOES_ENCERRADO,
    }


async def get_mapa_rock_in_rio() -> Dict[str, Any]:
    """Devolve o mapa da Cidade do Rock como link pronto para a conversa.

    O link vive só dentro de `mensagem`, sem campo estruturado próprio: é o
    mesmo desenho de `texto_por_dia`/`texto_por_palco` na tool de line-up, onde
    o bloco já formatado é o que o modelo copia.
    """
    agora = datetime.now(get_rio_timezone())

    # Antes de tocar em Redis ou encurtador: depois do limite não há link para
    # servir, e é este guard que também garante que o TTL do Redis nunca seja
    # calculado sobre uma janela negativa.
    if agora >= limite_do_festival():
        _marcar_origem("encerrado")
        return _resposta_encerrada()

    link, origem = await _obter_link(agora)
    _marcar_origem(origem)

    return {
        "disponivel": True,
        "evento": {"nome": NOME_DO_EVENTO, "local": LOCAL_DO_EVENTO},
        "mensagem": _bloco_do_mapa(link),
        "instrucoes_de_resposta": _INSTRUCOES_DE_RESPOSTA,
    }


def descricao_da_tool_mapa(tool_version: str) -> str:
    """Descrição publicada no catálogo MCP.

    Diz explicitamente o que esta tool NÃO é: sem a fronteira, "onde fica o
    Palco Mundo" tem chance de cair aqui em vez de na tool de line-up, que é
    quem tem os palcos.
    """
    return (
        f"[TOOL_VERSION: {tool_version}] Devolve o link do mapa oficial da "
        f"Cidade do Rock, onde acontece o {NOME_DO_EVENTO}.\n\n"
        "Use quando o cidadão pedir o mapa do evento, ou quiser ver como o "
        "espaço é dividido e onde ficam os palcos e as áreas dentro da Cidade "
        "do Rock.\n\n"
        "NÃO use para programação: quem toca, em que dia e em que palco é a "
        f"tool `{TOOL_DO_LINEUP}`.\n\n"
        "A resposta traz o texto pronto em `mensagem` — copie como está. O mapa "
        "é uma imagem, e esta tool não devolve o conteúdo dela: nunca afirme o "
        "que o mapa mostra nem descreva o que está desenhado nele."
    )
