#!/usr/bin/env python3
"""Roda o dataset rotulado do filtro de query (CHATR-294) e compara com o rótulo.

Dois modos:

- `mcp` (padrão): chama a tool `google_search` no MCP local, uma vez por linha.
  "Bloqueado" é a resposta de sucesso cujo `text` é `QUERY_BLOCKED_MESSAGE`. Todo o
  resto, inclusive `isError`, conta como "passou pelo filtro".
- `offline`: aplica o filtro em processo, sem servidor nem custo, e mostra o termo
  que casou. Serve para revisar o dataset e a lista antes de subir o servidor.

O resultado sai num CSV com as colunas do dataset mais `obtido`, `ok` e
`detalhe`. O processo termina com código 1 se alguma linha divergir do rótulo.

Exemplos (da raiz do repositório):

    uv run python src/tests/e2e/filtro_query/run_filtro_query.py --modo offline
    uv run python src/tests/e2e/filtro_query/run_filtro_query.py \
        --url http://localhost:80/mcp --concorrencia 4
"""

import argparse
import ast
import asyncio
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DATASET_PADRAO = Path(__file__).with_name("dataset.csv")
SEARCH_PATH = PROJECT_ROOT / "src" / "tools" / "search.py"
ENV_FILE = PROJECT_ROOT / "src" / "config" / ".env"

BLOQUEAR, PERMITIR, ERRO = "bloquear", "permitir", "erro"


def mensagem_de_bloqueio() -> str:
    """Lê `QUERY_BLOCKED_MESSAGE` do código-fonte, sem importar `search.py` (que
    puxaria o env inteiro). Assim a troca do placeholder pelo texto final não
    exige mexer aqui."""
    arvore = ast.parse(SEARCH_PATH.read_text(encoding="utf-8"))
    for no in arvore.body:
        if isinstance(no, ast.Assign) and any(
            isinstance(alvo, ast.Name) and alvo.id == "QUERY_BLOCKED_MESSAGE"
            for alvo in no.targets
        ):
            return ast.literal_eval(no.value)
    raise SystemExit(f"QUERY_BLOCKED_MESSAGE não encontrada em {SEARCH_PATH}")


def token_padrao() -> str:
    """`MCP_API_TOKEN`, ou o primeiro de `VALID_TOKENS` (ambiente ou .env local)."""
    if os.environ.get("MCP_API_TOKEN"):
        return os.environ["MCP_API_TOKEN"].strip()
    valor = os.environ.get("VALID_TOKENS", "")
    if not valor and ENV_FILE.exists():
        for linha in ENV_FILE.read_text(encoding="utf-8").splitlines():
            chave, _, resto = linha.partition("=")
            if chave.strip() == "VALID_TOKENS":
                valor = resto.strip().strip("'\"")
    return valor.split(",")[0].strip()


def ler_dataset(path: Path, categorias: set[str]) -> list[dict]:
    # `utf-8-sig` tolera o BOM que Excel e Sheets gravam ao exportar.
    with path.open(encoding="utf-8-sig", newline="") as f:
        linhas = list(csv.DictReader(f))
    if categorias:
        linhas = [linha for linha in linhas if linha["categoria"] in categorias]
    return linhas


# --------------------------------------------------------------------------------
# Modo offline
# --------------------------------------------------------------------------------


def rodar_offline(linhas: list[dict]) -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.tools.google_search.query_filter import query_filter

    for linha in linhas:
        decisao = query_filter.check(linha["query"])
        linha["obtido"] = PERMITIR if decisao.allowed else BLOQUEAR
        linha["detalhe"] = (
            f"{decisao.reason}: {decisao.matched}" if decisao.reason else ""
        )


# --------------------------------------------------------------------------------
# Modo MCP
# --------------------------------------------------------------------------------


def _texto_da_resposta(resultado) -> str | None:
    """`text` da resposta da tool, venha em `structuredContent` ou em JSON no
    primeiro bloco de conteúdo."""
    estruturado = resultado.structuredContent or {}
    estruturado = estruturado.get("result", estruturado)
    if isinstance(estruturado, dict) and "text" in estruturado:
        return estruturado["text"]
    for bloco in resultado.content or []:
        try:
            corpo = json.loads(getattr(bloco, "text", "") or "")
        except json.JSONDecodeError:
            continue
        if isinstance(corpo, dict):
            return corpo.get("text")
    return None


