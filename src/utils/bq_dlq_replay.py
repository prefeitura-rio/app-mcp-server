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

Para o poison — os payloads que o BigQuery recusou em definitivo, que o drain
automático nunca reprocessa porque repeti-los não muda o desfecho:

    # o que está lá e por quê (sem consumir; conteúdo do payload omitido)
    python -m src.utils.bq_dlq_replay --poison

    # depois de corrigir a causa (schema ajustado, coluna criada)
    python -m src.utils.bq_dlq_replay --requeue-poison --table <tabela>

    # quando a conclusão for que é irrecuperável
    python -m src.utils.bq_dlq_replay --purge-poison --table <tabela> --confirmar

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

    # Operações sobre o poison — a fila dos payloads que o BigQuery recusou em
    # definitivo. Até existirem, o poison era write-only: nada o lia de volta e
    # a única saída era o TTL expirando, em silêncio.
    poison = parser.add_argument_group("poison")
    poison.add_argument(
        "--poison",
        action="store_true",
        help="Lista o que está em poison, sem consumir nem alterar nada.",
    )
    poison.add_argument(
        "--mostrar-payload",
        action="store_true",
        dest="mostrar_payload",
        help=(
            "Inclui o conteúdo do payload na listagem. O payload carrega dado "
            "pessoal (telefone, endereço, coordenada) — por isso é opt-in."
        ),
    )
    poison.add_argument(
        "--requeue-poison",
        action="store_true",
        dest="requeue_poison",
        help="Devolve itens do poison para a DLQ normal (use após corrigir a causa).",
    )
    poison.add_argument(
        "--purge-poison",
        action="store_true",
        dest="purge_poison",
        help="Apaga o poison em definitivo. Exige --confirmar.",
    )
    poison.add_argument(
        "--confirmar",
        action="store_true",
        help="Confirma uma operação destrutiva (--purge-poison).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    # Import adiado, e por modo: `src.utils.bigquery` puxa `src.config.env`, que
    # aborta se faltar variável. Deixar o parse de argumentos acontecer antes
    # garante que `--help` funcione em qualquer ambiente.
    if args.depth_only:
        return _modo_profundidade(args)
    if args.poison:
        return _modo_inspecionar_poison(args)
    if args.requeue_poison:
        return _modo_reenfileirar_poison(args)
    if args.purge_poison:
        return _modo_descartar_poison(args)
    return _modo_replay(args)


def _modo_profundidade(args) -> int:
    from src.utils.bigquery import get_dlq_depth

    resultado = get_dlq_depth()
    print(
        json.dumps(resultado, ensure_ascii=False)
        if args.as_json
        else f"DLQ: {resultado['total']} item(ns) — redis={resultado['redis']} "
        f"poison={resultado['poison']} arquivos={resultado['arquivos']}"
    )
    return 0


def _modo_inspecionar_poison(args) -> int:
    """Lista o poison. Não consome, não reprocessa, não altera TTL.

    É a operação que vem antes de decidir entre `--requeue-poison` e
    `--purge-poison`: sem ler o erro que o BigQuery devolveu, não há como saber
    qual das duas é a certa.
    """
    from src.utils.bigquery import inspecionar_poison

    resultado = inspecionar_poison(
        limite=args.limit,
        table_full_name=args.table,
        incluir_payload=args.mostrar_payload,
    )

    if args.as_json:
        print(json.dumps(resultado, ensure_ascii=False))
    elif not resultado["itens"]:
        print("Nenhum item em poison.")
    else:
        print(f"{resultado['total']} item(ns) em poison:")
        for item in resultado["itens"]:
            print(
                f"  [{item['origem']}] {item.get('tabela')} — "
                f"{item.get('linhas', 0)} linha(s), falhou em {item.get('failed_at')}"
            )
            print(f"      erro: {item.get('erro')}")
            if item.get("campos"):
                print(f"      campos: {', '.join(item['campos'])}")
            if "payload" in item:
                print(
                    f"      payload: {json.dumps(item['payload'], ensure_ascii=False)}"
                )
        if not args.mostrar_payload:
            print(
                "\nConteúdo omitido (dado pessoal). Use --mostrar-payload para incluí-lo."
            )

    for erro in resultado["erros"]:
        print(f"  erro: {erro}", file=sys.stderr)
    return 1 if resultado["erros"] else 0


def _modo_reenfileirar_poison(args) -> int:
    """Devolve o poison à DLQ normal, para o worker de drain entregar.

    Não valida se a causa foi corrigida — não tem como. Se não foi, o drain
    recusa os itens de novo e eles voltam ao poison: uma tentativa perdida, e o
    caminho de volta continua existindo.
    """
    from src.utils.bigquery import reenfileirar_poison

    resumo = reenfileirar_poison(limite=args.limit, table_full_name=args.table)

    if args.as_json:
        print(json.dumps(resumo, ensure_ascii=False))
    else:
        print(
            f"{resumo['itens']} item(ns) / {resumo['linhas']} linha(s) devolvidos "
            f"do poison para a DLQ; {resumo['pendentes']} ainda em poison."
        )
        if resumo["itens"]:
            print(
                "O worker de drain os entrega na próxima varredura. Se a causa não "
                "tiver sido corrigida, eles voltam ao poison."
            )

    for erro in resumo["erros"]:
        print(f"  erro: {erro}", file=sys.stderr)
    return 1 if resumo["erros"] else 0


def _modo_descartar_poison(args) -> int:
    """Apaga o poison em definitivo. Exige confirmação explícita.

    O TTL faz a mesma coisa sozinho e em silêncio; a diferença aqui é que a
    perda é decidida por alguém e fica registrada em log. Por isso a barreira do
    `--confirmar`: um descarte disparado por engano é irreversível.
    """
    if not args.confirmar:
        alvo = args.table or "TODAS as tabelas"
        print(
            f"--purge-poison apaga em definitivo o poison de {alvo}. "
            f"Confira antes com --poison e repita com --confirmar.",
            file=sys.stderr,
        )
        return 2

    from src.utils.bigquery import descartar_poison

    resumo = descartar_poison(table_full_name=args.table)

    if args.as_json:
        print(json.dumps(resumo, ensure_ascii=False))
    else:
        print(
            f"{resumo['itens']} item(ns) do poison descartados "
            f"({resumo['chaves']} chave(s)/arquivo(s))."
        )

    for erro in resumo["erros"]:
        print(f"  erro: {erro}", file=sys.stderr)
    return 1 if resumo["erros"] else 0


def _modo_replay(args) -> int:
    from src.utils.bigquery import replay_bigquery_dlq

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
