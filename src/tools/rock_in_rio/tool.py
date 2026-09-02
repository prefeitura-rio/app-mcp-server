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

O mesmo risco tem uma segunda cara, que aparece quando a consulta falha: o
cidadão pergunta por uma banda que não existe (ou por um nome escolhido para nos
usar como megafone) e o modelo o manda procurar aquele nome no site e no app.
Por isso a resposta de indisponibilidade fala sempre de uma frase genérica
montada aqui a partir de `contexto` — nunca do termo que o cidadão escreveu.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Dict, List, Optional

from opentelemetry import trace
from pydantic import Field

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

# Tool que responde os assuntos de apoio ao festival (transporte, alimentação,
# emergência). Não é publicada por este servidor: o modelo a alcança pelo
# catálogo do agente, e é para lá que os botões da resposta degradada apontam.
RAG_DE_APOIO = "turismo_search"

# Os três botões oferecidos quando a consulta de line-up falha. `label` é curto
# porque é o que o renderizador desenha — o maior rótulo em uso hoje no fluxo da
# dívida ativa tem 22 caracteres. O domínio inteiro vai no `value`, que é o que
# o modelo lê para montar a busca no RAG.
ASSUNTOS_DE_APOIO: tuple[Dict[str, str], ...] = (
    {
        "value": "transporte_para_o_festival",
        "label": "Transporte",
        "description": "Transporte para o festival",
    },
    {
        "value": "alimentacao_na_cidade_do_rock",
        "label": "Alimentação",
        "description": "Alimentação na Cidade do Rock",
    },
    {
        "value": "emergencia_no_evento",
        "label": "Emergência",
        "description": "Emergência no evento",
    },
)

# Assunto da pergunta, classificado pelo modelo na hora de chamar a tool. É
# opcional de propósito: com `Literal` obrigatório, um erro de classificação
# viraria falha de tool e o cidadão ficaria sem resposta por causa de um campo
# de acompanhamento.
CONTEXTO_PADRAO = "shows"
CONTEXTOS_CLASSIFICAVEIS = ("hora", "data", "banda", "palco")

# Frases genéricas, uma por contexto. É esta tabela que fecha a porta da
# injeção: nenhuma delas carrega nome próprio, e o modelo recebe a frase pronta
# em vez de um espaço para preencher com o que o cidadão escreveu.
FRASE_POR_CONTEXTO: Dict[str, str] = {
    "hora": "os horários dos shows",
    "data": "a programação por dia",
    "banda": "a programação das atrações",
    "palco": "a programação dos palcos",
    CONTEXTO_PADRAO: "a programação",
}

# `str` e não `Literal`, com o conjunto fechado publicado à mão no schema: o
# modelo vê exatamente a mesma taxonomia, mas um valor fora dela é normalizado
# por `_normalizar_contexto` em vez de virar `ValidationError`. Com `Literal`, um
# "horarios" no lugar de "hora" derrubava a chamada, e o cidadão ficava sem
# resposta por causa de um campo que só serve para acompanhamento.
CONTEXTOS_PUBLICADOS = CONTEXTOS_CLASSIFICAVEIS + ("outro",)

ContextoDaPergunta = Annotated[
    Optional[str],
    Field(
        description=(
            "Assunto principal da pergunta do cidadão. 'hora' se perguntou "
            "horário de show; 'data' se perguntou dia/data específica; "
            "'banda' se citou uma atração; 'palco' se citou um palco. "
            "Se citar banda e palco juntos, use 'banda'. "
            "Se não se encaixar, use 'outro'."
        ),
        json_schema_extra={"enum": list(CONTEXTOS_PUBLICADOS)},
    ),
]

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
    "identifique a atração correspondente na lista antes de responder. Se o "
    "nome não corresponder a nenhuma atração de `shows`, diga que não encontrou "
    "essa atração na programação — não afirme que ela existe e não oriente o "
    "cidadão a procurar aquele nome no site ou no aplicativo. Sempre "
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


