"""Tool MCP que devolve o line-up do Rock in Rio 2026 (CHATR-187).

Uma tool só, que devolve a grade inteira dos sete dias e deixa a LLM escolher o
recorte — quem toca hoje, em que dia e palco toca uma banda, quais atrações há
num palco. Registrar uma tool por pergunta multiplicaria as chances de o modelo
escolher errado entre as dezenas já publicadas pelo servidor, e o payload
completo é pequeno o bastante para caber na conversa.

O risco número um desta tool é o modelo inventar horário de show: a fonte não
publica horários (ver `scraper.py`), e "às 22h no Palco Mundo" é uma frase que
sai natural de um modelo de linguagem. Por isso a ausência é dita de forma
explícita e redundante no retorno, junto com os links do app oficial, que é onde
a grade horária de fato existe.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List

from src.tools.rock_in_rio.cache import LineupIndisponivel, obter_lineup
from src.tools.rock_in_rio.scraper import DIAS_DO_EVENTO

# `_get_weekday_pt` é privado por convenção de nome, mas é a única tradução de
# dia da semana do projeto. Reimplementá-la aqui criaria uma segunda tabela para
# manter em sincronia com a primeira, sem ganho nenhum.
from src.utils.datetime_utils import _get_weekday_pt, get_rio_timezone
from src.utils.log import logger

NOME_DO_EVENTO = "Rock in Rio 2026"
LOCAL_DO_EVENTO = (
    "Cidade do Rock — Parque Olímpico do Rio, Barra da Tijuca, Rio de Janeiro"
)

APP_OFICIAL = {
    "android": "https://play.google.com/store/apps/details?id=br.com.rockinrio.app",
    "ios": "https://apps.apple.com/br/app/rock-in-rio/id1478184797",
}

DATAS_DO_EVENTO: tuple[date, ...] = tuple(data for _, data in DIAS_DO_EVENTO)
PRIMEIRO_DIA = DATAS_DO_EVENTO[0]
ULTIMO_DIA = DATAS_DO_EVENTO[-1]

# Hora em que a jornada de um dia de festival é considerada encerrada. Os shows
# entram pela madrugada, então às 2h de um sábado o que está acontecendo ainda é
# a programação de sexta. Sem esse deslocamento, "hoje" viraria a resposta errada
# justamente para quem está na Cidade do Rock de madrugada — o público mais
# provável de perguntar naquele horário.
HORA_FIM_DA_JORNADA = 6

_AVISO_SEM_HORARIOS = (
    "O site oficial do Rock in Rio não publica os horários dos shows — apenas o "
    "dia e o palco de cada atração. NÃO informe, estime ou deduza horário de "
    "show: essa informação não está nesta resposta. Para a grade horária, "
    "oriente o cidadão a consultar o aplicativo oficial do evento."
)

_INSTRUCOES_DE_RESPOSTA = (
    "Use os dados de `shows` para responder. Cada atração tem apenas data e "
    "palco — nunca horário. "
    + _AVISO_SEM_HORARIOS
    + " Os nomes dos artistas vêm exatamente como o site oficial publica; se o "
    "cidadão escrever o nome de forma diferente ou com erro de digitação, "
    "identifique a atração correspondente na lista antes de responder. Sempre "
    "ofereça os links de `app_oficial` ao fim da mensagem."
)


def _descrever_dia(data: date) -> Dict[str, str]:
    return {
        "data": data.isoformat(),
        "data_br": data.strftime("%d/%m/%Y"),
        "dia_semana": _get_weekday_pt(data.weekday()),
    }


def _situacao_temporal(agora: datetime) -> Dict[str, Any]:
    """Descreve onde estamos na linha do tempo do festival, sem ambiguidade.

    Devolve sempre a data de calendário junto do veredito, porque "hoje" dito
    isolado é a fonte clássica de confusão — ainda mais aqui, onde a jornada de
    um dia avança pela madrugada do dia seguinte e onde o festival tem um
    intervalo (08, 09 e 10 de setembro) em que ele não terminou, mas também não
    há show.
    """
    hoje = agora.date()

    # Antes das 6h, a jornada em andamento é a do dia anterior.
    jornada = hoje - timedelta(days=1) if agora.hour < HORA_FIM_DA_JORNADA else hoje

    # `localize` do próprio timezone, e não `agora.tzinfo.localize`: o `tzinfo`
    # de um datetime já localizado pelo pytz é uma instância de offset fixo, e
    # reusá-la para localizar outra data aplicaria o offset da data errada.
    limite_do_festival = get_rio_timezone().localize(
        datetime.combine(ULTIMO_DIA + timedelta(days=1), time(hour=HORA_FIM_DA_JORNADA))
    )

    if agora >= limite_do_festival:
        status = "encerrado"
    elif hoje < PRIMEIRO_DIA:
        status = "antes_do_festival"
    else:
        status = "durante_o_festival"

    proximos = [data for data in DATAS_DO_EVENTO if data > jornada]
    proximo_dia = proximos[0] if proximos else None

    situacao: Dict[str, Any] = {
        "status": status,
        "data_de_hoje": hoje.isoformat(),
        "data_de_hoje_br": hoje.strftime("%d/%m/%Y"),
        "dia_semana_de_hoje": _get_weekday_pt(hoje.weekday()),
        "jornada_de_referencia": jornada.isoformat(),
        "hoje_tem_show": jornada in DATAS_DO_EVENTO,
        "proximo_dia_com_show": _descrever_dia(proximo_dia) if proximo_dia else None,
    }

    # `jornada in DATAS_DO_EVENTO` não é redundante com `jornada != hoje`: a
    # segunda condição é verdadeira em QUALQUER madrugada, inclusive fora do
    # festival. Sem o filtro, às 3h de um dia 30/08 a resposta afirmava que "as
    # atrações em andamento são as do dia 29/08", e às 3h do dia 09/09 — no
    # intervalo do festival — afirmava isso ao lado de `hoje_tem_show: False` e
    # da observação dizendo que hoje não há programação. Duas frases que se
    # contradizem no mesmo payload é o pior insumo possível para o modelo.
    if jornada != hoje and jornada in DATAS_DO_EVENTO:
        situacao["observacao_jornada"] = (
            f"Agora são {agora.strftime('%H:%M')} de "
            f"{hoje.strftime('%d/%m/%Y')}, ainda dentro da madrugada da "
            f"programação de {jornada.strftime('%d/%m/%Y')}. As atrações "
            f"em andamento são as do dia {jornada.strftime('%d/%m/%Y')}."
        )

    if status == "encerrado":
        situacao["observacao"] = (
            f"O {NOME_DO_EVENTO} terminou em {ULTIMO_DIA.strftime('%d/%m/%Y')}. "
            "Informe ao cidadão que esta edição foi encerrada."
        )
    elif status == "durante_o_festival" and not situacao["hoje_tem_show"]:
        situacao["observacao"] = (
            "O festival está em andamento, mas hoje não há programação — "
            f"{NOME_DO_EVENTO} tem um intervalo entre 08 e 10 de setembro."
        )

    return situacao


def _resposta_indisponivel(motivo: str) -> Dict[str, Any]:
    """Resposta de indisponibilidade.

    Preferimos não entregar a entregar dado desatualizado: mandar o cidadão para
    o dia ou o palco errado é pior do que admitir que a consulta falhou. O que
    ainda dá para oferecer com segurança é o app oficial.
    """
    return {
        "disponivel": False,
        "motivo": motivo,
        "instrucoes_de_resposta": (
            "Não foi possível consultar a programação do Rock in Rio agora. "
            "Informe ao cidadão que a consulta está temporariamente indisponível "
            "e ofereça os links do aplicativo oficial. NÃO informe line-up, dia, "
            "palco ou horário de qualquer atração: não há dado confiável nesta "
            "resposta."
        ),
        "evento": {"nome": NOME_DO_EVENTO, "local": LOCAL_DO_EVENTO},
        "app_oficial": APP_OFICIAL,
    }


async def get_rock_in_rio_lineup() -> Dict[str, Any]:
    """Devolve a programação completa do Rock in Rio 2026.

    Returns:
        Dicionário com a grade dos sete dias, a situação temporal do festival e
        os links do aplicativo oficial. Em caso de indisponibilidade, devolve
        `disponivel: False` com orientação explícita de não inventar dados.
    """
    try:
        carregado = await obter_lineup()
    except LineupIndisponivel as erro:
        logger.warning(f"Line-up do Rock in Rio indisponível para a tool: {erro}")
        return _resposta_indisponivel(str(erro))
    except Exception as erro:
        logger.exception("Erro inesperado ao obter o line-up do Rock in Rio")
        return _resposta_indisponivel(f"Erro inesperado ao consultar a fonte: {erro}")

    agora = datetime.now(get_rio_timezone())
    shows: List[Dict[str, str]] = carregado.shows

    # `dict.fromkeys` deduplica preservando a ordem de aparição, que é o que
    # importa aqui: é ela que reflete a ordem dos palcos na página.
    palcos: List[str] = list(dict.fromkeys(show["palco"] for show in shows))

    atualizado_em = datetime.fromtimestamp(
        carregado.gerado_em_epoch, tz=get_rio_timezone()
    )

    return {
        "disponivel": True,
        "instrucoes_de_resposta": _INSTRUCOES_DE_RESPOSTA,
        "evento": {
            "nome": NOME_DO_EVENTO,
            "local": LOCAL_DO_EVENTO,
            "fuso_horario": str(get_rio_timezone()),
            "dias": [_descrever_dia(data) for data in DATAS_DO_EVENTO],
            "palcos": palcos,
        },
        "situacao": _situacao_temporal(agora),
        "horarios": {
            "disponiveis": False,
            "aviso": _AVISO_SEM_HORARIOS,
            "onde_consultar": "Aplicativo oficial do Rock in Rio",
        },
        "app_oficial": APP_OFICIAL,
        "total_de_atracoes": len(shows),
        "shows": shows,
        "atualizado_em": {
            "iso": atualizado_em.isoformat(),
            "hora_br": atualizado_em.strftime("%d/%m/%Y %H:%M"),
            "ha_segundos": int(carregado.idade_s),
            "origem": carregado.origem,
        },
    }


def descricao_da_tool(tool_version: str) -> str:
    """Descrição publicada no catálogo MCP.

    O aviso de ausência de horários aparece já aqui, e não só no retorno, para
    que o modelo saiba o que a tool não entrega antes mesmo de chamá-la.
    """
    dias = ", ".join(data.strftime("%d/%m") for data in DATAS_DO_EVENTO)
    return (
        f"[TOOL_VERSION: {tool_version}] Consulta a programação oficial do "
        f"{NOME_DO_EVENTO} na Cidade do Rock ({dias}). Devolve a grade completa "
        "com todas as atrações dos sete dias, indicando o DIA e o PALCO de cada "
        "uma, além dos links do aplicativo oficial.\n\n"
        "Use para: quem toca em determinado dia, em que dia e palco uma banda "
        "se apresenta, quais atrações há em um palco, e para montar a lista de "
        "dias que o cidadão precisa comparecer para ver as bandas que quer.\n\n"
        "ATENÇÃO: esta tool NÃO devolve horários de show, porque a fonte oficial "
        "não os publica. Nunca informe ou estime horário de apresentação a "
        "partir desta resposta; para isso, oriente o cidadão a usar o "
        "aplicativo oficial do evento."
    )
