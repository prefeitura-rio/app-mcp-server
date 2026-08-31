"""Chat local para testar o line-up do Rock in Rio (CHATR-187).

Sobe uma página com cara de WhatsApp em `http://127.0.0.1:8100/` onde dá para
perguntar o que o cidadão perguntaria — "quem toca hoje?", "que dia toca o Foo
Fighters?", "o que tem no Palco Sunset?" — e ver a resposta montada a partir do
retorno real da tool `rock_in_rio_lineup`.

**Não tem LLM, e é de propósito.** A pergunta que este runner responde é "os
dados da tool sustentam a conversa?", não "o modelo escolhe a tool certa?" — essa
segunda é o chat do VS Code em agent mode. Aqui o roteamento da pergunta é
determinístico e mora em Python, então duas execuções iguais dão a mesma
resposta e dá para apontar o dedo para o que quebrou.

O que se enxerga aqui e não se enxerga no Inspector:

- a lógica de "hoje" do `_situacao_temporal` — o intervalo de 08 a 10 de
  setembro, o festival ainda não começado, a jornada que avança pela madrugada;
- o dia e o palco de cada atração conferidos contra o que o site publica;
- a ausência de horários, que é o risco número um desta tool: pergunte "que
  horas toca" e veja a resposta que o cidadão recebe.

O painel do rodapé mostra a fatia crua do retorno usada em cada resposta e as
`instrucoes_de_resposta` que a LLM receberia — é onde se confere se o aviso de
"não invente horário" está de fato chegando.

Execução:

    uv run python src/tests/e2e/run_chat_rock_in_rio.py

Por padrão chama a tool no próprio processo: não precisa de servidor no ar, de
Redis nem de token, e breakpoint dentro da tool para de verdade. Para exercitar
a tool publicada pelo servidor MCP (nome registrado, autenticação, serialização
da resposta), use `--mcp`.

Mora em `src/tests/e2e/` com prefixo `run_` para não ser coletado pelo pytest —
mesma convenção dos demais runners desta pasta.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import re
import sys
import threading
import unicodedata
import webbrowser
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import get_close_matches
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

RAIZ = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RAIZ))

from src.tools.rock_in_rio.scraper import url_do_artista  # noqa: E402

PORTA_PADRAO = 8100
BIND_PADRAO = "127.0.0.1"
URL_MCP_PADRAO = "http://127.0.0.1:80/mcp"
ENV_PADRAO = "src/config/.env"

# Nome com que a tool é registrada em `src/app.py` — diferente do nome da função
# Python, e é justamente por isso que o modo `--mcp` tem valor.
NOME_DA_TOOL = "rock_in_rio_lineup"

# Palavras que só carregam a intenção da pergunta e atrapalham o casamento por
# similaridade com o nome do artista. Retirá-las transforma "quando toca o iron
# maden" em "iron maden", que o `difflib` casa com "IRON MAIDEN".
VAZIAS = {
    "a",
    "as",
    "ao",
    "aos",
    "com",
    "da",
    "das",
    "de",
    "do",
    "dos",
    "e",
    "em",
    "esse",
    "essa",
    "este",
    "esta",
    "eu",
    "no",
    "na",
    "nos",
    "nas",
    "o",
    "os",
    "para",
    "pra",
    "por",
    "qual",
    "quais",
    "quando",
    "que",
    "quem",
    "se",
    "ver",
    "vai",
    "vao",
    "um",
    "uma",
    "sobre",
    "me",
    "diz",
    "fala",
    "mostra",
    "toca",
    "tocar",
    "show",
    "shows",
    "atracao",
    "atracoes",
    "banda",
    "onde",
    "dia",
    "horario",
    "horarios",
    "hora",
    "horas",
    "line",
    "up",
    "lineup",
    "grade",
}

MESES = {"set": 9, "setembro": 9, "ago": 8, "agosto": 8, "out": 10, "outubro": 10}

# `.capitalize()` transformaria "ios" em "Ios".
LOJAS = {"android": "Android", "ios": "iOS"}

SAUDACOES = {"oi", "ola", "eai", "bom dia", "boa tarde", "boa noite", "hey"}
PEDIDOS_DE_AJUDA = {"ajuda", "menu", "opcoes", "help", "comandos", "?"}

# "que horas", e não só "hora": o objetivo é pegar quem pergunta por horário sem
# capturar "quantas horas de show", que também não temos como responder mas cai
# igualmente bem no mesmo aviso.
PADRAO_HORARIO = re.compile(
    r"\b(que horas|horario|horarios|hora do|hora de|abre|abertura|comeca|"
    r"comeco|inicia|inicio|termina|fecha)\b"
)


def _normalizar(texto: str) -> str:
    """Minúsculas, sem acento e com espaços colapsados."""
    decomposto = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def _nucleo(norm: str) -> str:
    """A pergunta sem as palavras que só marcam a intenção."""
    tokens = [t.strip(".,!?;:") for t in norm.split()]
    return " ".join(t for t in tokens if t and t not in VAZIAS)


@dataclass
class Resposta:
    """Uma virada de conversa: o que aparece na tela e o que a sustenta.

    `bruto` é a fatia do retorno da tool usada para montar as bolhas — é o que
    o painel do rodapé mostra, para quem precisar reportar exatamente o que a
    tool devolveu.
    """

    bolhas: List[str]
    sugestoes: List[str] = field(default_factory=list)
    bruto: Any = None


class Lineup:
    """Índices sobre o retorno da tool.

    Só indexa: nenhuma regra de negócio é recalculada aqui. "Hoje", em
    particular, sai de `situacao`, que é o campo que a tool monta — recalcular
    a jornada nesta página faria o chat concordar consigo mesmo em vez de
    conferir a tool.
    """

    def __init__(self, bruto: Dict[str, Any]) -> None:
        self.bruto = bruto
        self.disponivel = bool(bruto.get("disponivel"))
        self.motivo = bruto.get("motivo") or ""
        self.evento = bruto.get("evento") or {}
        self.situacao = bruto.get("situacao") or {}
        self.horarios = bruto.get("horarios") or {}
        self.app_oficial = bruto.get("app_oficial") or {}
        self.atualizado_em = bruto.get("atualizado_em") or {}
        self.instrucoes = bruto.get("instrucoes_de_resposta") or ""
        self.shows: List[Dict[str, str]] = bruto.get("shows") or []
        self.palcos: List[str] = self.evento.get("palcos") or []
        self.dias: List[Dict[str, str]] = self.evento.get("dias") or []

        self.por_data: Dict[str, List[Dict[str, str]]] = {}
        for show in self.shows:
            self.por_data.setdefault(show.get("data", ""), []).append(show)

        self.rotulo: Dict[str, str] = {}
        self.dia_semana: Dict[str, str] = {}
        for dia in self.dias:
            iso = dia.get("data", "")
            semana = dia.get("dia_semana", "")
            self.rotulo[iso] = f"{dia.get('data_br', iso)} ({semana})"
            # "Sexta-feira" -> "sexta": é como a pessoa escreve.
            self.dia_semana[iso] = _normalizar(semana).replace("-feira", "")

        self.por_artista: Dict[str, List[Dict[str, str]]] = {}
        for show in self.shows:
            self.por_artista.setdefault(
                _normalizar(show.get("artista", "")), []
            ).append(show)

    # ----- detecção -----

    def _iso_de(self, dia: int, mes: Optional[int]) -> Optional[str]:
        for iso in self.por_data:
            try:
                data = date.fromisoformat(iso)
            except ValueError:
                continue
            if data.day == dia and (mes is None or data.month == mes):
                return iso
        return None

    def datas_no_texto(self, norm: str) -> List[str]:
        """Datas do festival citadas na pergunta, na ordem em que aparecem."""
        achadas: List[str] = []

        def registrar(iso: Optional[str]) -> None:
            if iso and iso in self.por_data and iso not in achadas:
                achadas.append(iso)

        for dia, mes in re.findall(r"\b(\d{1,2})\s*[/-]\s*(\d{1,2})\b", norm):
            registrar(self._iso_de(int(dia), int(mes)))

        for dia, mes_txt in re.findall(
            r"\b(\d{1,2})\s*[-/ ]?\s*de?\s*([a-z]{3,})", norm
        ):
            if mes_txt in MESES:
                registrar(self._iso_de(int(dia), MESES[mes_txt]))

        for dia in re.findall(r"\bdia\s+(\d{1,2})\b", norm):
            registrar(self._iso_de(int(dia), None))

        if achadas:
            return achadas

        # Dia da semana só entra se nada mais casou: o festival tem duas
        # sextas, dois sábados e dois domingos, então isso é pergunta ambígua,
        # e responder por ela é o comportamento errado.
        for iso, semana in self.dia_semana.items():
            if semana and semana in norm:
                registrar(iso)
        return achadas

    def palco_no_texto(self, norm: str) -> Optional[str]:
        for palco in self.palcos:
            alvo = _normalizar(palco)
            if alvo in norm:
                return palco
            nucleo = alvo.replace("palco ", "").replace("espaco ", "")
            if len(nucleo) >= 5 and nucleo in norm:
                return palco
        return None

    def artistas_no_texto(self, norm: str) -> List[str]:
        """Artistas citados, do casamento mais forte para o mais fraco.

        Reproduz de propósito o que a tool pede à LLM: identificar a atração
        mesmo quando o cidadão escreve o nome de outro jeito ou com erro de
        digitação.
        """
        nomes = list(self.por_artista)
        contidos = [nome for nome in nomes if len(nome) >= 3 and nome in norm]
        if contidos:
            return sorted(contidos, key=len, reverse=True)[:3]

        alvo = _nucleo(norm)
        if len(alvo) < 3:
            return []

        contem = [nome for nome in nomes if alvo in nome]
        if contem:
            return sorted(contem, key=len)[:3]

        return get_close_matches(alvo, nomes, n=3, cutoff=0.7)


class Conversa:
    """Monta a resposta do bot para uma mensagem do cidadão."""

    def __init__(self, lineup: Lineup) -> None:
        self.lineup = lineup

    # ----- blocos reaproveitados -----

    def _links_do_app(self) -> str:
        """Vazio quando não há link — cabeçalho sozinho é pior que nada."""
        linhas = [
            f"{LOJAS.get(loja, loja.capitalize())}: {url}"
            for loja, url in self.lineup.app_oficial.items()
            if url
        ]
        if not linhas:
            return ""
        return "📱 App oficial do Rock in Rio:\n" + "\n".join(linhas)

    def _sugestoes_padrao(self) -> List[str]:
        sugestoes = ["Quem toca hoje?"]
        if self.lineup.dias:
            sugestoes.append(f"Grade de {self.lineup.dias[0].get('data_br', '')}")
        if self.lineup.palcos:
            sugestoes.append(self.lineup.palcos[0])
        if self.lineup.shows:
            sugestoes.append(
                f"Que dia toca {self.lineup.shows[0].get('artista', '').title()}?"
            )
        sugestoes.append("Que horas começa?")
        return [s for s in sugestoes if s.strip()]

    def _grade(self, iso: str, palco: Optional[str] = None) -> List[str]:
        """Uma bolha por palco — é como a grade fica legível no celular."""
        shows = self.lineup.por_data.get(iso, [])
        if palco:
            shows = [s for s in shows if s.get("palco") == palco]

        rotulo = self.lineup.rotulo.get(iso, iso)
        if not shows:
            alvo = f" no {palco}" if palco else ""
            return [f"Não há atração{alvo} em *{rotulo}*."]

        cabecalho = f"*{rotulo}*"
        if palco:
            cabecalho += f"\n🎪 {palco}"
        cabecalho += f"\n{len(shows)} atrações"

        if palco:
            # Já dito no cabeçalho: repetir o nome do palco aqui só gastaria
            # linha numa tela de celular.
            nomes = [s.get("artista", "") for s in shows]
            return [cabecalho, "\n".join(f"• {n}" for n in nomes)]

        bolhas = [cabecalho]
        ordem = [p for p in self.lineup.palcos if p in {s.get("palco") for s in shows}]
        for nome_do_palco in ordem:
            nomes = [
                s.get("artista", "") for s in shows if s.get("palco") == nome_do_palco
            ]
            bolhas.append(f"*{nome_do_palco}*\n" + "\n".join(f"• {n}" for n in nomes))
        return bolhas

    # ----- respostas por intenção -----

    def indisponivel(self) -> Resposta:
        """Espelha o `_resposta_indisponivel` da tool.

        Vale rodar o chat com a rede cortada só para ver esta tela: é o que o
        cidadão recebe quando o site não responde, e ela não pode conter
        line-up nenhum.
        """
        bolhas = [
            "A consulta à programação do Rock in Rio está indisponível agora, "
            "então não consigo informar dia nem palco de nenhuma atração."
        ]
        links = self._links_do_app()
        if links:
            bolhas.append(links)
        return Resposta(
            bolhas=bolhas,
            sugestoes=["Tentar de novo"],
            bruto={"disponivel": False, "motivo": self.lineup.motivo},
        )

    def boas_vindas(self) -> Resposta:
        evento = self.lineup.evento
        atualizado = self.lineup.atualizado_em
        cabecalho = (
            f"Oi! Aqui é o teste do line-up do *{evento.get('nome', 'Rock in Rio')}* 🎸\n"
            f"{len(self.lineup.shows)} atrações em {len(self.lineup.dias)} dias, "
            f"em {len(self.lineup.palcos)} palcos."
        )
        if atualizado.get("hora_br"):
            cabecalho += (
                f"\nDado de {atualizado['hora_br']} "
                f"(origem: {atualizado.get('origem', '?')})."
            )
        return Resposta(
            bolhas=[
                cabecalho,
                "Pergunte por *dia*, por *palco* ou por *atração*. "
                "Também respondo o que a tool sabe sobre hoje.",
            ],
            sugestoes=self._sugestoes_padrao(),
            bruto={"evento": evento, "atualizado_em": atualizado},
        )

    def ajuda(self) -> Resposta:
        return Resposta(
            bolhas=[
                "Dá para perguntar assim:\n"
                "• *quem toca hoje*\n"
                "• *grade de 04/09* ou *dia 12*\n"
                "• *palco sunset*\n"
                "• *que dia toca <atração>*\n"
                "• *quais palcos*"
            ],
            sugestoes=self._sugestoes_padrao(),
        )

    def horarios(self) -> Resposta:
        """A pergunta que a tool não pode responder — e o motivo de ela existir.

        O texto abaixo diz a mesma coisa que `horarios.aviso` diz à LLM. Se um
        dia a fonte passar a publicar horário, é aqui e no contrato que a
        mudança aparece primeiro.
        """
        return Resposta(
            bolhas=[
                "O site oficial não publica os horários dos shows — a programação "
                "traz só o *dia* e o *palco* de cada atração, e é isso que a tool "
                "devolve.",
                "Não dá para estimar: horário de show não está nesta resposta.\n\n"
                + self._links_do_app(),
            ],
            sugestoes=self._sugestoes_padrao(),
            bruto={"horarios": self.lineup.horarios},
        )

    def hoje(self, deslocamento: int = 0) -> Resposta:
        """Responde a partir de `situacao`, que é o que está sob teste."""
        situacao = self.lineup.situacao
        status = situacao.get("status")
        proximo = situacao.get("proximo_dia_com_show") or {}

        if deslocamento:
            base = situacao.get("data_de_hoje")
            try:
                alvo = (
                    date.fromisoformat(base) + timedelta(days=deslocamento)
                ).isoformat()
            except (TypeError, ValueError):
                alvo = None
            if alvo and alvo in self.lineup.por_data:
                return Resposta(
                    bolhas=self._grade(alvo),
                    sugestoes=self._sugestoes_padrao(),
                    bruto={"situacao": situacao, "data": alvo},
                )
            # Dia sem show não está em `rotulo`, que só indexa dias de
            # festival; formatar aqui evita mostrar ISO para o cidadão.
            rotulo = date.fromisoformat(alvo).strftime("%d/%m/%Y") if alvo else "amanhã"
            bolhas = [f"Não há programação em *{rotulo}*."]
            if proximo:
                bolhas.append(
                    f"O próximo dia com show é *{proximo.get('data_br')} "
                    f"({proximo.get('dia_semana')})*."
                )
            return Resposta(
                bolhas=bolhas,
                sugestoes=self._sugestoes_padrao(),
                bruto={"situacao": situacao},
            )

        bolhas: List[str] = []
        if situacao.get("observacao_jornada"):
            bolhas.append(situacao["observacao_jornada"])

        if status == "encerrado":
            bolhas.append(
                situacao.get("observacao") or "Esta edição do Rock in Rio já terminou."
            )
        elif situacao.get("hoje_tem_show"):
            bolhas.extend(self._grade(situacao.get("jornada_de_referencia", "")))
        else:
            hoje_br = situacao.get("data_de_hoje_br", "hoje")
            semana = situacao.get("dia_semana_de_hoje", "")
            bolhas.append(
                f"Hoje é *{hoje_br}* ({semana}) e não há programação."
                if status == "durante_o_festival"
                else f"Hoje é *{hoje_br}* ({semana}) e o festival ainda não começou."
            )
            if situacao.get("observacao"):
                bolhas.append(situacao["observacao"])
            if proximo:
                bolhas.append(
                    f"O próximo dia com show é *{proximo.get('data_br')} "
                    f"({proximo.get('dia_semana')})*. Quer ver a grade?"
                )

        sugestoes = self._sugestoes_padrao()
        if proximo.get("data_br"):
            sugestoes.insert(0, f"Grade de {proximo['data_br']}")
        return Resposta(
            bolhas=bolhas, sugestoes=sugestoes, bruto={"situacao": situacao}
        )

    def dia(self, datas: List[str], palco: Optional[str] = None) -> Resposta:
        if len(datas) > 1:
            rotulos = [self.lineup.rotulo.get(iso, iso) for iso in datas]
            return Resposta(
                bolhas=[
                    "Esse dia da semana cai duas vezes no festival. Qual deles?\n"
                    + "\n".join(f"• {r}" for r in rotulos)
                ],
                sugestoes=[
                    f"Grade de {self.lineup.rotulo[iso].split(' ')[0]}" for iso in datas
                ],
                bruto={"dias": [self.lineup.rotulo.get(iso) for iso in datas]},
            )

        iso = datas[0]
        shows = [
            s
            for s in self.lineup.por_data.get(iso, [])
            if not palco or s.get("palco") == palco
        ]
        return Resposta(
            bolhas=self._grade(iso, palco),
            sugestoes=self._sugestoes_padrao(),
            bruto={"data": iso, "palco": palco, "shows": shows},
        )

    def palco(self, nome: str) -> Resposta:
        shows = [s for s in self.lineup.shows if s.get("palco") == nome]
        if not shows:
            return Resposta(
                bolhas=[f"Não encontrei atração no *{nome}*."],
                sugestoes=self._sugestoes_padrao(),
                bruto={"palco": nome, "shows": []},
            )

        bolhas = [f"*{nome}*\n{len(shows)} atrações no festival"]
        for iso in sorted({s.get("data", "") for s in shows}):
            nomes = [s.get("artista", "") for s in shows if s.get("data") == iso]
            bolhas.append(
                f"*{self.lineup.rotulo.get(iso, iso)}*\n"
                + "\n".join(f"• {n}" for n in nomes)
            )
        return Resposta(
            bolhas=bolhas,
            sugestoes=self._sugestoes_padrao(),
            bruto={"palco": nome, "shows": shows},
        )

    def artista(self, chaves: List[str], exato: bool) -> Resposta:
        # Com mais de um candidato e sem casamento exato, escolher por conta
        # própria esconderia justamente o que se quer ver: "alok" casa com duas
        # atrações distintas do line-up.
        if len(chaves) > 1 and not exato:
            nomes = [
                self.lineup.por_artista[c][0].get("artista", "")
                for c in chaves
                if c in self.lineup.por_artista
            ]
            return Resposta(
                bolhas=["Achei mais de uma atração com esse nome. Qual delas?"],
                sugestoes=nomes,
                bruto={"candidatos": nomes},
            )

        shows = self.lineup.por_artista.get(chaves[0], [])
        bolhas = []
        for show in shows:
            iso = show.get("data", "")
            slug = show.get("slug", "")
            linhas = [
                f"*{show.get('artista', '')}*",
                f"📅 {self.lineup.rotulo.get(iso, iso)}",
                f"🎪 {show.get('palco', '')}",
            ]
            # A resposta da tool não carrega a URL de cada atração: ela sai do
            # slug, e repeti-la 156 vezes custaria contexto à toa. Derivar aqui
            # é o outro lado dessa decisão.
            if slug:
                linhas.append(f"🔗 {url_do_artista(slug)}")
            bolhas.append("\n".join(linhas))
        if len(shows) > 1:
            bolhas.insert(0, "Essa atração aparece em mais de um dia:")
        bolhas.append("O site não publica o horário — só o dia e o palco.")

        sugestoes = self._sugestoes_padrao()
        if shows:
            sugestoes.insert(0, shows[0].get("palco", ""))
        return Resposta(
            bolhas=bolhas,
            sugestoes=[s for s in sugestoes if s],
            bruto={"shows": shows},
        )

    def nao_encontrei(self, texto: str, norm: str) -> Resposta:
        """Um "não achei" que diz o que procurou.

        A distinção importa no teste: banda que não está nesta edição e pergunta
        que o roteamento não entendeu produzem a mesma tela para o cidadão, e é
        preciso conseguir diferenciar as duas olhando para ela.
        """
        procurado = _nucleo(norm)
        bolhas = [
            f"Não encontrei *{procurado}* no line-up desta edição."
            if procurado
            else f'Não entendi "{texto}".'
        ]
        bolhas.append(
            "Tente por dia (*04/09*), por palco (*Palco Mundo*) ou pelo nome "
            "de uma atração como o site publica."
        )
        return Resposta(bolhas=bolhas, sugestoes=self._sugestoes_padrao())

    # ----- roteamento -----

    def responder(self, texto: str) -> Resposta:
        resposta = self._rotear(texto)
        # Os chips vêm de duas fontes (a resposta específica e o menu padrão) e
        # repetem com frequência — "Grade de 04/09" sai das duas quando o
        # próximo dia com show é o primeiro do festival.
        vistos: Dict[str, None] = {}
        for sugestao in resposta.sugestoes:
            if sugestao.strip():
                vistos.setdefault(sugestao.strip(), None)
        resposta.sugestoes = list(vistos)[:6]
        return resposta

    def _rotear(self, texto: str) -> Resposta:
        norm = _normalizar(texto)
        if not self.lineup.disponivel:
            return self.indisponivel()
        if not norm:
            return self.ajuda()
        if norm in SAUDACOES:
            return self.boas_vindas()
        if norm in PEDIDOS_DE_AJUDA:
            return self.ajuda()

        # Horário antes de tudo: "que horas toca o Foo Fighters" casa também com
        # o artista, e a resposta certa é a que diz que horário não existe aqui.
        if PADRAO_HORARIO.search(norm):
            return self.horarios()

        if "amanha" in norm:
            return self.hoje(deslocamento=1)
        if "hoje" in norm or "agora" in norm:
            return self.hoje()

        if re.search(r"\bpalcos\b", norm):
            return Resposta(
                bolhas=[
                    "Os palcos desta edição:\n"
                    + "\n".join(f"• {p}" for p in self.lineup.palcos)
                ],
                sugestoes=self.lineup.palcos,
                bruto={"palcos": self.lineup.palcos},
            )
        if re.search(r"\bdias\b|\bdatas\b", norm):
            return Resposta(
                bolhas=[
                    "Os dias do festival:\n"
                    + "\n".join(
                        f"• {self.lineup.rotulo[d['data']]}"
                        for d in self.lineup.dias
                        if d.get("data") in self.lineup.rotulo
                    )
                ],
                sugestoes=[f"Grade de {d.get('data_br')}" for d in self.lineup.dias],
                bruto={"dias": self.lineup.dias},
            )

        datas = self.lineup.datas_no_texto(norm)
        palco = self.lineup.palco_no_texto(norm)
        if datas:
            return self.dia(datas, palco)
        if palco:
            return self.palco(palco)

        artistas = self.lineup.artistas_no_texto(norm)
        if artistas:
            exato = norm in self.lineup.por_artista or _nucleo(norm) in (
                self.lineup.por_artista
            )
            return self.artista(artistas, exato)

        return self.nao_encontrei(texto.strip(), norm)


# ===== origem do dado =====


def _limpar_valor_env(valor: str) -> str:
    """Tira as aspas do `.env`.

    O `VALID_TOKENS` fica entre aspas simples no arquivo, e as aspas não fazem
    parte do token — colá-las junto é a causa clássica de `401 invalid_token`.
    """
    valor = valor.strip()
    if valor[:1] in {"'", '"'}:
        fim = valor.find(valor[0], 1)
        if fim != -1:
            return valor[1:fim]
    return valor.strip("\"'")


def token_padrao() -> str:
    """Primeiro valor de `VALID_TOKENS`, do ambiente ou do `.env`."""
    bruto = os.environ.get("VALID_TOKENS", "")
    if not bruto:
        caminho = Path(os.environ.get("ENV_FILE", RAIZ / ENV_PADRAO))
        if caminho.exists():
            for linha in caminho.read_text(encoding="utf-8").splitlines():
                if linha.strip().startswith("VALID_TOKENS="):
                    bruto = _limpar_valor_env(linha.split("=", 1)[1])
                    break
    return next((t.strip() for t in bruto.split(",") if t.strip()), "")


async def carregar_no_processo(forcar: bool) -> Dict[str, Any]:
    from src.tools.rock_in_rio.cache import resetar_cache
    from src.tools.rock_in_rio.tool import get_rock_in_rio_lineup

    if forcar:
        # Sem isto, o "Recarregar" da página devolveria o mesmo dado por até
        # uma hora — o cache de processo só vence no teto de idade.
        resetar_cache()
    return await get_rock_in_rio_lineup()


async def carregar_via_mcp(url: str, token: str) -> Dict[str, Any]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(
        {
            "rio_mcp_local": {
                "transport": "streamable_http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    )
    tools = await client.get_tools()
    alvo = next((t for t in tools if t.name == NOME_DA_TOOL), None)
    if alvo is None:
        disponiveis = ", ".join(sorted(t.name for t in tools)) or "nenhuma"
        raise RuntimeError(
            f"Tool '{NOME_DA_TOOL}' não encontrada em {url}. Publicadas: {disponiveis}"
        )

    resposta = await alvo.ainvoke({})
    if isinstance(resposta, str):
        resposta = json.loads(resposta)
    if not isinstance(resposta, dict):
        raise RuntimeError(f"Resposta inesperada da tool: {type(resposta).__name__}")
    return resposta


def _app_oficial() -> Dict[str, str]:
    """Links do app, para a tela de indisponibilidade.

    Vêm do módulo da tool: repeti-los aqui criaria uma segunda cópia das URLs
    para manter em sincronia, e é justamente na indisponibilidade que elas são a
    única coisa útil que sobra para o cidadão.
    """
    try:
        from src.tools.rock_in_rio.tool import APP_OFICIAL

        return dict(APP_OFICIAL)
    except Exception:  # noqa: BLE001 - sem links é melhor que sem tela
        return {}


class Estado:
    """O line-up carregado, recarregável sem reiniciar o processo."""

    def __init__(self, carregador) -> None:
        self._carregador = carregador
        self._trava = threading.Lock()
        self.lineup: Optional[Lineup] = None
        self.conversa: Optional[Conversa] = None
        self.erro: str = ""

    def recarregar(self, forcar: bool = False) -> None:
        with self._trava:
            try:
                bruto = asyncio.run(self._carregador(forcar))
                self.erro = ""
            except Exception as erro:  # noqa: BLE001 - vira mensagem na tela
                bruto = {
                    "disponivel": False,
                    "motivo": f"{type(erro).__name__}: {erro}",
                    "app_oficial": _app_oficial(),
                }
                self.erro = str(bruto["motivo"])
            self.lineup = Lineup(bruto)
            self.conversa = Conversa(self.lineup)

    def cabecalho(self) -> Dict[str, Any]:
        """O que a página mostra na barra de cima: procedência do dado."""
        lineup = self.lineup
        if lineup is None:
            return {"titulo": "Rock in Rio", "linha": "carregando…"}
        if not lineup.disponivel:
            return {
                "titulo": lineup.evento.get("nome", "Rock in Rio"),
                "linha": "indisponível — sem dado para responder",
            }
        atualizado = lineup.atualizado_em
        return {
            "titulo": lineup.evento.get("nome", "Rock in Rio"),
            "linha": (
                f"{len(lineup.shows)} atrações · "
                f"{atualizado.get('hora_br', '?')} · "
                f"origem {atualizado.get('origem', '?')}"
            ),
        }


# A página inteira num literal só: um runner de teste que precisa de `static/`
# ao lado deixa de rodar quando alguém o copia para outro lugar.
PAGINA = """<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat local — Rock in Rio</title>
<style>
  :root {
    --barra: #008069;
    --barra-texto: #ffffff;
    --fundo: #efeae2;
    --entrada: #ffffff;
    --saida: #d9fdd3;
    --texto: #111b21;
    --secundario: #667781;
    --campo: #ffffff;
    --painel: #ffffff;
    --borda: rgba(0,0,0,.08);
    --chip: #ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --barra: #202c33;
      --barra-texto: #e9edef;
      --fundo: #0b141a;
      --entrada: #202c33;
      --saida: #005c4b;
      --texto: #e9edef;
      --secundario: #8696a0;
      --campo: #2a3942;
      --painel: #111b21;
      --borda: rgba(255,255,255,.1);
      --chip: #202c33;
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #d2dbdc;
    color: var(--texto);
    display: flex; justify-content: center;
  }
  @media (prefers-color-scheme: dark) { body { background: #0b141a; } }

  .app {
    width: 100%; max-width: 620px; height: 100dvh;
    display: flex; flex-direction: column;
    background: var(--fundo);
    background-image:
      radial-gradient(circle at 20% 30%, rgba(0,0,0,.022) 0 2px, transparent 3px),
      radial-gradient(circle at 70% 65%, rgba(0,0,0,.022) 0 2px, transparent 3px);
    background-size: 90px 90px, 130px 130px;
    box-shadow: 0 0 24px rgba(0,0,0,.18);
    position: relative; overflow: hidden;
  }

  header {
    background: var(--barra); color: var(--barra-texto);
    padding: 10px 12px; display: flex; align-items: center; gap: 12px;
    flex: 0 0 auto; z-index: 3;
  }
  .avatar {
    width: 40px; height: 40px; border-radius: 50%;
    background: rgba(255,255,255,.22);
    display: grid; place-items: center; font-size: 17px; flex: 0 0 auto;
  }
  .quem { flex: 1; min-width: 0; line-height: 1.3; }
  .quem b { font-size: 16px; font-weight: 600; display: block; }
  .quem span {
    font-size: 12px; opacity: .8; display: block;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  header button {
    background: transparent; border: 0; color: inherit; cursor: pointer;
    font-size: 18px; padding: 6px 8px; border-radius: 6px; opacity: .85;
  }
  header button:hover { background: rgba(255,255,255,.14); }

  #conversa {
    flex: 1; overflow-y: auto; padding: 14px 12px 6px;
    display: flex; flex-direction: column; gap: 3px;
  }
  .bolha {
    max-width: 82%; padding: 6px 9px 8px; border-radius: 7.5px;
    font-size: 14.5px; line-height: 1.42; position: relative;
    box-shadow: 0 1px .5px rgba(11,20,26,.13);
    white-space: normal; overflow-wrap: anywhere;
  }
  .bolha + .bolha { margin-top: 2px; }
  .entrada {
    background: var(--entrada); align-self: flex-start; border-top-left-radius: 0;
  }
  .saida {
    background: var(--saida); align-self: flex-end; border-top-right-radius: 0;
  }
  .entrada::before, .saida::before {
    content: ""; position: absolute; top: 0; width: 8px; height: 13px;
  }
  .entrada::before {
    left: -8px;
    background: linear-gradient(225deg, var(--entrada) 50%, transparent 50%);
  }
  .saida::before {
    right: -8px;
    background: linear-gradient(135deg, var(--saida) 50%, transparent 50%);
  }
  .grupo { margin-top: 8px; }
  .bolha a { color: #027eb5; }
  @media (prefers-color-scheme: dark) { .bolha a { color: #53bdeb; } }
  .hora {
    font-size: 11px; color: var(--secundario); float: right;
    margin: 6px -2px -4px 8px;
  }

  .digitando { display: flex; gap: 4px; padding: 10px 12px; }
  .digitando i {
    width: 7px; height: 7px; border-radius: 50%; background: var(--secundario);
    animation: pisca 1.2s infinite;
  }
  .digitando i:nth-child(2) { animation-delay: .18s; }
  .digitando i:nth-child(3) { animation-delay: .36s; }
  @keyframes pisca { 0%,60%,100% { opacity: .25; } 30% { opacity: .9; } }

  #sugestoes {
    display: flex; gap: 6px; padding: 6px 12px; overflow-x: auto;
    flex: 0 0 auto; scrollbar-width: none;
  }
  #sugestoes::-webkit-scrollbar { display: none; }
  #sugestoes button {
    background: var(--chip); color: #027eb5; border: 1px solid var(--borda);
    border-radius: 16px; padding: 6px 13px; font-size: 13.5px;
    white-space: nowrap; cursor: pointer; flex: 0 0 auto;
  }
  @media (prefers-color-scheme: dark) { #sugestoes button { color: #53bdeb; } }
  #sugestoes button:hover { filter: brightness(.96); }

  #barra {
    display: flex; gap: 8px; padding: 8px 12px 12px; flex: 0 0 auto;
    align-items: center;
  }
  #campo {
    flex: 1; border: 0; border-radius: 20px; padding: 11px 16px;
    font-size: 15px; background: var(--campo); color: var(--texto);
    outline: none; font-family: inherit;
  }
  #enviar {
    width: 42px; height: 42px; border-radius: 50%; border: 0; flex: 0 0 auto;
    background: var(--barra); color: #fff; font-size: 17px; cursor: pointer;
  }

  #painel {
    position: absolute; inset: auto 0 0 0; height: 62%;
    background: var(--painel); border-top: 1px solid var(--borda);
    transform: translateY(101%); transition: transform .22s ease;
    display: flex; flex-direction: column; z-index: 4;
  }
  #painel.aberto { transform: translateY(0); }
  #painel .topo {
    display: flex; gap: 6px; align-items: center; padding: 10px 12px;
    border-bottom: 1px solid var(--borda); flex-wrap: wrap;
  }
  #painel .topo b { font-size: 13px; margin-right: auto; }
  #painel .topo button {
    background: transparent; border: 1px solid var(--borda); color: var(--texto);
    border-radius: 14px; padding: 4px 11px; font-size: 12px; cursor: pointer;
  }
  #painel .topo button.ativo { background: var(--barra); color: #fff; border-color: transparent; }
  #painel pre {
    margin: 0; padding: 12px; overflow: auto; flex: 1;
    font-size: 12px; line-height: 1.5; white-space: pre-wrap;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: var(--texto);
  }
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="avatar">🎸</div>
    <div class="quem"><b id="titulo">Rock in Rio</b><span id="linha">carregando…</span></div>
    <button id="btn-recarregar" title="Recarregar o line-up da fonte">⟳</button>
    <button id="btn-painel" title="Ver a resposta crua da tool">&lt;/&gt;</button>
  </header>

  <div id="conversa"></div>
  <div id="sugestoes"></div>

  <form id="barra" autocomplete="off">
    <input id="campo" placeholder="Pergunte por dia, palco ou atração" autofocus>
    <button id="enviar" type="submit" title="Enviar">➤</button>
  </form>

  <div id="painel">
    <div class="topo">
      <b>o que sustenta a resposta</b>
      <button data-aba="fatia" class="ativo">fatia usada</button>
      <button data-aba="instrucoes">instruções p/ a LLM</button>
      <button data-aba="completo">resposta completa</button>
      <button id="fechar-painel">✕</button>
    </div>
    <pre id="conteudo-painel">—</pre>
  </div>
