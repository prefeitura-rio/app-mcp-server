"""
Runner de contrato da PGM (CHATR-164).

O runner fala com a PGM de verdade, então o que dá para cobrir aqui é o que ele
faz com a resposta — e as duas propriedades que não podem regredir sem alguém
perceber: quantas guias reais ele emite, e o que ele imprime.

O arquivo mora em `src/tests/e2e/` com prefixo `run_` justamente para não ser
coletado pelo pytest; importá-lo por caminho é o que permite testá-lo sem
mudar isso.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location(
        "run_pgm_contract",
        PROJECT_ROOT / "src/tests/e2e/run_pgm_contract.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_pgm_contract"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("run_pgm_contract", None)


# Registro de emissão real, reduzido aos campos que o runner lê.
REGISTRO_EMISSAO = {
    "dataVencimento": "31/08/2026",
    "pdf": "http://pgm/repositoriorelatorioscertidao/"
    "6a13bc0c-f48b-4459-858f-a836d673e210.pdf",
    "codigoDeBarras": "81650000048-3 25433659202-0 60831418100-9 11098057426-0",
    "codigoQrEMVPix": (
        "00020101021226850014br.gov.bcb.pix2563pix.santander.com.br/qr/v2/"
        "a2ecf2b0-c305-4a4c-8cb4-40561cff7e0b520400005303986540748"
        "25.435802BR5917PM RIO DE JANEIRO6014RIO DE JANEIRO62070503***63044CD6"
    ),
}


def test_emitir_chama_a_pgm_uma_unica_vez(runner, monkeypatch):
    """
    Cada chamada ao endpoint de emissão gera uma guia de pagamento de verdade
    contra os débitos de um contribuinte real. Verificar o processamento com
    uma segunda chamada cobraria uma segunda guia por execução.
    """
    chamadas = []

    async def fake_pgm_api(endpoint="", consumidor="", data=None):
        chamadas.append(endpoint)
        return [REGISTRO_EMISSAO]

    monkeypatch.setattr(runner, "pgm_api", fake_pgm_api)

    rel = runner.Relatorio()
    asyncio.run(runner.verificar_emissao(rel, "94/009914/2026-00"))

    assert chamadas == [runner.ENDPOINT_EMISSAO]
    assert rel.falhas == []


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("CPF 12345678901 nao encontrado", "CPF ########### nao encontrado"),
        ("CDA 01/184218/2026-00 invalida", "CDA 01/######/2026-00 invalida"),
        ("EF 0334852-76.2017.8.19.0001", "EF #######-76.2017.8.19.0001"),
        # Valor monetário atravessa intacto: nenhum grupo chega a 6 dígitos.
        ("saldo R$26.819,86 em aberto", "saldo R$26.819,86 em aberto"),
        ("guia 2026/0009656", "guia 2026/#######"),
    ],
)
def test_sem_identificadores_mascara_documento_e_preserva_valor(
    runner, texto, esperado
):
    assert runner.sem_identificadores(texto) == esperado


def test_sem_identificadores_aceita_none(runner):
    """`motivos` pode vir ausente; o diagnóstico não pode quebrar por isso."""
    assert runner.sem_identificadores(None) == "None"


def test_erro_da_consulta_nao_imprime_o_documento(runner, capsys):
    """
    `motivos` é texto livre escrito pela PGM e costuma ecoar o documento
    consultado — o relatório promete não imprimi-lo.
    """
    rel = runner.Relatorio()
    runner.verificar_itens_processados(
        rel,
        {
            "api_resposta_sucesso": False,
            "api_descricao_erro": "Contribuinte 12345678901 sem debitos",
        },
    )

    saida = capsys.readouterr().out
    assert "12345678901" not in saida
    assert "###########" in saida
