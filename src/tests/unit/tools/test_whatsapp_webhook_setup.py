"""
Unit tests para configuração programática de webhooks WhatsApp.

Testa as funções de subscrição e consulta de webhooks via Graph API,
permitindo configurar tudo por código sem acessar o Business Manager.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.whatsapp_webhook_setup import (
    get_waba_id_from_phone,
    get_webhook_subscriptions,
    subscribe_webhook_fields,
)


@pytest.fixture
def mock_env():
    """Mock das variáveis de ambiente."""
    with patch("src.tools.whatsapp_webhook_setup.env") as mock:
        mock.WA_TOKEN = "test_token_123"
        mock.WA_PHONE_NUMBER_ID = "123456789"
        yield mock


@pytest.mark.asyncio
async def test_get_waba_id_from_phone_success(mock_env):
    """Busca WABA ID a partir do phone number ID com sucesso."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "whatsapp_business_account_id": "999888777",
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        waba_id = await get_waba_id_from_phone()

        assert waba_id == "999888777"


@pytest.mark.asyncio
async def test_get_waba_id_from_phone_no_token():
    """Levanta ValueError se WA_TOKEN não configurado."""
    with patch("src.tools.whatsapp_webhook_setup.env") as mock_env:
        mock_env.WA_TOKEN = None
        mock_env.WA_PHONE_NUMBER_ID = "123"

        with pytest.raises(ValueError, match="WA_TOKEN não configurado"):
            await get_waba_id_from_phone()


@pytest.mark.asyncio
async def test_get_waba_id_from_phone_no_phone_number_id():
    """Levanta ValueError se WA_PHONE_NUMBER_ID não configurado."""
    with patch("src.tools.whatsapp_webhook_setup.env") as mock_env:
        mock_env.WA_TOKEN = "test_token"
        mock_env.WA_PHONE_NUMBER_ID = None

        with pytest.raises(ValueError, match="WA_PHONE_NUMBER_ID não configurado"):
            await get_waba_id_from_phone()


@pytest.mark.asyncio
async def test_get_waba_id_from_phone_api_error(mock_env):
    """API retorna erro, levanta Exception."""
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "Invalid phone number"

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        with pytest.raises(Exception, match="Erro ao buscar WABA ID"):
            await get_waba_id_from_phone()


@pytest.mark.asyncio
async def test_get_waba_id_from_phone_missing_field(mock_env):
    """Response sem whatsapp_business_account_id levanta Exception."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "123"}  # campo errado

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        with pytest.raises(Exception, match="WABA ID não encontrado"):
            await get_waba_id_from_phone()


@pytest.mark.asyncio
async def test_get_webhook_subscriptions_success(mock_env):
    """Lista subscrições de webhook com sucesso."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [{"subscribed_fields": ["messages", "message_statuses"]}]
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        result = await get_webhook_subscriptions("999888777")

        assert "data" in result
        assert result["data"][0]["subscribed_fields"] == [
            "messages",
            "message_statuses",
        ]


@pytest.mark.asyncio
async def test_get_webhook_subscriptions_error(mock_env):
    """API retorna erro ao listar subscrições."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Access denied"

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_response
        )

        result = await get_webhook_subscriptions("999888777")

        assert result["error"] == "Access denied"
        assert result["status_code"] == 403


@pytest.mark.asyncio
async def test_subscribe_webhook_fields_success(mock_env):
    """Subscreve campos de webhook com sucesso."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True}

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_response
        )

        result = await subscribe_webhook_fields(
            "999888777", fields=["messages", "message_statuses"]
        )

        assert result["success"] is True
        assert result["subscribed_fields"] == ["messages", "message_statuses"]


@pytest.mark.asyncio
async def test_subscribe_webhook_fields_default_fields(mock_env):
    """Subscrição sem campos usa default [messages, message_statuses]."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": True}

    with patch("httpx.AsyncClient") as mock_client:
        mock_post = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__.return_value.post = mock_post

        result = await subscribe_webhook_fields("999888777")

        assert result["success"] is True
        assert result["subscribed_fields"] == ["messages", "message_statuses"]

        # Verifica que o POST foi feito com os campos corretos
        call_args = mock_post.call_args
        assert call_args.kwargs["json"]["subscribed_fields"] == [
            "messages",
            "message_statuses",
        ]


@pytest.mark.asyncio
async def test_subscribe_webhook_fields_error(mock_env):
    """API retorna erro ao subscrever."""
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "Invalid fields"

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_response
        )

        result = await subscribe_webhook_fields("999888777")

        assert result["success"] is False
        assert result["error"] == "Invalid fields"
        assert result["status_code"] == 400


@pytest.mark.asyncio
async def test_subscribe_webhook_fields_no_token():
    """Levanta ValueError se WA_TOKEN não configurado."""
    with patch("src.tools.whatsapp_webhook_setup.env") as mock_env:
        mock_env.WA_TOKEN = None

        with pytest.raises(ValueError, match="WA_TOKEN não configurado"):
            await subscribe_webhook_fields("999888777")
