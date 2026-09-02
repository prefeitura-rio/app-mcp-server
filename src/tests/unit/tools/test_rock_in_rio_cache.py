"""Testes do cache do line-up do Rock in Rio (CHATR-187).

O que estes testes protegem é uma regra de negócio, não uma otimização:
**preferimos não entregar a entregar dado desatualizado**. Mandar o cidadão para
o palco ou o dia errado é pior do que dizer que a consulta falhou. Por isso o
teste central aqui é o que exige uma exceção quando a fonte cai e o cache já
passou do teto de idade.
"""

import asyncio
import time

import pytest

from src.tools.rock_in_rio import cache as cache_mod
from src.tools.rock_in_rio import estado
from src.tools.rock_in_rio.cache import (
    LineupIndisponivel,
    intervalo_refresh_s,
    obter_lineup,
    resetar_cache,
)
from src.tools.rock_in_rio.scraper import LineupInvalido, Show

# Capturados antes de qualquer teste rodar: a fixture `cache_limpo` substitui os
# dois por dublês, e o teste de resiliência do Redis precisa dos originais.
_LER_REDIS_ORIGINAL = cache_mod._ler_redis
_GRAVAR_REDIS_ORIGINAL = cache_mod._gravar_redis

SHOW = Show(
    data="2026-09-04",
    dia_slug="04-set",
    palco="Palco Mundo",
    artista="FOO FIGHTERS",
    slug="foo-fighters",
    url="https://rockinrio.com/rio/pt-br/line-up/foo-fighters/",
)


@pytest.fixture(autouse=True)
def cache_limpo(monkeypatch):
    """Isola cada teste: sem cache de processo e sem tocar no Redis real."""
    resetar_cache()
    monkeypatch.setattr(cache_mod, "_ler_redis", _redis_vazio)
    monkeypatch.setattr(cache_mod, "_gravar_redis", _redis_ignora)
    yield
    resetar_cache()


async def _redis_vazio():
    return None


async def _redis_ignora(registro):
    return None


class FonteFalsa:
    """Substitui `buscar_lineup`, contando quantas vezes foi à fonte."""

    def __init__(self, *, falha: Exception | None = None):
        self.chamadas = 0
        self.falha = falha

    async def __call__(self):
        self.chamadas += 1
        if self.falha is not None:
            raise self.falha
        return [SHOW]


def _instalar_fonte(monkeypatch, fonte: FonteFalsa) -> FonteFalsa:
    monkeypatch.setattr(cache_mod, "buscar_lineup", fonte)
    return fonte


def _semear_memoria(*, idade_s: float) -> None:
    cache_mod._memoria = {
        "gerado_em_epoch": time.time() - idade_s,
        "shows": [SHOW.to_dict()],
    }


@pytest.mark.asyncio
async def test_primeira_chamada_busca_e_segunda_usa_o_cache(monkeypatch):
    fonte = _instalar_fonte(monkeypatch, FonteFalsa())

    primeira = await obter_lineup()
    segunda = await obter_lineup()

    assert fonte.chamadas == 1
    assert primeira.origem == "site"
    assert segunda.origem == "memoria"
    assert segunda.shows[0]["artista"] == "FOO FIGHTERS"


@pytest.mark.asyncio
async def test_cache_alem_do_teto_e_revalidado(monkeypatch):
    fonte = _instalar_fonte(monkeypatch, FonteFalsa())
    _semear_memoria(idade_s=cache_mod.DEFAULT_MAX_IDADE_S + 60)

    carregado = await obter_lineup()

    assert fonte.chamadas == 1
    assert carregado.origem == "site"


@pytest.mark.asyncio
async def test_forcar_ignora_cache_fresco(monkeypatch):
    fonte = _instalar_fonte(monkeypatch, FonteFalsa())
    _semear_memoria(idade_s=1)

    carregado = await obter_lineup(forcar=True)

    assert fonte.chamadas == 1
    assert carregado.origem == "site"


@pytest.mark.asyncio
async def test_fonte_fora_do_ar_serve_cache_dentro_do_teto(monkeypatch):
    """Queda curta do site durante o festival não pode virar erro."""
    _instalar_fonte(monkeypatch, FonteFalsa(falha=RuntimeError("site fora do ar")))
    _semear_memoria(idade_s=cache_mod.DEFAULT_MAX_IDADE_S - 300)

    carregado = await obter_lineup(forcar=True)

    assert carregado.origem == "cache_stale"
    assert carregado.shows[0]["artista"] == "FOO FIGHTERS"


