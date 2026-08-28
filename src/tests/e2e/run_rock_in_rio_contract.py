"""Contrato do line-up do Rock in Rio contra o site oficial (CHATR-187).

Este runner existe por um motivo só: **avisar quando o site mudar**. A tool
depende de raspar HTML de um site de terceiro que pode mudar de tema a qualquer
momento, sem aviso, inclusive na véspera do festival. Os testes unitários rodam
sobre fixtures salvas e continuariam verdes nesse cenário.

Mora em `src/tests/e2e/` com prefixo `run_` para não ser coletado pelo pytest —
mesma convenção de `run_pgm_contract.py`. Chamada de rede não pode entrar no
caminho do CI: uma indisponibilidade momentânea do rockinrio.com reprovaria um
PR que não tem nada a ver com isso.

Execução:

    uv run python src/tests/e2e/run_rock_in_rio_contract.py

Sai com código 0 se o contrato está de pé, 1 se algo mudou.
"""

import asyncio
import sys
from collections import Counter

sys.path.insert(0, ".")

from src.tools.rock_in_rio.scraper import (  # noqa: E402
    DIAS_DO_EVENTO,
    LineupInvalido,
    buscar_lineup,
)

# Faixas, e não números exatos: uma atração a mais ou a menos é alteração
# legítima de line-up, e reprovar por isso transformaria o runner em ruído. O
# que precisa disparar é a mudança de ordem de grandeza — tipicamente o parser
# tendo parado de casar com o HTML.
MIN_ATRACOES_POR_DIA = 12
MAX_ATRACOES_POR_DIA = 60

PALCOS_CONHECIDOS = {
    "Palco Mundo",
    "Palco Sunset",
    "New Dance Order",
    "Espaço Favela",
    "Global Village",
    "Supernova",
}


async def main() -> int:
    print("Consultando o site oficial do Rock in Rio...\n")

    try:
        shows = await buscar_lineup()
    except LineupInvalido as erro:
        print(f"FALHA: o HTML do site não tem mais o formato esperado.\n  {erro}")
        return 1
    except Exception as erro:
        print(
            f"FALHA: não foi possível baixar as páginas.\n  {type(erro).__name__}: {erro}"
        )
        return 1

    problemas: list[str] = []
    por_dia = Counter(show.dia_slug for show in shows)

    for slug, data in DIAS_DO_EVENTO:
        total = por_dia.get(slug, 0)
        palcos = sorted({s.palco for s in shows if s.dia_slug == slug})
        print(
            f"  {data.strftime('%d/%m')} ({slug}): {total:3d} atrações | {len(palcos)} palcos"
        )

        if not MIN_ATRACOES_POR_DIA <= total <= MAX_ATRACOES_POR_DIA:
            problemas.append(
                f"{slug}: {total} atrações, fora da faixa esperada "
                f"({MIN_ATRACOES_POR_DIA}-{MAX_ATRACOES_POR_DIA})"
            )

        desconhecidos = set(palcos) - PALCOS_CONHECIDOS
        if desconhecidos:
            problemas.append(f"{slug}: palco não catalogado {sorted(desconhecidos)}")

    sem_nome = [s for s in shows if not s.artista.strip()]
    if sem_nome:
        problemas.append(f"{len(sem_nome)} atrações sem nome de artista")

    # O site publica os horários? Se um dia publicar, a limitação central da
    # tool cai e vale reabrir o escopo — este runner é o lugar mais provável
    # para alguém perceber isso.
    print(f"\n  Total: {len(shows)} atrações em {len(por_dia)} dias")

    if problemas:
        print("\nFALHA: o contrato com o site mudou.")
        for problema in problemas:
            print(f"  - {problema}")
        return 1

    print("\nOK: o contrato com o site oficial está de pé.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
