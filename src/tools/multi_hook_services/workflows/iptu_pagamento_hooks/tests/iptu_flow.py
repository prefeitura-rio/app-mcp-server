"""
Testes para IPTU Flow usando framework hooks-based.

Testa:
1. Happy path - fluxo completo
2. Navegação não-linear - voltar a steps anteriores
3. Validação de inputs
4. Tratamento de erros de API
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.tools.multi_step_service.core.models import ServiceState, AgentResponse
from src.tools.multi_hook_services.core.flow_executor import FlowExecutor
from src.tools.multi_hook_services.workflows.iptu_pagamento_hooks.iptu_flow import IPTUFlow
from src.tools.multi_hook_services.core.flow_exceptions import FlowPause

# Reutiliza modelos do workflow existente
from src.tools.multi_step_service.workflows.iptu_pagamento.core.models import (
    DadosGuias,
    Guia,
    DadosCotas,
    Cota,
    DadosDarm,
    Darm,
)


def create_mock_guia(numero_guia: str = "00", valor: str = "860,00") -> Guia:
    """Cria um objeto Guia mock para testes."""
    guia = Guia(
        Situacao={"codigo": "01", "descricao": "EM ABERTO"},
        Inscricao="12345678",
        Exercicio="2025",
        NGuia=numero_guia,
        Tipo="ORDINÁRIA",
        ValorIPTUOriginalGuia=valor,
        DataVenctoDescCotaUnica="07/02/2025",
        QuantDiasEmAtraso="0",
        PercentualDescCotaUnica="00007",
        ValorIPTUDescontoAvista="60,00",
        ValorParcelas="86,00",
        CreditoNotaCarioca="0,00",
        CreditoDECAD="0,00",
        CreditoIsencao="0,00",
        CreditoCotaUnica="0,00",
        ValorQuitado="0,00",
        DataQuitacao="",
        Deposito="N",
    )
    # Set calculated fields
    guia.valor_numerico = 860.0
    guia.valor_desconto_numerico = 60.0
    guia.valor_parcelas_numerico = 86.0
    guia.esta_quitada = False
    guia.esta_em_aberto = True
    return guia


def create_mock_cota(numero_cota: str = "01", paga: bool = False) -> Cota:
    """Cria um objeto Cota mock para testes."""
    codigo_situacao = "01" if paga else "02"  # 01=PAGA, 02=EM ABERTO
    cota = Cota(
        Situacao={"codigo": codigo_situacao, "descricao": "PAGA" if paga else "EM ABERTO"},
        NCota=numero_cota,
        ValorCota="86,00",
        DataVencimento="07/03/2025",
        ValorPago="86,00" if paga else "0,00",
        DataPagamento="01/03/2025" if paga else "",
        QuantDiasEmAtraso="0",
    )
    # Set calculated fields
    cota.valor_numerico = 86.0
    cota.valor_pago_numerico = 86.0 if paga else 0.0
    cota.dias_atraso_numerico = 0
    cota.esta_paga = paga
    cota.esta_vencida = False
    return cota


def create_mock_darm(numero_guia: str = "00", cotas: list = None) -> Darm:
    """Cria um objeto DARM mock para testes."""
    if cotas is None:
        cotas = ["01"]

    darm = Darm(
        Cotas=[{"ncota": c, "valor": "86,00"} for c in cotas],
        Inscricao="12345678",
        Exercicio="2025",
        NGuia=numero_guia,
        Tipo="ORDINÁRIA",
        DataVencimento="07/03/2025",
        ValorIPTUOriginal="860,00",
        ValorDARM="86,00",
        ValorDescCotaUnica="0,00",
        CreditoNotaCarioca="0,00",
        CreditoDECAD="0,00",
        CreditoIsencao="0,00",
        CreditoEmissao="0,00",
        ValorAPagar="86,00",
        SequenciaNumerica="12345678901234567890123456789012345678901234567",
        DescricaoDARM=f"DARM ref. cotas {','.join(cotas)}",
        CodReceita="310-7",
        DesReceita="RECEITA DE PAGAMENTO",
        Endereco="Rua Teste, 123",
        Nome="Proprietario Teste",
    )
    # Set calculated fields
    darm.valor_numerico = 86.0
    darm.codigo_barras = "12345678901234567890123456789012345678901234567"
    return darm


class TestIPTUFlowHappyPath:
    """Testes do fluxo completo (happy path)."""

    @pytest.mark.asyncio
    async def test_fluxo_completo_sucesso(self):
        """
        Testa o fluxo completo do IPTU:
        1. Inscrição → 2. Ano → 3. Guia → 4. Cotas → 5. Formato → 6. Confirmação → 7. Geração
        """
        # Setup
        state = ServiceState(user_id="test_user", service_name="iptu_pagamento")
        flow = IPTUFlow(state)
        executor = FlowExecutor()

        # Mock API
        flow.api.get_imovel_info = AsyncMock(return_value={
            "endereco": "Rua Teste, 123",
            "proprietario": "João Silva"
        })
        flow.api.consultar_guias = AsyncMock(return_value=DadosGuias(
            inscricao_imobiliaria="12345678",
            exercicio="2025",
            guias=[create_mock_guia("00"), create_mock_guia("01")],
            total_guias=2
        ))
        flow.api.obter_cotas = AsyncMock(return_value=DadosCotas(
            inscricao_imobiliaria="12345678",
            exercicio="2025",
            numero_guia="00",
            tipo_guia="ORDINÁRIA",
            cotas=[create_mock_cota("01"), create_mock_cota("02"), create_mock_cota("03")],
            total_cotas=3,
            valor_total=258.0
        ))
        flow.api.consultar_darm = AsyncMock(return_value=DadosDarm(
            inscricao_imobiliaria="12345678",
            exercicio="2025",
            numero_guia="00",
            cotas_selecionadas=["01", "02"],
            darm=create_mock_darm("00", ["01", "02"])
        ))
        flow.api.download_pdf_darm = AsyncMock(return_value="https://example.com/darm.pdf")

        # Execução passo-a-passo
        # Passo 1: Solicita inscrição
        result = await executor.execute(flow, state, {})
        assert result.status == "progress"
        assert "inscricao_imobiliaria" in str(result.agent_response.payload_schema)

        # Passo 2: Fornece inscrição, solicita ano
        result = await executor.execute(flow, state, {"inscricao_imobiliaria": "12345678"})
        assert result.status == "progress"
        assert "ano_exercicio" in str(result.agent_response.payload_schema)
        assert state.data["inscricao_imobiliaria"] == "12345678"

        # Passo 3: Fornece ano, solicita guia
        result = await executor.execute(flow, state, {"ano_exercicio": 2025})
        assert result.status == "progress"
        assert "guia_escolhida" in str(result.agent_response.payload_schema)
        assert state.data["ano_exercicio"] == 2025

        # Passo 4: Escolhe guia, solicita cotas
        result = await executor.execute(flow, state, {"guia_escolhida": "00"})
        assert result.status == "progress"
        assert "cotas_escolhidas" in str(result.agent_response.payload_schema)

        # Passo 5: Escolhe cotas (múltiplas), solicita formato DARM
        result = await executor.execute(flow, state, {"cotas_escolhidas": ["01", "02"]})
        assert result.status == "progress"
        assert "darm_separado" in str(result.agent_response.payload_schema)

        # Passo 6: Escolhe formato, solicita confirmação
        result = await executor.execute(flow, state, {"darm_separado": False})
        assert result.status == "progress"
        assert "confirmacao" in str(result.agent_response.payload_schema)

        # Passo 7: Confirma, gera DARMs
        result = await executor.execute(flow, state, {"confirmacao": True})
        assert result.status == "completed"
        assert result.agent_response.error_message is None
        assert "guias_geradas" in result.agent_response.data


class TestIPTUFlowNavegacaoNaoLinear:
    """Testes de navegação não-linear (voltar a steps anteriores)."""

    @pytest.mark.asyncio
    async def test_voltar_para_ano_exercicio(self):
        """
        Testa navegação não-linear: usuário volta para escolher outro ano
        depois de já ter escolhido guia e cotas.

        Fluxo:
        1. Inscrição → Ano → Guia → Cotas
        2. Usuário volta e muda o ano
        3. Sistema deve resetar guia e cotas automaticamente
        """
        # Setup
        state = ServiceState(user_id="test_user", service_name="iptu_pagamento")
        flow = IPTUFlow(state)
        executor = FlowExecutor()

        # Mock API
        flow.api.get_imovel_info = AsyncMock(return_value={
            "endereco": "Rua Teste, 123",
            "proprietario": "João Silva"
        })
        flow.api.consultar_guias = AsyncMock(return_value=DadosGuias(
            inscricao_imobiliaria="12345678",
            exercicio="2025",
            guias=[create_mock_guia()],
            total_guias=1
        ))
        flow.api.obter_cotas = AsyncMock(return_value=DadosCotas(
            inscricao_imobiliaria="12345678",
            exercicio="2025",
            numero_guia="00",
            tipo_guia="ORDINÁRIA",
            cotas=[create_mock_cota("01")],
            total_cotas=1,
            valor_total=86.0
        ))

        # Fluxo inicial até escolher cotas
        await executor.execute(flow, state, {"inscricao_imobiliaria": "12345678"})
        await executor.execute(flow, state, {"ano_exercicio": 2024})
        await executor.execute(flow, state, {"guia_escolhida": "00"})
        await executor.execute(flow, state, {"cotas_escolhidas": ["01"]})

        # Verifica que dados foram coletados
        assert state.data["ano_exercicio"] == 2024
        assert state.data["guia_escolhida"] == "00"
        assert state.data["cotas_escolhidas"] == ["01"]

        # Navegação não-linear: volta para ano (muda para 2025)
        result = await executor.execute(flow, state, {"ano_exercicio": 2025})

        # Verifica que:
        # 1. Ano foi atualizado
        assert state.data["ano_exercicio"] == 2025

        # 2. Dados posteriores foram resetados (guia e cotas)
        assert "guia_escolhida" not in state.data
        assert "cotas_escolhidas" not in state.data

        # 3. Status é "progress" (aguardando nova escolha de guia)
        assert result.status == "progress"


class TestIPTUFlowValidacao:
    """Testes de validação de inputs."""

    @pytest.mark.asyncio
    async def test_validacao_inscricao_invalida(self):
        """Testa que inscrições inválidas são rejeitadas pelo Pydantic."""
        state = ServiceState(user_id="test_user", service_name="iptu_pagamento")
        flow = IPTUFlow(state)
        executor = FlowExecutor()

        # Inscrição com mais de 15 dígitos deve falhar
        result = await executor.execute(flow, state, {"inscricao_imobiliaria": "1234567890123456"})

        assert result.status == "progress"  # Ainda em progresso (pedindo novamente)
        assert result.agent_response.error_message is not None  # Tem erro de validação


if __name__ == "__main__":
    """Permite executar testes diretamente."""
    import asyncio

    async def run_tests():
        print("🧪 Executando testes do IPTU Flow (hooks-based)\n")

        # Teste 1: Happy Path
        print("=" * 60)
        print("Teste 1: Fluxo Completo (Happy Path)")
        print("=" * 60)
        test1 = TestIPTUFlowHappyPath()
        await test1.test_fluxo_completo_sucesso()
        print("✅ Teste 1 passou!\n")

        # Teste 2: Navegação Não-Linear
        print("=" * 60)
        print("Teste 2: Navegação Não-Linear")
        print("=" * 60)
        test2 = TestIPTUFlowNavegacaoNaoLinear()
        await test2.test_voltar_para_ano_exercicio()
        print("✅ Teste 2 passou!\n")

        # Teste 3: Validação
        print("=" * 60)
        print("Teste 3: Validação de Inputs")
        print("=" * 60)
        test3 = TestIPTUFlowValidacao()
        await test3.test_validacao_inscricao_invalida()
        print("✅ Teste 3 passou!\n")

        print("=" * 60)
        print("✅ Todos os testes passaram!")
        print("=" * 60)

    asyncio.run(run_tests())