@pytest.mark.asyncio
async def test_fonte_fora_do_ar_e_cache_vencido_levanta(monkeypatch):
    """A regra de negócio central: nada além do teto sai daqui.

    Com a fonte fora do ar e o cache já vencido, a resposta correta é falhar —
    não servir a grade antiga.
    """
    _instalar_fonte(monkeypatch, FonteFalsa(falha=RuntimeError("site fora do ar")))
    _semear_memoria(idade_s=cache_mod.DEFAULT_MAX_IDADE_S + 1)

    with pytest.raises(LineupIndisponivel):
        await obter_lineup()


@pytest.mark.asyncio
async def test_fonte_fora_do_ar_sem_cache_nenhum_levanta(monkeypatch):
    _instalar_fonte(monkeypatch, FonteFalsa(falha=RuntimeError("site fora do ar")))

    with pytest.raises(LineupIndisponivel):
        await obter_lineup()


@pytest.mark.asyncio
async def test_registro_do_redis_alimenta_o_cache_de_processo(monkeypatch):
    fonte = _instalar_fonte(monkeypatch, FonteFalsa())

    async def redis_com_dado():
        return {
            "gerado_em_epoch": time.time() - 60,
            "shows": [SHOW.to_dict()],
        }

    monkeypatch.setattr(cache_mod, "_ler_redis", redis_com_dado)

    do_redis = await obter_lineup()
    da_memoria = await obter_lineup()

    assert fonte.chamadas == 0
    assert do_redis.origem == "redis"
    assert da_memoria.origem == "memoria"


@pytest.mark.asyncio
async def test_redis_indisponivel_nao_derruba_a_tool(monkeypatch):
    """O cache de processo existe justamente para este caso.

    Aqui os wrappers reais de Redis voltam ao lugar (a fixture os substitui por
    dublês), porque o que está sob teste é a resiliência deles próprios: um
    cliente que explode ao ser criado não pode escapar para quem chamou.
    """
    fonte = _instalar_fonte(monkeypatch, FonteFalsa())
    monkeypatch.setattr(cache_mod, "_ler_redis", _LER_REDIS_ORIGINAL)
    monkeypatch.setattr(cache_mod, "_gravar_redis", _GRAVAR_REDIS_ORIGINAL)

    async def redis_explode():
        raise ConnectionError("redis fora do ar")

    # Importado aqui dentro, e não no topo: `_ler_redis` também resolve o
    # cliente por import tardio, para manter este módulo leve.
    from src.utils import bigquery as bigquery_mod

    monkeypatch.setattr(bigquery_mod, "get_async_redis_client", redis_explode)

    carregado = await obter_lineup()

    assert fonte.chamadas == 1
    assert carregado.origem == "site"


@pytest.mark.asyncio
async def test_chamadas_concorrentes_vao_a_fonte_uma_vez_so(monkeypatch):
    """Cache frio com várias conversas ao mesmo tempo não pode virar enxurrada.

    Sem a trava, cada chamada concorrente dispararia o download dos sete dias.
    """

    class FonteLenta(FonteFalsa):
        async def __call__(self):
            self.chamadas += 1
            await asyncio.sleep(0.05)
            return [SHOW]

    fonte = _instalar_fonte(monkeypatch, FonteLenta())

    resultados = await asyncio.gather(*(obter_lineup() for _ in range(5)))

    assert fonte.chamadas == 1
    assert all(r.shows[0]["artista"] == "FOO FIGHTERS" for r in resultados)


def test_intervalo_de_refresh_e_limitado_ao_teto_de_idade(monkeypatch):
    """Revalidar depois do vencimento abriria janelas sem dado servível."""
    monkeypatch.setenv("ROCK_IN_RIO_MAX_IDADE_S", "600")
    monkeypatch.setenv("ROCK_IN_RIO_REFRESH_INTERVAL_S", "9000")

    assert intervalo_refresh_s() == 600.0


def test_intervalo_de_refresh_invalido_cai_no_padrao(monkeypatch):
    monkeypatch.setenv("ROCK_IN_RIO_REFRESH_INTERVAL_S", "nao-e-numero")

    assert intervalo_refresh_s() == cache_mod.DEFAULT_REFRESH_INTERVAL_S


def test_intervalo_de_refresh_padrao_e_menor_que_o_teto():
    """O desenho depende disso: revalidar antes de o dado vencer."""
    assert cache_mod.DEFAULT_REFRESH_INTERVAL_S < cache_mod.DEFAULT_MAX_IDADE_S


@pytest.mark.asyncio
async def test_fallback_serve_a_memoria_sem_ir_ao_redis(monkeypatch):
    """Com a fonte fora, o Redis só é consultado se a memória não servir.

    As duas cópias estariam igualmente dentro do teto, que é o contrato; pagar
    um round-trip (até o `socket_timeout`) para escolher a mais nova entre elas
    é justamente o que não vale a pena na requisição em que a fonte já falhou.
    """
    fonte = _instalar_fonte(monkeypatch, FonteFalsa(falha=ConnectionError("fora")))
    _semear_memoria(idade_s=30)

    consultas = 0

    async def contar_ler_redis():
        nonlocal consultas
        consultas += 1
        return None

    monkeypatch.setattr(cache_mod, "_ler_redis", contar_ler_redis)

    carregado = await obter_lineup(forcar=True)

    assert carregado.origem == "cache_stale"
    assert fonte.chamadas == 1
    assert consultas == 0