</div>

<script>
const conversa = document.getElementById("conversa");
const sugestoes = document.getElementById("sugestoes");
const campo = document.getElementById("campo");
const painel = document.getElementById("painel");
const conteudoPainel = document.getElementById("conteudo-painel");

let ultimaFatia = null;
let instrucoes = "";
let abaAtual = "fatia";

const espera = (ms) => new Promise((r) => setTimeout(r, ms));

function escapar(t) {
  return t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function formatar(texto) {
  let html = escapar(texto);
  html = html.replace(/\\*([^*\\n]+)\\*/g, "<strong>$1</strong>");
  html = html.replace(/(https?:\\/\\/[^\\s<]+)/g,
    '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
  return html.replace(/\\n/g, "<br>");
}

function agora() {
  return new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

function addBolha(texto, lado, primeira) {
  const div = document.createElement("div");
  div.className = "bolha " + lado + (primeira ? " grupo" : "");
  div.innerHTML = formatar(texto) + '<span class="hora">' + agora() + "</span>";
  conversa.appendChild(div);
  conversa.scrollTop = conversa.scrollHeight;
}

function digitando(ligar) {
  const existente = document.getElementById("digitando");
  if (existente) existente.remove();
  if (!ligar) return;
  const div = document.createElement("div");
  div.id = "digitando";
  div.className = "bolha entrada grupo digitando";
  div.innerHTML = "<i></i><i></i><i></i>";
  conversa.appendChild(div);
  conversa.scrollTop = conversa.scrollHeight;
}

function renderSugestoes(lista) {
  sugestoes.innerHTML = "";
  (lista || []).forEach((s) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = s;
    b.onclick = () => enviar(s);
    sugestoes.appendChild(b);
  });
}

function aplicarCabecalho(cab) {
  if (!cab) return;
  document.getElementById("titulo").textContent = cab.titulo;
  document.getElementById("linha").textContent = cab.linha;
}

async function mostrar(dados) {
  aplicarCabecalho(dados.cabecalho);
  renderSugestoes([]);
  digitando(true);
  await espera(280);
  digitando(false);
  for (let i = 0; i < dados.bolhas.length; i++) {
    addBolha(dados.bolhas[i], "entrada", i === 0);
    if (i < dados.bolhas.length - 1) await espera(200);
  }
  renderSugestoes(dados.sugestoes);
  ultimaFatia = dados.bruto;
  if (painel.classList.contains("aberto")) pintarPainel();
}

async function enviar(texto) {
  const limpo = (texto || "").trim();
  if (!limpo) return;
  addBolha(limpo, "saida", true);
  campo.value = "";
  const r = await fetch("/api/mensagem", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ texto: limpo }),
  });
  await mostrar(await r.json());
}