async def rodar_mcp(
    linhas: list[dict], url: str, token: str, concorrencia: int, timeout: float
) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    bloqueio = mensagem_de_bloqueio()
    semaforo = asyncio.Semaphore(concorrencia)
    headers = {"Authorization": f"Bearer {token}"} if token else None

    async with streamablehttp_client(url, headers=headers, timeout=timeout) as (
        leitura,
        escrita,
        _,
    ):
        async with ClientSession(leitura, escrita) as sessao:
            await sessao.initialize()

            async def uma(linha: dict, posicao: int) -> None:
                async with semaforo:
                    inicio = time.monotonic()
                    try:
                        resultado = await sessao.call_tool(
                            "google_search", {"query": linha["query"]}
                        )
                    except Exception as erro:
                        linha["obtido"] = ERRO
                        linha["detalhe"] = f"{type(erro).__name__}: {erro}"[:200]
                    else:
                        texto = _texto_da_resposta(resultado)
                        bloqueada = not resultado.isError and texto == bloqueio
                        linha["obtido"] = BLOQUEAR if bloqueada else PERMITIR
                        linha["detalhe"] = (
                            "isError (passou pelo filtro)" if resultado.isError else ""
                        )
                    ms = int((time.monotonic() - inicio) * 1000)
                    linha["detalhe"] = f"{linha['detalhe']} [{ms} ms]".strip()
                    print(
                        f"[{posicao}/{len(linhas)}] {linha['id']} "
                        f"{linha['obtido']:<9} {linha['query'][:60]!r}",
                        flush=True,
                    )

            await asyncio.gather(*(uma(linha, i) for i, linha in enumerate(linhas, 1)))


# --------------------------------------------------------------------------------
# Relatório
# --------------------------------------------------------------------------------


def gravar_e_resumir(linhas: list[dict], saida: Path) -> int:
    for linha in linhas:
        linha["ok"] = "sim" if linha["obtido"] == linha["esperado"] else "nao"

    campos = list(linhas[0].keys())
    with saida.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(linhas)

    matriz = Counter((linha["esperado"], linha["obtido"]) for linha in linhas)
    divergentes = [linha for linha in linhas if linha["ok"] == "nao"]
    print(
        f"\nTotal: {len(linhas)} | iguais ao rótulo: {len(linhas) - len(divergentes)}"
    )
    print("esperado  x obtido   : linhas")
    for (esperado, obtido), n in sorted(matriz.items()):
        print(f"{esperado:<9} x {obtido:<9}: {n}")

    por_categoria = Counter(linha["categoria"] for linha in divergentes)
    if divergentes:
        print("\nDivergências por categoria:")
        for categoria, n in por_categoria.most_common():
            print(f"  {categoria}: {n}")
        print("\nDivergências:")
        for linha in divergentes:
            print(
                f"  {linha['id']} esperado={linha['esperado']} "
                f"obtido={linha['obtido']} {linha['query']!r} {linha['detalhe']}"
            )
    print(f"\nResultado completo: {saida}")
    return 1 if divergentes else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--modo", choices=["mcp", "offline"], default="mcp")
    parser.add_argument("--dataset", type=Path, default=DATASET_PADRAO)
    parser.add_argument(
        "--saida", type=Path, default=Path("filtro_query_resultado.csv")
    )
    parser.add_argument(
        "--url", default=os.environ.get("MCP_URL", "http://localhost:80/mcp")
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Padrão: MCP_API_TOKEN ou o primeiro de VALID_TOKENS.",
    )
    parser.add_argument("--concorrencia", type=int, default=4)
    parser.add_argument(
        "--timeout", type=float, default=60, help="Segundos por chamada HTTP."
    )
    parser.add_argument(
        "--categoria",
        action="append",
        default=[],
        help="Roda só esta categoria. Pode repetir.",
    )
    args = parser.parse_args()

    linhas = ler_dataset(args.dataset, set(args.categoria))
    if not linhas:
        print("Nenhuma linha selecionada.")
        return 1

    if args.modo == "offline":
        rodar_offline(linhas)
    else:
        token = args.token if args.token is not None else token_padrao()
        asyncio.run(rodar_mcp(linhas, args.url, token, args.concorrencia, args.timeout))
    return gravar_e_resumir(linhas, args.saida)


if __name__ == "__main__":
    sys.exit(main())