@pytest.mark.asyncio
async def test_shows_devolvidos_nao_sao_a_lista_do_cache(monkeypatch):
    """A resposta da tool não pode dar acesso de escrita ao cache do processo."""
    _instalar_fonte(monkeypatch, FonteFalsa())

    primeira = await obter_lineup()
    primeira.shows.clear()

    segunda = await obter_lineup()

    assert segunda.origem == "memoria"
    assert segunda.shows[0]["artista"] == "FOO FIGHTERS"


@pytest.mark.asyncio
async def test_aquecer_lineup_carrega_no_startup(monkeypatch):
    fonte = _instalar_fonte(monkeypatch, FonteFalsa())

    assert await cache_mod.aquecer_lineup() is True
    assert fonte.chamadas == 1


@pytest.mark.asyncio
async def test_aquecer_lineup_nao_derruba_o_boot(monkeypatch):
    """Fonte fora do ar no boot é transitória.

    Deixar a exceção subir converteria degradação parcial em indisponibilidade
    total — o pod não subiria por causa de um site de terceiro.
    """
    _instalar_fonte(monkeypatch, FonteFalsa(falha=ConnectionError("fora")))

    assert await cache_mod.aquecer_lineup() is False


@pytest.mark.asyncio
async def test_laco_de_refresh_sobrevive_a_falha_da_fonte(monkeypatch):
    """O laço precisa atravessar uma indisponibilidade para se recuperar depois.

    Se morresse no primeiro erro, o pod ficaria sem revalidar até o próximo
    deploy e o dado venceria o teto de idade em silêncio.
    """
    fonte = _instalar_fonte(monkeypatch, FonteFalsa(falha=ConnectionError("fora")))
    monkeypatch.setattr(cache_mod, "intervalo_refresh_s", lambda: 0.01)

    tarefa = asyncio.create_task(cache_mod.run_refresh_loop())
    await asyncio.sleep(0.08)
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa

    assert fonte.chamadas >= 2


def test_ttl_do_redis_nunca_e_zero(monkeypatch):
    """`SET ... EX 0` é erro no Redis.

    Um teto configurado abaixo de um segundo zerava o TTL e fazia toda gravação
    falhar dentro do `except` de `_gravar_redis` — o cache compartilhado nunca
    chegaria a se formar, e ninguém veria o motivo.
    """
    monkeypatch.setenv("ROCK_IN_RIO_MAX_IDADE_S", "0.5")

    assert cache_mod._ttl_redis_s() == 1


# --------------------------------------------------------------------------
# Veredito do ciclo e alerta de transição
#
# Estes testes existem por causa de um incidente concreto: em 01/09/2026 o site
# mudou de estrutura, a tool ficou fora do ar e nada acusou. O que se protege
# aqui não é o cache, é o **sinal** — que a falha fique registrada, classificada
# pelo modo certo, e que o alerta saia uma vez só.
# --------------------------------------------------------------------------


@pytest.fixture
def interceptor_falso(monkeypatch):
    """Captura as chamadas ao error interceptor, sem sair para a rede."""
    from src.utils import error_interceptor

    chamadas = []

    async def _capturar(**kwargs):
        chamadas.append(kwargs)
        return True

    monkeypatch.setattr(error_interceptor, "send_general_error", _capturar)
    return chamadas


@pytest.mark.asyncio
async def test_ciclo_bem_sucedido_registra_o_veredito(monkeypatch):
    _instalar_fonte(monkeypatch, FonteFalsa())

    await obter_lineup()

    resultado = estado.ultimo_resultado()
    assert resultado is not None
    assert resultado.sucesso is True
    assert resultado.atracoes == 1
    assert resultado.palcos == 1
    assert resultado.falha is None


@pytest.mark.asyncio
async def test_falha_de_formato_e_classificada_como_formato(monkeypatch):
    """`LineupInvalido` é "o site mudou": exige código, não passa sozinho.

    A classificação é o que torna o alerta acionável — sem ela, quem for
    acordado não sabe se espera o próximo ciclo ou abre o parser.
    """
    _instalar_fonte(monkeypatch, FonteFalsa(falha=LineupInvalido("formato")))

    with pytest.raises(LineupIndisponivel):
        await obter_lineup()

    resultado = estado.ultimo_resultado()
    assert resultado.sucesso is False
    assert resultado.falha == estado.FALHA_FORMATO
    assert resultado.detalhe == "LineupInvalido"


