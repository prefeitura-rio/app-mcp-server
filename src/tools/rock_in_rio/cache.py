"""Cache do line-up do Rock in Rio, em dois níveis (CHATR-187).

A regra de negócio que desenha este módulo é uma só: **preferimos não entregar a
entregar dado desatualizado**. Mandar o cidadão para o palco errado, ou para o
dia errado, é pior do que dizer que a informação está indisponível no momento.

Disso decorre tudo o mais:

- Existe um teto duro de idade (`ROCK_IN_RIO_MAX_IDADE_S`, 60 min). Nada mais
  velho que isso sai daqui — sai uma exceção.
- A atualização roda em background num intervalo *menor* que o teto (15 min).
  Se a revalidação só acontecesse no vencimento, o dado expiraria e já estaria
  velho demais no mesmo instante, e a tolerância a uma queda curta do site
  nasceria zerada. Revalidar antes é o que transforma o teto em teto de verdade.
- Não há snapshot versionado como último recurso. Um arquivo commitado semanas
  antes é justamente o dado desatualizado que a regra proíbe.

A cadeia de degradação, portanto, é: site → cache dentro do teto → falha
explícita. E são dois níveis de cache porque o Redis é infraestrutura externa: o
processo guarda a sua própria cópia na frente, então um Redis indisponível deixa
a tool mais lenta para aquecer, nunca fora do ar.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.tools.rock_in_rio.scraper import Show, buscar_lineup
from src.utils.infisical import getenv_or_action
from src.utils.log import logger

# Chave do Redis. O sufixo de versão existe para que uma mudança no formato do
# registro não seja lida por pods que ainda rodam a versão anterior do código.
# Foi para `v2` quando `url` e `dia_slug` saíram de cada show (ver
# `Show.para_resposta`): durante um rollout, as duas formas conviveriam na
# mesma chave.
CHAVE_REDIS = "rock_in_rio:lineup:v2"

DEFAULT_MAX_IDADE_S = 3600.0
DEFAULT_REFRESH_INTERVAL_S = 900.0


class LineupIndisponivel(RuntimeError):
    """Não há dado dentro do teto de idade, e a fonte não respondeu.

    Deliberadamente um erro, e não um retorno vazio ou antigo: quem chama
    precisa dizer ao cidadão que a informação está indisponível.
    """


@dataclass(frozen=True)
class LineupCarregado:
    """Grade completa mais a procedência, que a resposta expõe ao cidadão."""

    shows: List[Dict[str, str]]
    gerado_em_epoch: float
    origem: str

    @property
    def idade_s(self) -> float:
        return max(0.0, time.time() - self.gerado_em_epoch)


# Configuração já resolvida, guardada por processo. `getenv_or_action` cai no
# `.env` da raiz quando a variável não está no ambiente, e esse caminho refaz um
# `Path(".env").exists()` a cada chamada — que aqui aconteceria várias vezes por
# requisição, porque `_dentro_do_teto` consulta o teto toda vez que roda. Os
# valores não mudam em runtime; `resetar_cache` limpa junto, para os testes
# poderem trocá-los.
_config_cache: Dict[str, float] = {}


def _config_float(nome: str, padrao: float) -> float:
    """Lê configuração numérica tolerando valor ausente ou inválido.

    Lida aqui e não em `src/config/env.py` pelo mesmo motivo documentado em
    `src/health/external_tables.py`: mantém o módulo importável isoladamente
    nos testes, sem exigir o ambiente inteiro do servidor.
    """
    if nome not in _config_cache:
        _config_cache[nome] = _ler_config_float(nome, padrao)
    return _config_cache[nome]


def _ler_config_float(nome: str, padrao: float) -> float:
    bruto = getenv_or_action(nome, default=str(padrao), action="ignore")
    try:
        valor = float(bruto)
    except (TypeError, ValueError):
        logger.warning(f"{nome} inválido ({bruto!r}); usando o padrão de {padrao}s.")
        return padrao
    if valor <= 0:
        logger.warning(f"{nome}={valor} não é positivo; usando o padrão de {padrao}s.")
        return padrao
    return valor


def max_idade_s() -> float:
    """Teto duro de idade do dado servido."""
    return _config_float("ROCK_IN_RIO_MAX_IDADE_S", DEFAULT_MAX_IDADE_S)


def _ttl_redis_s() -> int:
    """TTL do registro no Redis, espelhando o teto de idade.

    Piso de 1s porque `SET ... EX 0` é erro no Redis: um teto configurado abaixo
    de um segundo faria toda gravação falhar dentro do `except` de
    `_gravar_redis`, e o cache compartilhado nunca chegaria a se formar.
    """
    return max(1, int(max_idade_s()))


def intervalo_refresh_s() -> float:
    """Intervalo do laço de atualização em background.

    Elevado ao teto de idade se for configurado acima dele: um intervalo maior
    que o teto garantiria janelas em que o dado expira antes da próxima
    revalidação, que é exatamente o buraco que este desenho evita.
    """
    intervalo = _config_float(
        "ROCK_IN_RIO_REFRESH_INTERVAL_S", DEFAULT_REFRESH_INTERVAL_S
    )
    teto = max_idade_s()
    if intervalo > teto:
        logger.warning(
            f"ROCK_IN_RIO_REFRESH_INTERVAL_S={intervalo}s é maior que o teto de "
            f"idade ({teto}s); reduzindo para o teto."
        )
        return teto
    return intervalo


# Cache de processo. Fica na frente do Redis: é o que mantém a tool de pé quando
# o Redis está indisponível.
_memoria: Optional[Dict[str, Any]] = None

# Trava para não disparar sete downloads por chamada concorrente quando o cache
# está frio. Recriada quando o event loop muda: `asyncio.Lock` se prende ao loop
# em que é usada pela primeira vez, e um objeto de módulo sobreviveria entre os
# loops distintos que a suíte de testes cria.
_lock: Optional[asyncio.Lock] = None
_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _obter_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def resetar_cache() -> None:
    """Descarta o cache de processo. Existe para isolamento entre testes."""
    global _memoria
    _memoria = None
    _config_cache.clear()


def _dentro_do_teto(registro: Optional[Dict[str, Any]]) -> bool:
    if not registro:
        return False
    idade = time.time() - float(registro.get("gerado_em_epoch", 0.0))
    return 0 <= idade <= max_idade_s()


async def _ler_redis() -> Optional[Dict[str, Any]]:
    """Lê o registro compartilhado. Qualquer falha vira `None`, nunca exceção."""
    try:
        from src.utils.bigquery import get_async_redis_client

        client = await get_async_redis_client()
        if client is None:
            return None
        bruto = await client.get(CHAVE_REDIS)
        if not bruto:
            return None
        registro = json.loads(bruto)
        if not isinstance(registro, dict) or not registro.get("shows"):
            return None
        return registro
    except Exception as erro:
        logger.warning(f"Falha ao ler o line-up do Redis: {erro}")
        return None


async def _gravar_redis(registro: Dict[str, Any]) -> None:
    """Publica o registro para as demais réplicas. Falha não interrompe nada."""
    try:
        from src.utils.bigquery import get_async_redis_client

        client = await get_async_redis_client()
        if client is None:
            return
        # O TTL do Redis espelha o teto de idade: assim a própria infraestrutura
        # descarta o registro velho, e nenhuma réplica que suba depois encontra
        # lá dentro algo que a regra de negócio proíbe servir.
        await client.set(CHAVE_REDIS, json.dumps(registro), ex=_ttl_redis_s())
    except Exception as erro:
        logger.warning(f"Falha ao gravar o line-up no Redis: {erro}")


def _registro(shows: List[Show]) -> Dict[str, Any]:
    return {
        "gerado_em_epoch": time.time(),
        "shows": [show.para_resposta() for show in shows],
    }


def _carregado(registro: Dict[str, Any], origem: str) -> LineupCarregado:
    """Empacota o registro para entrega.

    A lista é copiada de propósito: `LineupCarregado.shows` vai direto para a
    resposta da tool, e devolver o próprio objeto do cache deixaria qualquer
    reordenação ou filtragem in-place feita mais adiante envenenar o cache de
    todas as requisições seguintes. Cópia rasa basta — os dicts de cada show
    ninguém escreve.
    """
    return LineupCarregado(list(registro["shows"]), registro["gerado_em_epoch"], origem)


async def _buscar_e_guardar() -> Dict[str, Any]:
    global _memoria
    shows = await buscar_lineup()
    registro = _registro(shows)
    _memoria = registro
    await _gravar_redis(registro)
    return registro


async def obter_lineup(*, forcar: bool = False) -> LineupCarregado:
    """Devolve a grade completa, dentro do teto de idade.

    Args:
        forcar: Ignora o cache e vai à fonte. Usado pelo laço de atualização.

    Raises:
        LineupIndisponivel: Sem dado dentro do teto e com a fonte fora do ar.
    """
    global _memoria

    if not forcar and _dentro_do_teto(_memoria):
        return _carregado(_memoria, "memoria")

    async with _obter_lock():
        # Outra corrotina pode ter preenchido o cache enquanto esta esperava a
        # trava; sem esta recheca, a espera na fila viraria um download a mais.
        if not forcar and _dentro_do_teto(_memoria):
            return _carregado(_memoria, "memoria")

        if not forcar:
            compartilhado = await _ler_redis()
            if _dentro_do_teto(compartilhado):
                _memoria = compartilhado
                return _carregado(compartilhado, "redis")

        try:
            registro = await _buscar_e_guardar()
            return _carregado(registro, "site")
        except Exception as erro:
            logger.warning(f"Falha ao buscar o line-up do Rock in Rio: {erro}")

            # Último recurso: qualquer dado que ainda respeite o teto. Uma queda
            # curta do site durante o festival não pode virar erro para o
            # cidadão — mas uma queda longa precisa virar, e vira.
            #
            # A memória é consultada primeiro e o Redis só se ela não servir. As
            # duas cópias estão igualmente dentro do teto, que é o contrato; ir
            # ao Redis para escolher a mais nova entre elas custaria um
            # round-trip (até o `socket_timeout`) justamente na requisição em que
            # a fonte já falhou. A idade real de quem for servido vai no
            # `atualizado_em` da resposta de qualquer forma.
            if _dentro_do_teto(_memoria):
                return _carregado(_memoria, "cache_stale")

            compartilhado = await _ler_redis()
            if _dentro_do_teto(compartilhado):
                return _carregado(compartilhado, "cache_stale")

            raise LineupIndisponivel(
                "Line-up do Rock in Rio indisponível: a fonte não respondeu e não "
                "há dado em cache dentro do teto de idade."
            ) from erro


async def aquecer_lineup() -> bool:
    """Carrega o line-up no startup para ninguém pagar o cache frio.

    Nunca levanta: uma fonte fora do ar no boot é transitória, e derrubar o pod
    por causa dela converteria degradação parcial em indisponibilidade total —
    mesmo critério já adotado pelo `health_lifespan` em `src/app.py`.
    """
    try:
        carregado = await obter_lineup(forcar=True)
        logger.info(
            f"Line-up do Rock in Rio aquecido no startup: {len(carregado.shows)} atrações"
        )
        return True
    except Exception:
        logger.exception("Falha ao aquecer o line-up do Rock in Rio no startup")
        return False


async def run_refresh_loop() -> None:
    """Revalida o line-up periodicamente, antes de o dado atingir o teto."""
    while True:
        await asyncio.sleep(intervalo_refresh_s())
        try:
            await obter_lineup(forcar=True)
        except asyncio.CancelledError:
            raise
        except Exception as erro:
            # Só loga: o laço precisa sobreviver a uma indisponibilidade
            # passageira do site para conseguir se recuperar no próximo ciclo.
            logger.warning(f"Ciclo de atualização do line-up falhou: {erro}")