async function pintarPainel() {
  if (abaAtual === "fatia") {
    conteudoPainel.textContent = ultimaFatia
      ? JSON.stringify(ultimaFatia, null, 2)
      : "Esta resposta não usou dado da tool.";
  } else if (abaAtual === "instrucoes") {
    conteudoPainel.textContent =
      instrucoes || "A tool não devolveu instrucoes_de_resposta.";
  } else {
    conteudoPainel.textContent = "carregando…";
    const r = await fetch("/api/bruto");
    conteudoPainel.textContent = JSON.stringify(await r.json(), null, 2);
  }
}

document.getElementById("barra").onsubmit = (e) => {
  e.preventDefault();
  enviar(campo.value);
};

document.getElementById("btn-painel").onclick = () => {
  painel.classList.toggle("aberto");
  if (painel.classList.contains("aberto")) pintarPainel();
};
document.getElementById("fechar-painel").onclick = () =>
  painel.classList.remove("aberto");

document.querySelectorAll("#painel .topo button[data-aba]").forEach((b) => {
  b.onclick = () => {
    document
      .querySelectorAll("#painel .topo button[data-aba]")
      .forEach((o) => o.classList.remove("ativo"));
    b.classList.add("ativo");
    abaAtual = b.dataset.aba;
    pintarPainel();
  };
});

