"""Regrava as fixtures do line-up do Rock in Rio a partir do site oficial.

Existe porque as fixtures são o ponto cego dos testes unitários: elas rodam
sobre HTML salvo e continuam verdes quando o site muda — foi exatamente o que
aconteceu em 01/09/2026, quando a tool ficou fora do ar com a suíte toda verde.
Quando o site mudar de novo, regravar precisa ser barato, senão não é feito.

    uv run python scripts/regravar_fixtures_rock_in_rio.py

Baixa os sete dias, **valida cada um pelo parser antes de gravar** e só então
substitui os arquivos. A validação não é zelo: gravar uma página que o parser
não lê trocaria um teste que falha por um teste que passa sobre lixo.

Depois de rodar, os números dos testes precisam ser reconferidos — o script
imprime a contagem por dia e o total justamente para isso.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

from src.tools.rock_in_rio.scraper import (  # noqa: E402
    DIAS_DO_EVENTO,
    DAY_URL_TEMPLATE,
    LineupInvalido,
    _USER_AGENT,
    TIMEOUT_S,
    parse_dia,
)

DESTINO = Path("src/tests/fixtures/rock_in_rio")


async def main() -> int:
    import httpx

    DESTINO.mkdir(parents=True, exist_ok=True)
    total = 0
    palcos: list[str] = []
    baixadas: dict[str, str] = {}

    async with httpx.AsyncClient(
        timeout=TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for slug, data in DIAS_DO_EVENTO:
            url = DAY_URL_TEMPLATE.format(slug=slug)
            resposta = await client.get(url)
            if resposta.status_code != 200:
                print(f"FALHA: {url} devolveu HTTP {resposta.status_code}")
                return 1

            # Valida antes de gravar: uma fixture que o parser não lê é pior
            # que a fixture velha, porque some com o sinal de erro.
            try:
                shows = parse_dia(resposta.text, dia_slug=slug, data=data)
            except LineupInvalido as erro:
                print(f"FALHA: o parser não lê a página de {slug} — {erro}")
                print("Conserte o parser antes de regravar a fixture.")
                return 1

            baixadas[slug] = resposta.text
            total += len(shows)
            for show in shows:
                if show.palco not in palcos:
                    palcos.append(show.palco)
            print(f"  {slug}: {len(shows):3d} atrações")

    for slug, html in baixadas.items():
        (DESTINO / f"dia-{slug}.html").write_text(html, encoding="utf-8")

    print(f"\n  Total: {total} atrações")
    print(f"  Palcos (na ordem do documento): {palcos}")
    print(
        "\nOK: sete fixtures regravadas. Reconfira os números em "
        "src/tests/unit/tools/test_rock_in_rio_scraper.py e o catálogo de palcos "
        "em src/tests/e2e/run_rock_in_rio_contract.py."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
