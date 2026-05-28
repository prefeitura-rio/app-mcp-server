"""
Unit tests para consulta de status de mensagens WhatsApp.

Testa a função check_message_read_status que consulta no Redis se uma
mensagem foi lida (duplo check azul), entregue ou enviada.
"""

from unittest.mock import patch

import pytest

from src.tools.whatsapp_message_status import check_message_read_status


@pytest.fixture
def mock_redis():
    """Mock do Redis client."""
    with patch("src.tools.whatsapp_message_status.get_redis_client") as mock:
        yield mock.return_value


def test_check_message_read_status_empty_id():
    """message_id vazio retorna erro."""
    result = check_message_read_status("")

    assert result["found"] is False
    assert result["message_id"] == ""
    assert "obrigatório" in result["error"]


def test_check_message_read_status_not_found(mock_redis):
    """message_id não encontrado no Redis retorna found=False."""
    mock_redis.hgetall.return_value = {}

    result = check_message_read_status("wamid.123")

    assert result["found"] is False
    assert result["message_id"] == "wamid.123"
    assert "não encontrado" in result["error"].lower()
    mock_redis.hgetall.assert_called_once_with("msg_status:wamid.123")


def test_check_message_read_status_sent(mock_redis):
    """Status 'sent' retorna is_sent=True, demais False."""
    mock_redis.hgetall.return_value = {
        "status": "sent",
        "timestamp": "1234567890",
        "recipient_id": "5521999999999",
        "updated_at": "1714567890",
    }

    result = check_message_read_status("wamid.123")

    assert result["found"] is True
    assert result["message_id"] == "wamid.123"
    assert result["status"] == "sent"
    assert result["is_sent"] is True
    assert result["is_delivered"] is False
    assert result["is_read"] is False
    assert result["timestamp"] == "1234567890"
    assert result["recipient_id"] == "5521999999999"
    assert result["updated_at"] == "1714567890"


def test_check_message_read_status_delivered(mock_redis):
    """Status 'delivered' retorna is_sent=True, is_delivered=True."""
    mock_redis.hgetall.return_value = {
        "status": "delivered",
        "timestamp": "1234567891",
        "recipient_id": "5521888888888",
        "updated_at": "1714567891",
    }

    result = check_message_read_status("wamid.456")

    assert result["found"] is True
    assert result["status"] == "delivered"
    assert result["is_sent"] is True
    assert result["is_delivered"] is True
    assert result["is_read"] is False


def test_check_message_read_status_read(mock_redis):
    """Status 'read' retorna duplo check azul: is_read=True."""
    mock_redis.hgetall.return_value = {
        "status": "read",
        "timestamp": "1234567892",
        "recipient_id": "5521777777777",
        "updated_at": "1714567892",
    }

    result = check_message_read_status("wamid.789")

    assert result["found"] is True
    assert result["message_id"] == "wamid.789"
    assert result["status"] == "read"
    assert result["is_sent"] is True
    assert result["is_delivered"] is True
    assert result["is_read"] is True  # Duplo check azul!


def test_check_message_read_status_failed(mock_redis):
    """Status 'failed' retorna todos flags False."""
    mock_redis.hgetall.return_value = {
        "status": "failed",
        "timestamp": "1234567893",
        "recipient_id": "5521666666666",
        "updated_at": "1714567893",
    }

    result = check_message_read_status("wamid.999")

    assert result["found"] is True
    assert result["status"] == "failed"
    assert result["is_sent"] is False
    assert result["is_delivered"] is False
    assert result["is_read"] is False


def test_check_message_read_status_redis_error(mock_redis):
    """Erro no Redis retorna found=False com mensagem de erro."""
    mock_redis.hgetall.side_effect = Exception("Connection error")

    result = check_message_read_status("wamid.error")

    assert result["found"] is False
    assert result["message_id"] == "wamid.error"
    assert "Erro ao consultar Redis" in result["error"]


def test_check_message_read_status_missing_fields(mock_redis):
    """Redis retorna dados incompletos, função usa defaults vazios."""
    mock_redis.hgetall.return_value = {
        "status": "read",
        # timestamp, recipient_id e updated_at ausentes
    }

    result = check_message_read_status("wamid.incomplete")

    assert result["found"] is True
    assert result["status"] == "read"
    assert result["is_read"] is True
    assert result["timestamp"] == ""
    assert result["recipient_id"] == ""
    assert result["updated_at"] == ""