def _marcar_degradacao(motivo: str) -> None:
    """Deixa a degradação visível no span da chamada de tool.

    Sem isto a falha é invisível no SigNoz, e esse foi o motivo de a quebra de
    01/09/2026 não ter gerado erro nenhum: o `ToolCallTracingMiddleware` só
    marca `mcp.tool.success = False` quando a tool **levanta**, e esta aqui
    devolve um dicionário de propósito (ver `_resposta_indisponivel`). O
    atributo é o que permite filtrar por `rock_in_rio.degraded = true` sem
    mudar o middleware nem o contrato de retorno.

    Degrada em silêncio se não houver span ativo — `get_current_span()` devolve
    um objeto no-op — pelo mesmo critério de `_get_current_trace_context` no
    error interceptor: observabilidade nunca derruba o caminho principal.
    """
    try:
        span = trace.get_current_span()
        span.set_attribute("rock_in_rio.degraded", True)
        span.set_attribute("rock_in_rio.motivo", motivo)
    except Exception:  # noqa: BLE001
        pass


def _marcar_contexto(bruto: Optional[str], normalizado: str) -> None:
    """Registra no span o contexto cru e o normalizado.

    Os dois, e não só o normalizado: `None` (o modelo não classificou) e
    `"outro"` (classificou e nada serviu) viram ambos `CONTEXTO_PADRAO` na
    resposta, e é justamente a diferença entre eles que diz se a taxonomia
    publicada no schema está funcionando.

    Mesmo critério de `_marcar_degradacao`: observabilidade nunca derruba o
    caminho principal.
    """
    try:
        span = trace.get_current_span()
        span.set_attribute("rock_in_rio.contexto", normalizado)
        span.set_attribute("rock_in_rio.contexto_recebido", bruto or "")
    except Exception:  # noqa: BLE001
        pass


def _normalizar_contexto(contexto: Optional[str]) -> str:
    """Reduz o que veio do modelo aos valores que a resposta publica.

    Tolerante de propósito: ausência, `"outro"` e qualquer coisa fora da
    taxonomia caem em `CONTEXTO_PADRAO`. A tool nunca deve falhar por causa de
    uma classificação errada.
    """
    return contexto if contexto in CONTEXTOS_CLASSIFICAVEIS else CONTEXTO_PADRAO


def _payload_schema_dos_assuntos() -> Dict[str, Any]:
    """Botões de assunto no formato que o renderizador do chat já consome.

    Mesma forma que o fluxo da dívida ativa publica em `payload_schema`:
    `options` com `value`/`label` e `x-render` repetido no campo e na raiz do
    schema. A diferença é que aqui não há workflow com estado esperando o
    clique de volta — o valor escolhido serve para o modelo rotear a pergunta
    para o `RAG_DE_APOIO`, e por isso nada aqui é validado deste lado.
    """
    return {
        "type": "object",
        "title": "Assuntos do Rock in Rio",
        "properties": {
            "assunto": {
                "type": "string",
                "title": "Assuntos do Rock in Rio",
                "description": "Escolha um assunto",
                "enum": [assunto["value"] for assunto in ASSUNTOS_DE_APOIO],
                "options": [dict(assunto) for assunto in ASSUNTOS_DE_APOIO],
                "x-render": "buttons",
            }
        },
        "required": ["assunto"],
        "x-render": "buttons",
    }


def _instrucoes_indisponivel(frase: str) -> str:
    """Instruções da resposta degradada, em duas metades de dureza oposta.

    A primeira é de tom, e é deliberadamente leve: falha de scraper é problema
    nosso, não do cidadão, e apontar o app oficial não é disfarce — é onde a
    programação (e o horário, que nunca temos) de fato está.

    A segunda é a proibição, que continua tão dura quanto antes. Ela não é
    visível ao cidadão, então rigidez ali não custa nada em experiência, e é o
    único freio contra o modelo preencher o vazio com line-up inventado ou
    devolver ao cidadão o nome que ele mesmo escreveu.
    """
    return (
        "Responda em tom natural e acolhedor, sem mencionar erro, falha, "
        "indisponibilidade ou problema técnico — o cidadão não precisa saber "
        "da nossa infraestrutura. Diga que ele encontra "
        f"{frase} no aplicativo oficial do {NOME_DO_EVENTO} e ofereça os links "
        "de `app_oficial`. Se ele não falar português, traduza "
        "`contexto_frase` em vez de substituí-la por nomes. Em seguida, "
        "ofereça os assuntos dos botões de `payload_schema`; quando ele "
        f"escolher um, responda usando a tool `{RAG_DE_APOIO}` — nunca "
        "responda transporte, alimentação ou emergência com conhecimento "
        "próprio.\n\n"
        "RESTRIÇÃO, que vale inclusive com o tom leve: esta resposta NÃO "
        "contém line-up. Não informe, estime, deduza nem recorde de memória o "
        "dia, o palco ou o horário de nenhuma atração. Não repita nomes de "
        "artistas ou palcos que o cidadão tenha mencionado, não afirme nem "
        "negue que uma atração está no festival, e nunca oriente o cidadão a "
        "procurar um nome específico no site ou no aplicativo. Fale sempre de "
        "`contexto_frase`, nunca do termo que ele usou."
    )