document.getElementById("btn-recarregar").onclick = async () => {
  digitando(true);
  const r = await fetch("/api/recarregar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  await mostrar(await r.json());
};

(async () => {
  const r = await fetch("/api/inicio");
  const dados = await r.json();
  instrucoes = dados.instrucoes || "";
  await mostrar(dados);
  campo.focus();
})();
</script>
</body>
</html>
"""


# ===== servidor =====

ESTADO: Optional[Estado] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "ChatRockInRio/1.0"

    def _responder(self, corpo: bytes, tipo: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, dados: Any, status: int = 200) -> None:
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self._responder(corpo, "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 - assinatura da stdlib
        assert ESTADO is not None
        if self.path in {"/", "/index.html"}:
            self._responder(PAGINA.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/inicio":
            lineup = ESTADO.lineup
            resposta = (
                ESTADO.conversa.responder("oi")
                if ESTADO.conversa
                else Resposta(bolhas=["carregando…"])
            )
            self._json(
                {
                    "cabecalho": ESTADO.cabecalho(),
                    "bolhas": resposta.bolhas,
                    "sugestoes": resposta.sugestoes,
                    "bruto": resposta.bruto,
                    "instrucoes": lineup.instrucoes if lineup else "",
                }
            )
        elif self.path == "/api/bruto":
            self._json(ESTADO.lineup.bruto if ESTADO.lineup else {})
        else:
            self._json({"erro": "rota desconhecida"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - assinatura da stdlib
        assert ESTADO is not None
        tamanho = int(self.headers.get("Content-Length") or 0)
        try:
            corpo = json.loads(self.rfile.read(tamanho) or b"{}")
        except json.JSONDecodeError:
            self._json({"erro": "corpo não é JSON"}, status=400)
            return

        if self.path == "/api/recarregar":
            ESTADO.recarregar(forcar=True)
            resposta = ESTADO.conversa.responder("oi")
            self._json(
                {
                    "cabecalho": ESTADO.cabecalho(),
                    "bolhas": ["Line-up recarregado.", *resposta.bolhas],
                    "sugestoes": resposta.sugestoes,
                    "bruto": resposta.bruto,
                }
            )
            return

        if self.path == "/api/mensagem":
            texto = str(corpo.get("texto") or "")
            # A carga acontece no startup, e uma falha transitória naquele
            # instante deixaria a sessão inteira sem dado. Estando indisponível
            # não há o que perder em tentar de novo: no caminho normal esta
            # linha nunca roda.
            if ESTADO.lineup is not None and not ESTADO.lineup.disponivel:
                ESTADO.recarregar(forcar=True)
            resposta = ESTADO.conversa.responder(texto)
            self._json(
                {
                    "cabecalho": ESTADO.cabecalho(),
                    "bolhas": resposta.bolhas,
                    "sugestoes": resposta.sugestoes,
                    "bruto": resposta.bruto,
                }
            )
            return

        self._json({"erro": "rota desconhecida"}, status=404)

    def log_message(self, formato: str, *args: Any) -> None:
        """Uma linha curta por requisição — o log padrão polui o terminal."""
        if self.path.startswith("/api/"):
            print(f"  {self.command} {self.path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Chat local com cara de WhatsApp para testar o line-up do Rock in Rio."
        )
    )
    parser.add_argument("--porta", type=int, default=PORTA_PADRAO)
    parser.add_argument(
        "--bind",
        default=BIND_PADRAO,
        help=(
            "Padrão 127.0.0.1. Para mostrar a alguém, prefira `tailscale serve "
            "--bg 8100` a abrir para a rede local com 0.0.0.0."
        ),
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help=(
            "Chama a tool pelo servidor MCP em vez de no processo. Exercita o "
            "nome registrado, a autenticação e a serialização da resposta."
        ),
    )
    parser.add_argument("--url", default=URL_MCP_PADRAO, help="Servidor MCP alvo.")
    parser.add_argument(
        "--token", default="", help="Bearer do servidor. Padrão: 1º de VALID_TOKENS."
    )
    parser.add_argument(
        "--sem-navegador", action="store_true", help="Não abre o navegador sozinho."
    )
    return parser.parse_args()


def main() -> int:
    global ESTADO
    args = parse_args()

    if args.mcp:
        token = args.token or token_padrao()
        if not token:
            print(
                "FALHA: --mcp precisa de token. Passe --token ou configure "
                f"VALID_TOKENS em {ENV_PADRAO}."
            )
            return 1
        print(f"Chamando {NOME_DA_TOOL} via MCP em {args.url}")

        async def carregador(forcar: bool) -> Dict[str, Any]:
            return await carregar_via_mcp(args.url, token)
    else:
        print(f"Chamando {NOME_DA_TOOL} no próprio processo")
        carregador = carregar_no_processo

    ESTADO = Estado(carregador)
    ESTADO.recarregar()
    if ESTADO.erro:
        # Não é motivo para não subir: a tela de indisponibilidade é uma das
        # coisas que vale conferir aqui, e a próxima pergunta tenta de novo.
        print(
            f"AVISO: line-up indisponível — {ESTADO.erro}\n"
            "       A primeira pergunta refaz a consulta; o botão ⟳ também."
        )
    else:
        print(f"OK: {ESTADO.cabecalho()['linha']}")

    endereco = f"http://{args.bind}:{args.porta}/"
    try:
        servidor = ThreadingHTTPServer((args.bind, args.porta), Handler)
    except OSError as erro:
        if erro.errno != errno.EADDRINUSE:
            raise
        # Um traceback de socket não diz o que fazer. Como a porta 8100 é
        # compartilhada com outros runners locais, esbarrar num processo antigo
        # ainda no ar é o desfecho provável, não o exótico.
        print(
            f"FALHA: a porta {args.porta} já está em uso.\n"
            f"  Quem está lá:  lsof -nP -iTCP:{args.porta} -sTCP:LISTEN\n"
            f"  Outra porta:   --porta {args.porta + 1}"
        )
        return 1
    print(f"\nChat em {endereco}   (ctrl+c para parar)\n")
    if not args.sem_navegador:
        webbrowser.open(endereco)

    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrado.")
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
