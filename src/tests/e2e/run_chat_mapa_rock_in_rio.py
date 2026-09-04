"""Chat local para testar o mapa da Cidade do Rock.

Sobe uma página com cara de WhatsApp em `http://127.0.0.1:8101/` onde dá para
pedir o mapa como o cidadão pediria e ver a mensagem que ele receberia — com o
card de preview do link montado ao lado, que é a parte que nenhum teste
automatizado mostra.

**Não tem LLM, e é de propósito** — mesma escolha do `run_chat_rock_in_rio.py`.
A pergunta aqui é "a mensagem e o link sustentam a conversa?", não "o modelo
escolhe a tool certa?".

O que se enxerga aqui e não se enxerga no Inspector nem nos testes unitários:

- **o card do preview**: o mapa é um JPEG de ~1,4 MB feito para desktop, e o
  quanto dele sobra num retângulo de preview é decisão de produto, não de
  código. A página desenha o card com a mesma proporção do WhatsApp;
- **as três telas da tool** — link curto, URL crua (encurtador fora do ar) e
  evento encerrado — sem precisar esperar 14/09 nem derrubar nada: a barra de
  simulação troca o relógio, o encurtador e o Redis;
- **a fronteira com a tool de line-up**: pergunte "quem toca sexta" e veja o
  runner apontar que aquilo é da `rock_in_rio_lineup`. Publicar uma segunda tool
  de Rock in Rio criou essa ambiguidade, e é o risco novo desta mudança;
- **a procedência da imagem**: o painel faz um HEAD na URL e mostra tamanho,
  `last-modified` e `ETag`. Como não re-hospedamos, é assim que se percebe que o
  Rock in Rio trocou os bytes por trás da mesma URL.

Execução:

    uv run python src/tests/e2e/run_chat_mapa_rock_in_rio.py

Por padrão **o encurtador é dublado**: não cria link de verdade, não precisa de
token, de Redis nem de servidor no ar. O painel mostra o payload exato que iria
para a API real.

    --real   chama o encurtador de verdade (precisa de SHORT_API_URL/TOKEN)
    --mcp    chama a tool publicada pelo servidor MCP

`--real` **cria link de verdade**, e não há encurtador de homologação: o
`SHORT_API_URL` configurado é `https://pref.rio`, que é produção. Com o link do
mapa já publicado, cada cache miss no `--real` bate em 409 e cai no caminho de
confirmação — que é justamente o que vale ver.

Mora em `src/tests/e2e/` com prefixo `run_` para não ser coletado pelo pytest —
mesma convenção dos demais runners desta pasta.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import errno
import json
import os
import re
import sys
import threading
import unicodedata
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

RAIZ = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(RAIZ))

from src.tools.rock_in_rio import mapa as mapa_mod  # noqa: E402
from src.tools.rock_in_rio.mapa import (  # noqa: E402
    DESCRICAO,
    SHORT_PATH,
    TITULO,
    URL_DO_MAPA,
)
from src.tools.rock_in_rio.tool import (  # noqa: E402
    PRIMEIRO_DIA,
    ULTIMO_DIA,
    limite_do_festival,
)
from src.utils.datetime_utils import get_rio_timezone  # noqa: E402

PORTA_PADRAO = 8101
BIND_PADRAO = "127.0.0.1"
URL_MCP_PADRAO = "http://127.0.0.1:80/mcp"
ENV_PADRAO = "src/config/.env"

# Nome com que a tool é registrada em `src/app.py` — diferente do nome da função
# Python, e é justamente por isso que o modo `--mcp` tem valor.
NOME_DA_TOOL = "rock_in_rio_mapa"

# Link que o dublê devolve. O host é falso de propósito: se ele aparecer num
# print de tela, fica claro que aquilo não saiu do encurtador de verdade.
LINK_DUBLE = f"https://exemplo-encurtador.invalid/link/{SHORT_PATH}"

# Pedido de mapa. "mapao" entra porque é o nome do arquivo que o Rock in Rio
# publica e o termo que circula; "croqui" e "planta" porque é como parte do
# público chama a mesma coisa.
PADRAO_MAPA = re.compile(
    r"\b(mapa|mapao|croqui|planta|layout|esquema|onde fica|como chegar|"
    r"onde e|localizacao|cidade do rock)\b"
)

# Pergunta que é da OUTRA tool. Existe para tornar a fronteira visível: com duas
# tools de Rock in Rio publicadas, "onde toca o Foo Fighters" pode cair aqui por
# causa do "onde", e a resposta certa é mandar para a `rock_in_rio_lineup`.
PADRAO_LINEUP = re.compile(
    r"\b(quem toca|line ?up|lineup|grade|programacao|atracao|atracoes|banda|"
    r"bandas|show|shows|palco|palcos|que dia|que horas|horario|horarios)\b"
)

SAUDACOES = {"oi", "ola", "eai", "bom dia", "boa tarde", "boa noite", "hey"}
PEDIDOS_DE_AJUDA = {"ajuda", "menu", "opcoes", "help", "comandos", "?"}

SUGESTOES = [
    "Me manda o mapa",
    "Como é a Cidade do Rock?",
    "Onde fica o evento?",
    "Quem toca sexta?",
]


def _normalizar(texto: str) -> str:
    """Minúsculas, sem acento e com espaços colapsados."""
    decomposto = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def _rio(data: dt.date, hora: int = 20) -> dt.datetime:
    """Datetime no fuso do Rio, via `localize` — nunca `replace(tzinfo=...)`."""
    return get_rio_timezone().localize(dt.datetime.combine(data, dt.time(hour=hora)))


# ===== simulação =====

# Os relógios que valem a pena visitar. "real" é o único que não mente; os
# outros existem para alcançar telas que, no calendário, só aparecem uma vez.
RELOGIOS: Dict[str, Callable[[], Optional[dt.datetime]]] = {
    "real": lambda: None,
    "durante": lambda: _rio(PRIMEIRO_DIA, 20),
    "ultima_madrugada": lambda: limite_do_festival() - dt.timedelta(hours=1),
    "encerrado": lambda: limite_do_festival() + dt.timedelta(seconds=1),
}

ROTULO_RELOGIO = {
    "real": "agora (relógio real)",
    "durante": f"{PRIMEIRO_DIA.strftime('%d/%m')} 20h — festival rolando",
    "ultima_madrugada": f"{ULTIMO_DIA.strftime('%d/%m')} 05h — última madrugada",
    "encerrado": "depois do fim — evento encerrado",
}


@dataclass
class Simulacao:
    """O que a barra de controles troca antes de cada chamada da tool."""

    relogio: str = "real"
    encurtador: str = "ok"  # ok | fora
    redis: str = "memoria"  # memoria | vazio | fora

    def agora(self) -> Optional[dt.datetime]:
        return RELOGIOS.get(self.relogio, RELOGIOS["real"])()


@dataclass
class Chamada:
    """O que a última chamada da tool fez por baixo — é o que o painel mostra."""

    resposta: Dict[str, Any] = field(default_factory=dict)
    payload_do_encurtador: Optional[Dict[str, Any]] = None
    encurtador_chamado: bool = False
    origem_do_link: str = ""
    cache_antes: Optional[str] = None
    cache_depois: Optional[str] = None


class Dubles:
    """Substitui Redis e encurtador por dublês inspecionáveis.

    O cache vive num dicionário de processo em vez de num Redis de verdade: o
    ponto do runner é ver o *comportamento* de cache (a segunda pergunta não
    chama o encurtador de novo), e subir um Redis para isso só afastaria quem
    quer abrir a página e conversar.
    """

    def __init__(self, real: bool) -> None:
        self.real = real
        self.cache: Dict[str, str] = {}
        self.chamada = Chamada()
        self._original_short_url = mapa_mod.get_short_url

    # ----- Redis -----

    def _ler(self, modo: str):
        async def ler():
            if modo == "fora":
                # Ergue de dentro do dublê para exercitar o try/except real de
                # `_ler_redis`? Não: `_ler_redis` é quem estamos substituindo.
                # Aqui o objetivo é só a tela — o teste unitário cobre o outro.
                return None
            if modo == "vazio":
                return None
            return self.cache.get(mapa_mod.CHAVE_REDIS)

        return ler

    def _gravar(self, modo: str):
        async def gravar(link: str, ttl_s: int):
            if modo in {"fora", "vazio"}:
                return
            self.cache[mapa_mod.CHAVE_REDIS] = link

        return gravar

    # ----- encurtador -----

    def _encurtador(self, modo: str):
        async def encurtar(**kwargs):
            self.chamada.encurtador_chamado = True
            self.chamada.payload_do_encurtador = dict(kwargs)
            if modo == "fora":
                return None
            if self.real:
                # Cria link de verdade. `get_short_url` já engole os erros e
                # devolve None, que é exatamente o caminho de fallback.
                return await self._original_short_url(**kwargs)
            return LINK_DUBLE

        return encurtar

    async def chamar(self, sim: Simulacao) -> Chamada:
        """Aplica os dublês, chama a tool e restaura tudo."""
        self.chamada = Chamada()
        self.chamada.cache_antes = self.cache.get(mapa_mod.CHAVE_REDIS)

        originais = {
            "_ler_redis": mapa_mod._ler_redis,
            "_gravar_redis": mapa_mod._gravar_redis,
            "get_short_url": mapa_mod.get_short_url,
            "datetime": mapa_mod.datetime,
        }
        agora = sim.agora()
        try:
            mapa_mod._ler_redis = self._ler(sim.redis)
            mapa_mod._gravar_redis = self._gravar(sim.redis)
            mapa_mod.get_short_url = self._encurtador(sim.encurtador)
            if agora is not None:

                class DatetimeFalso:
                    @staticmethod
                    def now(tz=None):
                        return agora

                mapa_mod.datetime = DatetimeFalso

            self.chamada.resposta = await mapa_mod.get_mapa_rock_in_rio()
        finally:
            for nome, valor in originais.items():
                setattr(mapa_mod, nome, valor)

        self.chamada.cache_depois = self.cache.get(mapa_mod.CHAVE_REDIS)
        self.chamada.origem_do_link = self._origem()
        return self.chamada

    def _origem(self) -> str:
        """De onde veio o link desta chamada — o mesmo eixo do span."""
        c = self.chamada
        if not c.resposta.get("disponivel"):
            return "encerrado"
        if not c.encurtador_chamado:
            return "redis"
        mensagem = c.resposta.get("mensagem") or ""
        if URL_DO_MAPA in mensagem:
            return "url_crua"
        # O dublê devolve `LINK_DUBLE`; qualquer outro link só pode ter vindo da
        # confirmação de `_link_ja_publicado`, que vai à rede de verdade.
        if LINK_DUBLE not in mensagem:
            return "confirmado"
        return "encurtador"


# ===== conversa =====


@dataclass
class Resposta:
    """Uma virada de conversa: o que aparece na tela e o que a sustenta."""

    bolhas: List[str]
    sugestoes: List[str] = field(default_factory=list)
    bruto: Any = None
    # Link que o card de preview deve desenhar. Vazio quando a resposta não
    # oferece mapa — é o que se confere na tela de evento encerrado.
    preview: str = ""


class Conversa:
    """Monta a resposta do bot. O roteamento é determinístico e mora aqui."""

    def __init__(self, dubles: Dubles, sim: Simulacao, via_mcp) -> None:
        self.dubles = dubles
        self.sim = sim
        self.via_mcp = via_mcp

    async def _chamar_a_tool(self) -> Chamada:
        if self.via_mcp is not None:
            chamada = Chamada()
            chamada.resposta = await self.via_mcp()
            chamada.origem_do_link = "servidor mcp"
            return chamada
        return await self.dubles.chamar(self.sim)

    async def mapa(self) -> Resposta:
        chamada = await self._chamar_a_tool()
        resposta = chamada.resposta
        mensagem = resposta.get("mensagem") or "(a tool não devolveu mensagem)"

        bolhas = [mensagem]
        disponivel = bool(resposta.get("disponivel"))
        if not disponivel:
            # A tool devolve o motivo para a LLM, não para o cidadão. Mostrar
            # aqui é do runner: quem testa precisa saber por que caiu nesta tela.
            bolhas.append(
                f"_(tool: disponivel=false, motivo={resposta.get('motivo')})_"
            )

        return Resposta(
            bolhas=bolhas,
            sugestoes=SUGESTOES,
            bruto={
                "resposta_da_tool": resposta,
                "origem_do_link": chamada.origem_do_link,
                "encurtador_chamado": chamada.encurtador_chamado,
                "payload_enviado_ao_encurtador": chamada.payload_do_encurtador,
                "cache": {
                    "chave": mapa_mod.CHAVE_REDIS,
                    "antes": chamada.cache_antes,
                    "depois": chamada.cache_depois,
                },
            },
            preview=self._link_da_mensagem(mensagem) if disponivel else "",
        )

    @staticmethod
    def _link_da_mensagem(mensagem: str) -> str:
        achado = re.search(r"https?://\S+", mensagem or "")
        return achado.group(0) if achado else ""

    def lineup(self) -> Resposta:
        """A fronteira entre as duas tools, dita em voz alta.

        Não chama a `rock_in_rio_lineup`: o ponto é ver que a pergunta **não**
        é desta tool. Se um dia esta tela aparecer para "me manda o mapa", é o
        `PADRAO_MAPA` que está errado.
        """
        return Resposta(
            bolhas=[
                "Essa pergunta é de programação — quem responde é a tool "
                "*rock_in_rio_lineup*, não a do mapa.",
                "Para ver essa conversa, rode o outro runner:\n"
                "`uv run python src/tests/e2e/run_chat_rock_in_rio.py`",
            ],
            sugestoes=SUGESTOES,
            bruto={"roteamento": "fora do escopo desta tool"},
        )

    def boas_vindas(self) -> Resposta:
        return Resposta(
            bolhas=[
                "Oi! Aqui é o teste da tool *rock_in_rio_mapa* 🗺️\n"
                "Ela faz uma coisa só: devolver o link do mapa da Cidade do Rock.",
                "Peça o mapa e confira a mensagem e o card de preview. "
                "Use a barra de cima para simular o relógio, o encurtador "
                "fora do ar e o cache.",
            ],
            sugestoes=SUGESTOES,
        )

    def ajuda(self) -> Resposta:
        return Resposta(
            bolhas=[
                "Dá para perguntar assim:\n"
                "• *me manda o mapa*\n"
                "• *onde fica o evento*\n"
                "• *como é a Cidade do Rock*\n\n"
                "E para ver a fronteira com a outra tool:\n"
                "• *quem toca sexta* (deve ser recusado aqui)"
            ],
            sugestoes=SUGESTOES,
        )

    def nao_entendi(self, texto: str) -> Resposta:
        return Resposta(
            bolhas=[
                f'Não entendi "{texto}".',
                "Esta tool só entrega o mapa da Cidade do Rock. "
                "Peça *o mapa* ou *onde fica o evento*.",
            ],
            sugestoes=SUGESTOES,
        )

    async def responder(self, texto: str) -> Resposta:
        norm = _normalizar(texto)
        if not norm:
            return self.ajuda()
        if norm in SAUDACOES:
            return self.boas_vindas()
        if norm in PEDIDOS_DE_AJUDA:
            return self.ajuda()

        # Mapa antes de line-up: "onde fica o Palco Mundo" casa com os dois, e
        # entre as duas tools essa pergunta é de mapa.
        if PADRAO_MAPA.search(norm):
            return await self.mapa()
        if PADRAO_LINEUP.search(norm):
            return self.lineup()
        return self.nao_entendi(texto.strip())


# ===== origem do dado, quando é via MCP =====


def _limpar_valor_env(valor: str) -> str:
    """Tira as aspas do `.env` — colá-las junto dá `401 invalid_token`."""
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
    # A tool publicada passa por `add_tool_version`, que embrulha em `data`.
    return resposta.get("data", resposta)


def procedencia_da_imagem() -> Dict[str, Any]:
    """HEAD na URL do mapa: tamanho, `last-modified` e `ETag`.

    Como não re-hospedamos a imagem, estes três campos são a única forma de
    perceber que o Rock in Rio trocou os bytes por trás da mesma URL. Falha
    vira mensagem, não exceção: a página tem de subir sem rede.
    """
    try:
        import httpx

        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            r = client.head(URL_DO_MAPA)
        tamanho = int(r.headers.get("content-length") or 0)
        return {
            "url": URL_DO_MAPA,
            "status": r.status_code,
            "content_type": r.headers.get("content-type", "?"),
            "tamanho_bytes": tamanho,
            "tamanho_mb": round(tamanho / 1_000_000, 2) if tamanho else None,
            "last_modified": r.headers.get("last-modified", "?"),
            "etag": r.headers.get("etag", "?"),
            "observacao": (
                "Se `last_modified` ou `etag` mudarem entre execuções, o Rock in "
                "Rio trocou a imagem sem que nada mude do nosso lado."
            ),
        }
    except Exception as erro:  # noqa: BLE001 - vira texto na tela
        return {"url": URL_DO_MAPA, "erro": f"{type(erro).__name__}: {erro}"}


# ===== estado =====


class Estado:
    """Serializa as chamadas: os dublês trocam atributos de módulo."""

    def __init__(self, dubles: Dubles, via_mcp) -> None:
        self.dubles = dubles
        self.via_mcp = via_mcp
        self.sim = Simulacao()
        self._trava = threading.Lock()

    def responder(self, texto: str) -> Resposta:
        with self._trava:
            conversa = Conversa(self.dubles, self.sim, self.via_mcp)
            try:
                return asyncio.run(conversa.responder(texto))
            except Exception as erro:  # noqa: BLE001 - vira bolha na tela
                return Resposta(
                    bolhas=[
                        f"A tool levantou: *{type(erro).__name__}*",
                        str(erro),
                    ],
                    sugestoes=SUGESTOES,
                    bruto={"excecao": f"{type(erro).__name__}: {erro}"},
                )

    def limpar_cache(self) -> None:
        with self._trava:
            self.dubles.cache.clear()

    def cabecalho(self) -> Dict[str, Any]:
        if self.via_mcp is not None:
            return {
                "titulo": "Mapa — via servidor MCP",
                "linha": "relógio, cache e encurtador são os do servidor",
                "simulavel": False,
            }
        cacheado = bool(self.dubles.cache)
        return {
            "titulo": "Mapa da Cidade do Rock",
            "linha": (
                f"{ROTULO_RELOGIO.get(self.sim.relogio, self.sim.relogio)} · "
                f"encurtador {'real' if self.dubles.real else 'dublê'}"
                f"{' (fora do ar)' if self.sim.encurtador == 'fora' else ''} · "
                f"cache {'com link' if cacheado else 'vazio'}"
            ),
            "simulavel": True,
        }


# A página inteira num literal só: um runner de teste que precisa de `static/`
# ao lado deixa de rodar quando alguém o copia para outro lugar.
PAGINA = """<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat local — Mapa do Rock in Rio</title>
<style>
  :root {
    --barra: #008069; --barra-texto: #ffffff; --fundo: #efeae2;
    --entrada: #ffffff; --saida: #d9fdd3; --texto: #111b21;
    --secundario: #667781; --campo: #ffffff; --painel: #ffffff;
    --borda: rgba(0,0,0,.08); --chip: #ffffff; --preview: #f5f6f6;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --barra: #202c33; --barra-texto: #e9edef; --fundo: #0b141a;
      --entrada: #202c33; --saida: #005c4b; --texto: #e9edef;
      --secundario: #8696a0; --campo: #2a3942; --painel: #111b21;
      --borda: rgba(255,255,255,.1); --chip: #202c33; --preview: #1b2429;
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #d2dbdc; color: var(--texto);
    display: flex; justify-content: center;
  }
  @media (prefers-color-scheme: dark) { body { background: #0b141a; } }

  .app {
    width: 100%; max-width: 620px; height: 100dvh;
    display: flex; flex-direction: column; background: var(--fundo);
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

  #simulacao {
    background: var(--painel); border-bottom: 1px solid var(--borda);
    padding: 8px 12px; display: flex; gap: 14px; flex-wrap: wrap;
    align-items: center; flex: 0 0 auto; font-size: 12px;
  }
  #simulacao.oculto { display: none; }
  #simulacao label { color: var(--secundario); margin-right: 4px; }
  #simulacao select {
    background: var(--campo); color: var(--texto); font-size: 12px;
    border: 1px solid var(--borda); border-radius: 6px; padding: 4px 6px;
    font-family: inherit;
  }
  #simulacao .grupo-sim { display: flex; align-items: center; }
  #limpar-cache {
    background: transparent; border: 1px solid var(--borda);
    color: var(--texto); border-radius: 12px; padding: 4px 10px;
    font-size: 12px; cursor: pointer; margin-left: auto;
  }

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
  .entrada { background: var(--entrada); align-self: flex-start; border-top-left-radius: 0; }
  .saida { background: var(--saida); align-self: flex-end; border-top-right-radius: 0; }
  .entrada::before, .saida::before {
    content: ""; position: absolute; top: 0; width: 8px; height: 13px;
  }
  .entrada::before {
    left: -8px; background: linear-gradient(225deg, var(--entrada) 50%, transparent 50%);
  }
  .saida::before {
    right: -8px; background: linear-gradient(135deg, var(--saida) 50%, transparent 50%);
  }
  .grupo { margin-top: 8px; }
  .bolha a { color: #027eb5; }
  @media (prefers-color-scheme: dark) { .bolha a { color: #53bdeb; } }
  .hora {
    font-size: 11px; color: var(--secundario); float: right;
    margin: 6px -2px -4px 8px;
  }

  /* Card de preview, na proporção que o WhatsApp usa para link com imagem. */
  .card {
    background: var(--preview); border-radius: 6px; overflow: hidden;
    margin: -2px -3px 6px; cursor: pointer;
  }
  .card .moldura {
    width: 100%; aspect-ratio: 1.91 / 1; background: rgba(0,0,0,.06);
    display: grid; place-items: center; overflow: hidden;
  }
  .card img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .card .falhou {
    font-size: 12px; color: var(--secundario); padding: 18px; text-align: center;
  }
  .card .texto { padding: 6px 9px 8px; }
  .card .texto b { font-size: 13.5px; display: block; }
  .card .texto span { font-size: 12.5px; color: var(--secundario); }
  .card .texto i {
    font-size: 11.5px; color: var(--secundario); font-style: normal;
    display: block; margin-top: 2px; text-transform: uppercase;
  }
  .aviso-card {
    font-size: 11.5px; color: var(--secundario); margin: 2px 0 0;
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
  #painel .topo button.ativo {
    background: var(--barra); color: #fff; border-color: transparent;
  }
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
    <div class="avatar">🗺️</div>
    <div class="quem"><b id="titulo">Mapa da Cidade do Rock</b><span id="linha">carregando…</span></div>
    <button id="btn-painel" title="Ver a resposta crua da tool">&lt;/&gt;</button>
  </header>

  <div id="simulacao">
    <div class="grupo-sim">
      <label for="sim-relogio">relógio</label>
      <select id="sim-relogio"></select>
    </div>
    <div class="grupo-sim">
      <label for="sim-encurtador">encurtador</label>
      <select id="sim-encurtador">
        <option value="ok">no ar</option>
        <option value="fora">fora do ar</option>
      </select>
    </div>
    <div class="grupo-sim">
      <label for="sim-redis">cache</label>
      <select id="sim-redis">
        <option value="memoria">ligado</option>
        <option value="vazio">sempre vazio</option>
        <option value="fora">fora do ar</option>
      </select>
    </div>
    <button id="limpar-cache" title="Esvazia o cache sem reiniciar">limpar cache</button>
  </div>

  <div id="conversa"></div>
  <div id="sugestoes"></div>

  <form id="barra" autocomplete="off">
    <input id="campo" placeholder="Peça o mapa da Cidade do Rock" autofocus>
    <button id="enviar" type="submit" title="Enviar">➤</button>
  </form>

  <div id="painel">
    <div class="topo">
      <b>o que sustenta a resposta</b>
      <button data-aba="fatia" class="ativo">fatia usada</button>
      <button data-aba="instrucoes">instruções p/ a LLM</button>
      <button data-aba="imagem">procedência da imagem</button>
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
const CARD = JSON.parse(document.getElementById("dados-card").textContent);

let ultimaFatia = null;
let abaAtual = "fatia";

const espera = (ms) => new Promise((r) => setTimeout(r, ms));

function escapar(t) {
  return t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function formatar(texto) {
  let html = escapar(texto);
  html = html.replace(/\\*([^*\\n]+)\\*/g, "<strong>$1</strong>");
  html = html.replace(/_\\(([^)]+)\\)_/g, '<em style="opacity:.7">($1)</em>');
  html = html.replace(/`([^`\\n]+)`/g, "<code>$1</code>");
  html = html.replace(/(https?:\\/\\/[^\\s<]+)/g,
    '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
  return html.replace(/\\n/g, "<br>");
}

function agora() {
  return new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

function addBolha(texto, lado, primeira, preview) {
  const div = document.createElement("div");
  div.className = "bolha " + lado + (primeira ? " grupo" : "");
  let html = "";
  if (preview) {
    // O card usa a imagem ORIGINAL, não o link curto: é o que o crawler do
    // WhatsApp buscaria em `image_url`. Se ele não aparecer aqui, também não
    // aparece lá.
    html +=
      '<div class="card" onclick="window.open(\\'' + preview + '\\',\\'_blank\\')">' +
        '<div class="moldura">' +
          '<img src="' + CARD.imagem + '" alt="" ' +
               'onerror="this.parentNode.innerHTML=\\'<div class=falhou>a imagem não carregou — ' +
               'o preview do WhatsApp também não teria thumbnail</div>\\'">' +
        "</div>" +
        '<div class="texto"><b>' + escapar(CARD.titulo) + "</b>" +
          "<span>" + escapar(CARD.descricao) + "</span>" +
          "<i>" + escapar(new URL(preview).hostname) + "</i>" +
        "</div>" +
      "</div>";
  }
  html += formatar(texto) + '<span class="hora">' + agora() + "</span>";
  div.innerHTML = html;
  conversa.appendChild(div);
  if (preview && CARD.tamanho_mb) {
    const aviso = document.createElement("div");
    aviso.className = "bolha entrada aviso-card";
    aviso.style.boxShadow = "none";
    aviso.style.background = "transparent";
    aviso.textContent =
      "↑ card montado aqui com a imagem de " + CARD.tamanho_mb +
      " MB. O WhatsApp costuma desistir de thumbnail desse tamanho — confira num aparelho real.";
    conversa.appendChild(aviso);
  }
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
  document.getElementById("simulacao").classList.toggle("oculto", !cab.simulavel);
}

async function mostrar(dados) {
  aplicarCabecalho(dados.cabecalho);
  renderSugestoes([]);
  digitando(true);
  await espera(260);
  digitando(false);
  for (let i = 0; i < dados.bolhas.length; i++) {
    addBolha(dados.bolhas[i], "entrada", i === 0, i === 0 ? dados.preview : "");
    if (i < dados.bolhas.length - 1) await espera(180);
  }
  renderSugestoes(dados.sugestoes);
  ultimaFatia = dados.bruto;
  if (painel.classList.contains("aberto")) pintarPainel();
}

async function enviar(texto) {
  const limpo = (texto || "").trim();
  if (!limpo) return;
  addBolha(limpo, "saida", true, "");
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
      : "Esta resposta não chamou a tool.";
  } else if (abaAtual === "instrucoes") {
    const inst =
      (ultimaFatia && ultimaFatia.resposta_da_tool &&
       ultimaFatia.resposta_da_tool.instrucoes_de_resposta) || "";
    conteudoPainel.textContent =
      inst || "A última resposta não veio da tool — peça o mapa primeiro.";
  } else {
    conteudoPainel.textContent = "carregando…";
    const r = await fetch("/api/imagem");
    conteudoPainel.textContent = JSON.stringify(await r.json(), null, 2);
  }
}

async function trocarSimulacao() {
  const corpo = {
    relogio: document.getElementById("sim-relogio").value,
    encurtador: document.getElementById("sim-encurtador").value,
    redis: document.getElementById("sim-redis").value,
  };
  const r = await fetch("/api/simulacao", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(corpo),
  });
  aplicarCabecalho((await r.json()).cabecalho);
}

document.getElementById("barra").onsubmit = (e) => {
  e.preventDefault();
  enviar(campo.value);
};
["sim-relogio", "sim-encurtador", "sim-redis"].forEach((id) => {
  document.getElementById(id).onchange = trocarSimulacao;
});
document.getElementById("limpar-cache").onclick = async () => {
  const r = await fetch("/api/limpar-cache", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: "{}" });
  aplicarCabecalho((await r.json()).cabecalho);
  addBolha("_(cache esvaziado — a próxima pergunta vai ao encurtador)_",
           "entrada", true, "");
};

document.getElementById("btn-painel").onclick = () => {
  painel.classList.toggle("aberto");
  if (painel.classList.contains("aberto")) pintarPainel();
};
document.getElementById("fechar-painel").onclick = () =>
  painel.classList.remove("aberto");

document.querySelectorAll("#painel .topo button[data-aba]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#painel .topo button[data-aba]")
      .forEach((o) => o.classList.remove("ativo"));
    b.classList.add("ativo");
    abaAtual = b.dataset.aba;
    pintarPainel();
  };
});

(async () => {
  const r = await fetch("/api/inicio");
  const dados = await r.json();
  const sel = document.getElementById("sim-relogio");
  dados.relogios.forEach((op) => {
    const o = document.createElement("option");
    o.value = op.valor;
    o.textContent = op.rotulo;
    sel.appendChild(o);
  });
  await mostrar(dados);
  campo.focus();
})();
</script>
</body>
</html>
"""


# ===== servidor =====

ESTADO: Optional[Estado] = None
CARD: Dict[str, Any] = {}


class Handler(BaseHTTPRequestHandler):
    server_version = "ChatMapaRockInRio/1.0"

    def _responder(self, corpo: bytes, tipo: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, dados: Any, status: int = 200) -> None:
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self._responder(corpo, "application/json; charset=utf-8", status)

    def _pagina(self) -> bytes:
        # Os dados do card entram como JSON num `<script type="application/json">`
        # em vez de interpolados no JS: assim nenhum caractere da descrição
        # precisa ser escapado à mão.
        dados = json.dumps(CARD, ensure_ascii=False)
        bloco = f'<script id="dados-card" type="application/json">{dados}</script>'
        return PAGINA.replace(
            "<script>\nconst conversa", bloco + "\n<script>\nconst conversa"
        ).encode("utf-8")

    def do_GET(self) -> None:  # noqa: N802 - assinatura da stdlib
        assert ESTADO is not None
        if self.path in {"/", "/index.html"}:
            self._responder(self._pagina(), "text/html; charset=utf-8")
        elif self.path == "/api/inicio":
            resposta = ESTADO.responder("oi")
            self._json(
                {
                    "cabecalho": ESTADO.cabecalho(),
                    "bolhas": resposta.bolhas,
                    "sugestoes": resposta.sugestoes,
                    "bruto": resposta.bruto,
                    "preview": resposta.preview,
                    "relogios": [
                        {"valor": chave, "rotulo": ROTULO_RELOGIO[chave]}
                        for chave in RELOGIOS
                    ],
                }
            )
        elif self.path == "/api/imagem":
            self._json(procedencia_da_imagem())
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

        if self.path == "/api/simulacao":
            ESTADO.sim = Simulacao(
                relogio=str(corpo.get("relogio") or "real"),
                encurtador=str(corpo.get("encurtador") or "ok"),
                redis=str(corpo.get("redis") or "memoria"),
            )
            self._json({"cabecalho": ESTADO.cabecalho()})
            return

        if self.path == "/api/limpar-cache":
            ESTADO.limpar_cache()
            self._json({"cabecalho": ESTADO.cabecalho()})
            return

        if self.path == "/api/mensagem":
            resposta = ESTADO.responder(str(corpo.get("texto") or ""))
            self._json(
                {
                    "cabecalho": ESTADO.cabecalho(),
                    "bolhas": resposta.bolhas,
                    "sugestoes": resposta.sugestoes,
                    "bruto": resposta.bruto,
                    "preview": resposta.preview,
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
        description="Chat local com cara de WhatsApp para testar o mapa do Rock in Rio."
    )
    parser.add_argument("--porta", type=int, default=PORTA_PADRAO)
    parser.add_argument(
        "--bind",
        default=BIND_PADRAO,
        help=(
            "Padrão 127.0.0.1. Para mostrar a alguém, prefira `tailscale serve "
            "--bg 8101` a abrir para a rede local com 0.0.0.0."
        ),
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help=(
            "Chama o encurtador de verdade em vez do dublê. CRIA LINK REAL, e "
            "em PRODUÇÃO (pref.rio) — não há encurtador de homologação configurado."
        ),
    )
    parser.add_argument(
        "--mcp",
        action="store_true",
        help=(
            "Chama a tool pelo servidor MCP em vez de no processo. Exercita o "
            "nome registrado, a autenticação e a serialização da resposta. "
            "Desliga a simulação: relógio, cache e encurtador são os do servidor."
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
    global ESTADO, CARD
    args = parse_args()

    via_mcp = None
    if args.mcp:
        token = args.token or token_padrao()
        if not token:
            print(
                "FALHA: --mcp precisa de token. Passe --token ou configure "
                f"VALID_TOKENS em {ENV_PADRAO}."
            )
            return 1
        print(f"Chamando {NOME_DA_TOOL} via MCP em {args.url}")

        async def via_mcp() -> Dict[str, Any]:  # noqa: F811 - fecha sobre args
            return await carregar_via_mcp(args.url, token)
    elif args.real:
        print(
            f"Chamando {NOME_DA_TOOL} no próprio processo, com o encurtador REAL.\n"
            "        Cada cache miss cria um link de verdade."
        )
    else:
        print(f"Chamando {NOME_DA_TOOL} no próprio processo, com encurtador dublê")

    procedencia = procedencia_da_imagem()
    CARD = {
        "imagem": URL_DO_MAPA,
        "titulo": TITULO,
        "descricao": DESCRICAO,
        "tamanho_mb": procedencia.get("tamanho_mb"),
    }
    if procedencia.get("erro"):
        print(f"AVISO: não consegui inspecionar a imagem — {procedencia['erro']}")
    else:
        print(
            f"Imagem: {procedencia.get('tamanho_mb')} MB · "
            f"{procedencia.get('content_type')} · "
            f"modificada em {procedencia.get('last_modified')}"
        )

    ESTADO = Estado(Dubles(real=args.real), via_mcp)

    endereco = f"http://{args.bind}:{args.porta}/"
    try:
        servidor = ThreadingHTTPServer((args.bind, args.porta), Handler)
    except OSError as erro:
        if erro.errno != errno.EADDRINUSE:
            raise
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
