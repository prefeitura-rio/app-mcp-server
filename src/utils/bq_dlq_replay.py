"""Reprocessamento manual da dead-letter queue de escrita do BigQuery.

Complemento operacional do worker automático (`drain_bigquery_dlq_loop`): o
worker é conservador de propósito — diante de erro transitório ele para e tenta
de novo depois, para não empilhar falha sobre falha enquanto o BigQuery está
fora. Isso resolve o caso comum sozinho, mas deixa dois em que alguém precisa
olhar: conferir o que está parado antes de mexer, e reprocessar na hora depois
de corrigir a causa, sem esperar o próximo ciclo.

Uso:

    # o que está parado, sem consumir nada
    python -m src.utils.bq_dlq_replay --dry-run

    # reprocessa até 500 itens de todas as tabelas
    python -m src.utils.bq_dlq_replay --limit 500

    # reprocessa só uma tabela
    python -m src.utils.bq_dlq_replay --table rj-iplanrio.brutos_eai_logs.feedback

O processo carrega `src.config.env`, ou seja, exige o mesmo ambiente do
servidor (credenciais do GCP e `REDIS_URL`). O jeito normal de rodar é dentro
do pod:

    kubectl exec -it deploy/mcp -- uv run python -m src.utils.bq_dlq_replay --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bq_dlq_replay",
        description="Devolve ao BigQuery as escritas paradas na DLQ.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Máximo de itens a reprocessar (padrão: BIGQUERY_DLQ_DRAIN_BATCH).",
    )
    parser.add_argument(
        "--table",
        default=None,
        help="Reprocessa apenas esta tabela (ex.: projeto.dataset.tabela).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Só relata o que existe na DLQ; não escreve nada e não consome a fila.",
    )
    parser.add_argument(
        "--depth-only",
        action="store_true",
        help="Imprime apenas a profundidade da DLQ e termina.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Saída em JSON, para consumo por script.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    # Import adiado: `src.utils.bigquery` puxa `src.config.env`, que aborta se
    # faltar variável. Deixar o parse de argumentos acontecer antes garante que
    # `--help` funcione em qualquer ambiente.
    from src.utils.bigquery import get_dlq_depth, replay_bigquery_dlq

    if args.depth_only:
        resultado = get_dlq_depth()
        print(
            json.dumps(resultado, ensure_ascii=False)
            if args.as_json
            else f"DLQ: {resultado['total']} item(ns) — redis={resultado['redis']} "
            f"poison={resultado['poison']} arquivos={resultado['arquivos']}"
        )
        return 0

    resumo = replay_bigquery_dlq(
        limite=args.limit, table_full_name=args.table, dry_run=args.dry_run
    )

    if args.as_json:
        print(json.dumps(resumo, ensure_ascii=False))
    else:
        prefixo = "[dry-run] " if args.dry_run else ""
        print(
            f"{prefixo}{resumo['itens']} item(ns) / {resumo['linhas']} linha(s); "
            f"{resumo['poison']} em poison; {resumo['pendentes']} pendente(s)."
        )
        for erro in resumo["erros"]:
            print(f"  erro: {erro}", file=sys.stderr)

    # Erro transitório vira código de saída não-zero para o operador (ou o job
    # que chamou) perceber que a fila não escoou por completo. Item em poison
    # não conta aqui: ele saiu da fila com sucesso, só precisa de correção.
    return 1 if resumo["erros"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
