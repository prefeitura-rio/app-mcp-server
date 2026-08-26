#!/usr/bin/env python3
"""
Verifica o contrato da API da PGM contra o que o código espera (CHATR-164).

Diferente dos outros runners desta pasta, que exercitam o MCP por HTTP, este
fala com a PGM pelo mesmo caminho da produção (`internal_request` ->
chatbot-integrations) e checa a **forma** da resposta: os campos que o código lê
continuam existindo, com os tipos e as relações que assumimos.

Existe porque a resposta de emissão da PGM não é documentada e já nos
surpreendeu: descobrimos por log que ela não devolve valor, natureza nem id de
guia, depois de uma implementação inteira ter sido escrita supondo que sim
(ver docs/decisions/CHATR-164-valor-e-itens.md).

## PII

A resposta da PGM carrega nome, CPF e endereço do cidadão. Nada disso é
impresso: o relatório mostra nomes de campos, contagens e valores monetários,
nunca o conteúdo dos campos identificadores.

## Execução

Credenciais em `src/config/.env` (carregado automaticamente) ou no ambiente:
CHATBOT_INTEGRATIONS_URL, CHATBOT_INTEGRATIONS_KEY, CHATBOT_PGM_API_URL,
CHATBOT_PGM_ACCESS_KEY.

    # Só consulta — read-only, seguro.
    uv run python src/tests/e2e/run_pgm_contract.py --cpf 12345678901

    # Também emite guia. ATENÇÃO: gera guia de verdade na PGM.
    uv run python src/tests/e2e/run_pgm_contract.py --cpf 12345678901 --emitir
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from src.tools.divida_ativa import (  # noqa: E402
    _itens_da_consulta,
    consultar_debitos,
    pgm_api,
    processar_registros,
)
from src.tools.valores_pagamento import (  # noqa: E402
    valor_brl_para_numero,
    valor_do_codigo_de_barras,
    valor_do_pix,
)


ENDPOINT_CONSULTA = "v2/cdas/dividas-contribuinte"
CONSUMIDOR_CONSULTA = "consultar-dividas-contribuinte"
ENDPOINT_EMISSAO = "v2/guiapagamento/emitir/avista"
CONSUMIDOR_EMISSAO = "emitir-guia-vista"

# Campos que o código lê de cada estrutura. Sumiço de qualquer um destes é
# quebra de contrato, mesmo que a chamada continue devolvendo 200.
CAMPOS_CONSULTA = ("naturezasDivida", "debitosNaoParceladosComSaldoTotal")
CAMPOS_DEBITOS = (
    "cdasNaoAjuizadasNaoParceladas",
    "efsNaoParceladas",
    "saldoTotalNaoParcelado",
)
CAMPOS_CDA = ("cdaId", "naturezaDivida", "naturezaDividaGrupoId", "valorSaldoTotal")
CAMPOS_EF = ("numeroExecucaoFiscal", "saldoExecucaoFiscalNaoParcelada")
CAMPOS_GUIA_PARCELADA = ("numero",)
CAMPOS_EMISSAO = ("dataVencimento", "pdf", "codigoDeBarras", "codigoQrEMVPix")

# Nunca impressos, nem em diagnóstico de campo faltante.
CAMPOS_PII = {
    "nomeContribuinte",
    "nomeRequerente",
    "cpf_Cnpj",
    "cpfCnpj",
    "enderecoImovel",
    "bairroImovel",
    "codigoInscricaoImobiliaria",
    "arquivoBase64",
}


class Relatorio:
    """Acumula os checks e decide o código de saída."""

    def __init__(self) -> None:
        self.falhas: List[str] = []
        self.avisos: List[str] = []
        self.verificados = 0

    def ok(self, descricao: str, detalhe: str = "") -> None:
        sufixo = f" — {detalhe}" if detalhe else ""
        self.verificados += 1
        print(f"  \033[32mOK\033[0m   {descricao}{sufixo}")

    def falhou(self, descricao: str, detalhe: str = "") -> None:
        sufixo = f" — {detalhe}" if detalhe else ""
        print(f"  \033[31mFALHA\033[0m {descricao}{sufixo}")
        self.falhas.append(descricao)

    def aviso(self, descricao: str) -> None:
        print(f"  \033[33mAVISO\033[0m {descricao}")
        self.avisos.append(descricao)

    def secao(self, titulo: str) -> None:
        print(f"\n\033[1m{titulo}\033[0m")

    def encerrar(self) -> int:
        print()
        if self.falhas:
            print(f"\033[31m{len(self.falhas)} quebra(s) de contrato:\033[0m")
            for falha in self.falhas:
                print(f"  - {falha}")
            return 1

        # Sem nenhum check aprovado, "íntegro" seria falso conforto: quer
        # dizer que nada foi exercitado, não que está tudo certo.
        if self.verificados <= 1:
            print("\033[33mNada verificado — o contrato segue desconhecido\033[0m")
            for aviso in self.avisos:
                print(f"  - {aviso}")
            return 2

        resumo = f"Contrato da PGM íntegro ({self.verificados} checks)"
        if self.avisos:
            resumo += f", {len(self.avisos)} aviso(s)"
        print(f"\033[32m{resumo}\033[0m")
        return 0


def chaves_visiveis(registro: Dict[str, Any]) -> List[str]:
    """Chaves de um registro, sem as que carregam PII."""
    return sorted(chave for chave in registro if chave not in CAMPOS_PII)


def verificar_campos(
    rel: Relatorio, registro: Dict[str, Any], campos: tuple, rotulo: str
) -> None:
    ausentes = [campo for campo in campos if campo not in registro]
    if ausentes:
        rel.falhou(
            f"{rotulo}: campo(s) ausente(s) {ausentes}",
            f"veio {chaves_visiveis(registro)}",
        )
    else:
        rel.ok(f"{rotulo}: todos os campos esperados presentes")


def verificar_consulta(rel: Relatorio, dados: Dict[str, Any]) -> Dict[str, Any]:
    rel.secao("Consulta de débitos — resposta crua da PGM")

    verificar_campos(rel, dados, CAMPOS_CONSULTA, "consulta")

    debitos = dados.get("debitosNaoParceladosComSaldoTotal") or {}
    verificar_campos(rel, debitos, CAMPOS_DEBITOS, "debitosNaoParcelados")

    cdas = debitos.get("cdasNaoAjuizadasNaoParceladas") or []
    efs = debitos.get("efsNaoParceladas") or []
    guias = (dados.get("guiasParceladasComSaldoTotal") or {}).get(
        "guiasParceladas"
    ) or []

    print(f"       massa: {len(cdas)} CDA(s), {len(efs)} EF(s), {len(guias)} guia(s)")

    for indice, cda in enumerate(cdas):
        verificar_campos(rel, cda, CAMPOS_CDA, f"CDA[{indice}]")
        verificar_soma_da_cda(rel, cda, indice)

    for indice, ef in enumerate(efs):
        verificar_campos(rel, ef, CAMPOS_EF, f"EF[{indice}]")
        # Documentado como ausente. Se aparecer, o casamento por eliminação
        # deixa de ser necessário e o código pode ler direto.
        if "naturezaDivida" in ef:
            rel.aviso(
                f"EF[{indice}] agora traz 'naturezaDivida' — "
                "revisar a dedução por eliminação em _itens_da_consulta"
            )

    for indice, guia in enumerate(guias):
        verificar_campos(rel, guia, CAMPOS_GUIA_PARCELADA, f"guia[{indice}]")

    if not cdas:
        rel.aviso("nenhuma CDA na massa: contrato da CDA não foi exercitado")
    if not efs:
        rel.aviso("nenhuma EF na massa: contrato da EF não foi exercitado")

    verificar_naturezas(rel, dados, cdas)
    verificar_soma_dos_itens(rel, debitos, cdas, efs, guias)

    return {"cdas": cdas, "efs": efs, "guias": guias}


def verificar_soma_da_cda(rel: Relatorio, cda: Dict[str, Any], indice: int) -> None:
    """`valorSaldoTotal` deve ser principal + honorários."""
    principal = valor_brl_para_numero(cda.get("valorSaldoPrincipal"))
    honorarios = valor_brl_para_numero(cda.get("valorSaldoHonorarios"))
    total = valor_brl_para_numero(cda.get("valorSaldoTotal"))

    if principal is None or honorarios is None or total is None:
        rel.aviso(f"CDA[{indice}]: sem os três valores, soma não verificada")
        return

    if round(principal + honorarios, 2) == total:
        rel.ok(f"CDA[{indice}]: principal + honorários = total", f"{total:.2f}")
    else:
        rel.falhou(
            f"CDA[{indice}]: principal + honorários != total",
            f"{principal:.2f} + {honorarios:.2f} != {total:.2f}",
        )


def verificar_naturezas(
    rel: Relatorio, dados: Dict[str, Any], cdas: List[Dict[str, Any]]
) -> None:
    """
    A natureza de cada CDA precisa estar na lista agregada da consulta.

    É essa relação que sustenta deduzir a natureza da EF por eliminação: se uma
    CDA trouxesse natureza fora da lista, a sobra deixaria de significar EF.
    """
    agregadas = dados.get("naturezasDivida") or []
    if not isinstance(agregadas, list):
        rel.falhou("naturezasDivida não é lista", f"veio {type(agregadas).__name__}")
        return

    if not cdas:
        return

    fora = [
        cda.get("naturezaDivida")
        for cda in cdas
        if cda.get("naturezaDivida") and cda.get("naturezaDivida") not in agregadas
    ]
    if fora:
        rel.falhou("natureza de CDA fora de naturezasDivida", f"{fora}")
    else:
        rel.ok("naturezas das CDAs estão em naturezasDivida", f"{agregadas}")

    cobertas = {cda.get("naturezaDivida") for cda in cdas if cda.get("naturezaDivida")}
    sobrando = [nat for nat in agregadas if nat not in cobertas]
    if sobrando:
        print(f"       naturezas sem CDA (candidatas a EF): {sobrando}")


def verificar_soma_dos_itens(
    rel: Relatorio,
    debitos: Dict[str, Any],
    cdas: List[Dict[str, Any]],
    efs: List[Dict[str, Any]],
    guias: List[Dict[str, Any]],
) -> None:
    """
    A soma dos itens não parcelados deve dar `saldoTotalNaoParcelado`.

    É a premissa do casamento guia<->natureza feito pelo consumidor: se a soma
    não fecha aqui, não vai fechar contra o valor das guias emitidas.
    """
    informado = valor_brl_para_numero(debitos.get("saldoTotalNaoParcelado"))
    if informado is None:
        rel.aviso("saldoTotalNaoParcelado ausente: soma não verificada")
        return

    itens = _itens_da_consulta(cdas, efs, guias)
    somaveis = [
        item["valor"]
        for item in itens
        if item["tipo"] in ("cda", "ef") and item["valor"] is not None
    ]
    soma = round(sum(somaveis), 2)

    if soma == informado:
        rel.ok("soma dos itens = saldoTotalNaoParcelado", f"{soma:.2f}")
    else:
        rel.falhou(
            "soma dos itens != saldoTotalNaoParcelado",
            f"{soma:.2f} != {informado:.2f}",
        )


def verificar_itens_processados(rel: Relatorio, resultado: Dict[str, Any]) -> None:
    rel.secao("Consulta de débitos — saída processada pela tool")

    if not resultado.get("api_resposta_sucesso"):
        rel.falhou("consultar_debitos não teve sucesso", str(resultado)[:200])
        return

    itens = resultado.get("itens")
    if itens is None:
        rel.falhou("resposta sem 'itens'", f"veio {sorted(resultado)}")
        return

    rel.ok(f"'itens' presente com {len(itens)} entrada(s)")

    tipos_validos = {"cda", "ef", "guia"}
    for indice, item in enumerate(itens):
        if item.get("tipo") not in tipos_validos:
            rel.falhou(f"item[{indice}] com tipo inesperado", str(item.get("tipo")))
        if item.get("valor") is not None and not isinstance(
            item["valor"], (int, float)
        ):
            rel.falhou(
                f"item[{indice}].valor não é número",
                type(item["valor"]).__name__,
            )

    if all(item.get("tipo") in tipos_validos for item in itens):
        rel.ok("todos os itens têm tipo válido e valor numérico ou nulo")

    if "naturezas_divida" in resultado:
        rel.ok("'naturezas_divida' presente", str(resultado["naturezas_divida"]))
    else:
        rel.falhou("resposta sem 'naturezas_divida'")


async def verificar_emissao(rel: Relatorio, cda_id: str) -> None:
    rel.secao(f"Emissão de guia — CDA {cda_id}")

    registros = await pgm_api(
        endpoint=ENDPOINT_EMISSAO,
        consumidor=CONSUMIDOR_EMISSAO,
        data={"origem_solicitação": 0, "cdas": [cda_id], "efs": []},
    )

    if isinstance(registros, dict) and "erro" in registros:
        rel.falhou("emissão devolveu erro", str(registros.get("motivos"))[:200])
        return

    lista = registros if isinstance(registros, list) else [registros]
    lista = [item for item in lista if isinstance(item, dict) and "pdf" in item]

    if not lista:
        rel.falhou("emissão sem registro de guia")
        return

    for indice, registro in enumerate(lista):
        verificar_campos(rel, registro, CAMPOS_EMISSAO, f"guia[{indice}]")
        verificar_campos_derivados(rel, registro, indice)

    resultado = await processar_registros(
        endpoint=ENDPOINT_EMISSAO,
        consumidor=CONSUMIDOR_EMISSAO,
        parametros_entrada={"origem_solicitação": 0, "cdas": [cda_id], "efs": []},
    )
    guias = resultado.get("guias_emitidas") or []
    if guias and all("valor" in guia for guia in guias):
        rel.ok("processar_registros devolve 'valor' em toda guia")
    else:
        rel.falhou("alguma guia saiu sem a chave 'valor'")


def verificar_campos_derivados(
    rel: Relatorio, registro: Dict[str, Any], indice: int
) -> None:
    """
    O que sustenta a extração do valor.

    PIX e código de barras descrevem a mesma guia por caminhos independentes.
    Divergirem significa que um dos dois parsers está lendo errado — ou que a
    PGM mudou o layout de um deles.
    """
    if any(campo in registro for campo in ("valorTotal", "naturezaDivida")):
        rel.aviso(
            f"guia[{indice}]: a emissão agora traz valor ou natureza — "
            "extrair direto passa a ser melhor que derivar do código de pagamento"
        )

    do_pix = valor_do_pix(registro.get("codigoQrEMVPix"))
    das_barras = valor_do_codigo_de_barras(registro.get("codigoDeBarras"))

    if do_pix is None and das_barras is None:
        rel.falhou(f"guia[{indice}]: nenhuma fonte deu valor")
        return

    if do_pix is None:
        rel.aviso(f"guia[{indice}]: PIX sem campo 54, valor veio do código de barras")
    elif das_barras is None:
        rel.aviso(f"guia[{indice}]: código de barras não deu valor, PIX resolveu")
    elif do_pix == das_barras:
        rel.ok(f"guia[{indice}]: PIX e código de barras concordam", f"{do_pix:.2f}")
    else:
        rel.falhou(
            f"guia[{indice}]: PIX e código de barras divergem",
            f"pix={do_pix:.2f} barras={das_barras:.2f}",
        )


def primeira_cda_emissivel(cdas: List[Dict[str, Any]]) -> Optional[str]:
    """CDA que a PGM aceita transformar em guia."""
    for cda in cdas:
        if cda.get("isCdaBlqueadaEmissaoGuia"):
            continue
        if cda.get("cdaId"):
            return cda["cdaId"]
    return None


def checar_credenciais(rel: Relatorio) -> bool:
    faltando = [
        nome
        for nome in (
            "CHATBOT_INTEGRATIONS_URL",
            "CHATBOT_INTEGRATIONS_KEY",
            "CHATBOT_PGM_API_URL",
            "CHATBOT_PGM_ACCESS_KEY",
        )
        if not (os.environ.get(nome) or "").strip()
    ]
    if faltando:
        rel.falhou(
            f"credenciais ausentes: {faltando}",
            "defina no ambiente ou em src/config/.env",
        )
        return False
    return True


async def executar(args: argparse.Namespace) -> int:
    rel = Relatorio()

    rel.secao("Ambiente")
    if not checar_credenciais(rel):
        return rel.encerrar()
    rel.ok("credenciais presentes")

    dados = await pgm_api(
        endpoint=ENDPOINT_CONSULTA,
        consumidor=CONSUMIDOR_CONSULTA,
        data={"origem_solicitação": 0, "cpfCnpj": args.cpf},
    )

    if isinstance(dados, dict) and dados.get("erro"):
        # Contribuinte sem débito é resposta legítima da PGM, não contrato
        # quebrado — mas o run não verificou nada, e dizer "íntegro" seria
        # pior que dizer "não deu para verificar".
        rel.aviso(f"a PGM recusou a consulta: {dados.get('motivos')}")
        rel.aviso("escolha um CPF/CNPJ da massa que tenha débitos em aberto")
        return rel.encerrar()

    if not isinstance(dados, dict):
        rel.falhou(
            "consulta não devolveu objeto de dados",
            f"veio {type(dados).__name__}",
        )
        return rel.encerrar()

    massa = verificar_consulta(rel, dados)

    resultado = await consultar_debitos(
        {"consulta_debitos": "cpfCnpj", "cpfCnpj": args.cpf}
    )
    verificar_itens_processados(rel, resultado)

    if args.emitir:
        cda_id = args.cda or primeira_cda_emissivel(massa["cdas"])
        if cda_id:
            await verificar_emissao(rel, cda_id)
        else:
            rel.aviso("nenhuma CDA elegível para emissão: contrato não exercitado")

    return rel.encerrar()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verifica o contrato da API da PGM contra o que o código espera."
    )
    parser.add_argument(
        "--cpf",
        required=True,
        help="CPF/CNPJ da massa de teste (só dígitos). Não é impresso no relatório.",
    )
    parser.add_argument(
        "--emitir",
        action="store_true",
        help=(
            "Também emite guia e verifica o contrato da emissão. "
            "ATENÇÃO: gera uma guia de verdade na PGM."
        ),
    )
    parser.add_argument(
        "--cda",
        help="CDA específica para a emissão. Padrão: a primeira elegível da consulta.",
    )
    args = parser.parse_args()

    return asyncio.run(executar(args))


if __name__ == "__main__":
    sys.exit(main())