def _resposta_indisponivel(motivo: str, contexto: str) -> Dict[str, Any]:
    """Resposta de indisponibilidade.

    Preferimos não entregar a entregar dado desatualizado: mandar o cidadão para
    o dia ou o palco errado é pior do que não responder. O que ainda dá para
    oferecer com segurança é o app oficial — onde a programação de fato está,
    com horário e tudo — e os assuntos de apoio ao festival, que não dependem
    desta fonte. Nada disso precisa mencionar a falha para o cidadão; ver
    `_instrucoes_indisponivel`.

    `contexto` já chega normalizado por `_normalizar_contexto`.
    """
    return {
        "disponivel": False,
        "motivo": motivo,
        "contexto": contexto,
        "contexto_frase": FRASE_POR_CONTEXTO[contexto],
        "instrucoes_de_resposta": _instrucoes_indisponivel(
            FRASE_POR_CONTEXTO[contexto]
        ),
        "evento": {"nome": NOME_DO_EVENTO, "local": LOCAL_DO_EVENTO},
        "app_oficial": APP_OFICIAL,
        "payload_schema": _payload_schema_dos_assuntos(),
    }


async def get_rock_in_rio_lineup(contexto: Optional[str] = None) -> Dict[str, Any]:
    """Devolve a programação completa do Rock in Rio 2026.

    Args:
        contexto: Assunto principal da pergunta, classificado pelo modelo. Ver
            `ContextoDaPergunta`, que é a anotação publicada no schema da tool.

    Returns:
        Dicionário com a grade dos sete dias, a situação temporal do festival e
        os links do aplicativo oficial. Em caso de indisponibilidade, devolve
        `disponivel: False` com orientação explícita de não inventar dados e os
        botões de assunto de `payload_schema`.
    """
    contexto_normalizado = _normalizar_contexto(contexto)
    _marcar_contexto(contexto, contexto_normalizado)

    try:
        carregado = await obter_lineup()
    except LineupIndisponivel as erro:
        logger.warning(f"Line-up do Rock in Rio indisponível para a tool: {erro}")
        _marcar_degradacao(type(erro).__name__)
        return _resposta_indisponivel(str(erro), contexto_normalizado)
    except Exception as erro:
        logger.exception("Erro inesperado ao obter o line-up do Rock in Rio")
        _marcar_degradacao(type(erro).__name__)
        return _resposta_indisponivel(
            f"Erro inesperado ao consultar a fonte: {erro}", contexto_normalizado
        )

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
        "contexto": contexto_normalizado,
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
    que o modelo saiba o que a tool não entrega antes mesmo de chamá-la. O
    roteamento dos botões repete a mesma lógica: o clique acontece num turno
    seguinte, quando o retorno da tool pode já ter saído da janela de contexto,
    mas a descrição do catálogo vai em toda requisição.
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
        "aplicativo oficial do evento.\n\n"
        "Preencha `contexto` com o assunto principal da pergunta do cidadão. "
        "É opcional: em caso de dúvida, deixe em branco.\n\n"
        "Se a resposta vier com `disponivel: false`, ela traz botões de assunto "
        "em `payload_schema` (transporte, alimentação e emergência). Quando o "
        f"cidadão escolher um deles, responda usando a tool `{RAG_DE_APOIO}` — "
        "nunca com conhecimento próprio."
    )