@pytest.mark.asyncio
async def test_falha_de_rede_e_classificada_como_fonte(monkeypatch):
    _instalar_fonte(monkeypatch, FonteFalsa(falha=ConnectionError("fora")))

    with pytest.raises(LineupIndisponivel):
        await obter_lineup()

    resultado = estado.ultimo_resultado()
    assert resultado.sucesso is False
    assert resultado.falha == estado.FALHA_FONTE
    assert resultado.detalhe == "ConnectionError"


@pytest.mark.asyncio
async def test_instrumentacao_nao_muda_o_fallback_para_o_cache(monkeypatch):
    """O registro do veredito não pode alterar o que o cidadão recebe.

    A regra de negócio continua sendo a mesma: com a fonte fora do ar e cache
    dentro do teto, serve-se o cache. O sinal é para quem opera, não para quem
    pergunta.
    """
    _instalar_fonte(monkeypatch, FonteFalsa(falha=ConnectionError("fora")))
    _semear_memoria(idade_s=60)

    carregado = await obter_lineup(forcar=True)

    assert carregado.origem == "cache_stale"
    assert carregado.shows[0]["artista"] == "FOO FIGHTERS"
    assert estado.ultimo_resultado().falha == estado.FALHA_FONTE


@pytest.mark.asyncio
async def test_resetar_cache_limpa_o_veredito(monkeypatch):
    _instalar_fonte(monkeypatch, FonteFalsa())
    await obter_lineup()
    assert estado.ultimo_resultado() is not None

    resetar_cache()

    assert estado.ultimo_resultado() is None


@pytest.mark.asyncio
async def test_alerta_sai_uma_vez_na_virada_e_nao_a_cada_ciclo(
    monkeypatch, interceptor_falso
):
    """O laço roda de 15 em 15 min: alertar por ciclo seria ~96 reports/dia.

    Mesmo desenho de `_alert_transition` em `src/health/external_tables.py`.
    """
    _instalar_fonte(monkeypatch, FonteFalsa(falha=LineupInvalido("formato")))

    for _ in range(3):
        with pytest.raises(LineupIndisponivel):
            await obter_lineup(forcar=True)

    assert len(interceptor_falso) == 1
    assert interceptor_falso[0]["error_type"] == "LineupFormatoMudou"
    assert "correção de código" in interceptor_falso[0]["error_message"]


@pytest.mark.asyncio
async def test_alerta_de_fonte_diz_que_e_transitorio(monkeypatch, interceptor_falso):
    _instalar_fonte(monkeypatch, FonteFalsa(falha=ConnectionError("fora")))

    with pytest.raises(LineupIndisponivel):
        await obter_lineup(forcar=True)

    assert interceptor_falso[0]["error_type"] == "LineupFonteIndisponivel"


@pytest.mark.asyncio
async def test_recuperacao_nao_gera_alerta(monkeypatch, interceptor_falso):
    """Voltar a funcionar é log, não alerta — ninguém precisa ser acordado."""
    fonte = FonteFalsa(falha=ConnectionError("fora"))
    _instalar_fonte(monkeypatch, fonte)
    with pytest.raises(LineupIndisponivel):
        await obter_lineup(forcar=True)
    assert len(interceptor_falso) == 1

    fonte.falha = None
    await obter_lineup(forcar=True)

    assert len(interceptor_falso) == 1
    assert estado.ultimo_resultado().sucesso is True


@pytest.mark.asyncio
async def test_interceptor_fora_do_ar_nao_engole_a_excecao_original(monkeypatch):
    """Observabilidade nunca pode virar a causa da falha.

    Se o interceptor cair, o que precisa continuar subindo é o erro do site —
    senão a instrumentação transforma uma queda diagnosticável numa outra.
    """
    from src.utils import error_interceptor

    async def _explodir(**kwargs):
        raise RuntimeError("interceptor fora do ar")

    monkeypatch.setattr(error_interceptor, "send_general_error", _explodir)
    _instalar_fonte(monkeypatch, FonteFalsa(falha=LineupInvalido("formato")))

    with pytest.raises(LineupIndisponivel):
        await obter_lineup(forcar=True)

    assert estado.ultimo_resultado().falha == estado.FALHA_FORMATO


@pytest.mark.asyncio
async def test_cancelamento_nao_vira_falha_da_fonte(monkeypatch, interceptor_falso):
    """O shutdown do pod cancela a task no meio do ciclo.

    Tratar isso como queda inventaria uma falha a cada deploy que pegasse um
    ciclo em andamento — e dispararia o alerta de transição por ela.
    """
    _instalar_fonte(monkeypatch, FonteFalsa(falha=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await obter_lineup(forcar=True)

    assert estado.ultimo_resultado() is None
    assert interceptor_falso == []
