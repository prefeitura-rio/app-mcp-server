"""
Testes pra src/tools/inbound_media.py — recepcao de midia inbound do WhatsApp
(stub que registra + sugere reply, sem processar conteudo ainda).
"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _ensure_package(name: str, path: Path) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg
    return pkg


@pytest.fixture
def inbound_media_module(monkeypatch):
    # Stub src.utils.log com logger fake — evita pull de loguru e dependencias.
    log_messages: list = []
    fake_logger = types.SimpleNamespace(
        info=lambda msg: log_messages.append(("info", msg)),
        warning=lambda msg: log_messages.append(("warning", msg)),
        error=lambda msg: log_messages.append(("error", msg)),
        debug=lambda msg: log_messages.append(("debug", msg)),
    )
    fake_log_module = types.ModuleType("src.utils.log")
    fake_log_module.logger = fake_logger
    monkeypatch.setitem(sys.modules, "src.utils.log", fake_log_module)

    # Garante que os pacotes parent existem como modulos (necessario pra import
    # `src.tools.inbound_media` quando carregamos via importlib).
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    spec = importlib.util.spec_from_file_location(
        "src.tools.inbound_media",
        PROJECT_ROOT / "src" / "tools" / "inbound_media.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["src.tools.inbound_media"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._test_log_messages = log_messages  # type: ignore[attr-defined]
    return module


def test_register_image_returns_received_with_pt_br_suggestion(inbound_media_module):
    register = inbound_media_module.register_inbound_media
    result = asyncio.run(
        register(
            media_type="image",
            user_number="5521989091014",
            salesforce_download_path="/services/data/v62.0/sobjects/ContentVersion/068xxxYYY/VersionData",
            content_version_id="068xxxYYY",
            file_extension="jpg",
            file_size_bytes=192939,
            message_id="entry-uuid-1",
            messaging_session_id="0Mw880XYZ",
        )
    )
    assert result["status"] == "received"
    assert result["media_type"] == "image"
    assert result["processing"] == "deferred"
    assert "imagem" in result["suggested_reply_pt_br"]
    # log emitido com payload audit
    logs = inbound_media_module._test_log_messages
    assert any("register_inbound_media (receive-stub)" in m for _, m in logs)
    assert any("068xxxYYY" in m for _, m in logs)


def test_register_audio_uses_audio_reply(inbound_media_module):
    register = inbound_media_module.register_inbound_media
    result = asyncio.run(
        register(
            media_type="audio",
            user_number="5521989091014",
            file_extension="oga",
            file_size_bytes=14509,
        )
    )
    assert result["status"] == "received"
    assert result["media_type"] == "audio"
    assert "áudio" in result["suggested_reply_pt_br"]


def test_register_location_uses_location_reply(inbound_media_module):
    register = inbound_media_module.register_inbound_media
    result = asyncio.run(
        register(
            media_type="location",
            user_number="5521989091014",
            latitude=-22.9,
            longitude=-43.2,
        )
    )
    assert result["status"] == "received"
    assert "localização" in result["suggested_reply_pt_br"]


def test_register_location_accepts_name_param(inbound_media_module):
    """Regressao (2026-06-19): o LLM repassa o `name` do pin de location (rótulo
    do local) junto com lat/lng. Antes o param nao existia → ToolException
    strict-pydantic 'unexpected keyword argument name' → engine caía no fallback
    'problema técnico ao processar'. A tool deve ACEITAR name e auditá-lo."""
    register = inbound_media_module.register_inbound_media
    result = asyncio.run(
        register(
            media_type="location",
            user_number="5521965850470",
            latitude=-22.950967,
            longitude=-43.18,
            address="",
            name="Praça Saens Peña",
        )
    )
    assert result["status"] == "received"
    assert result["media_type"] == "location"
    logs = inbound_media_module._test_log_messages
    assert any("Praça Saens Peña" in m for _, m in logs)


def test_register_unsupported_uses_unsupported_reply(inbound_media_module):
    register = inbound_media_module.register_inbound_media
    result = asyncio.run(
        register(media_type="unsupported", user_number="5521989091014")
    )
    assert result["status"] == "received"
    # Pos-ADR-013 (2026-05-12): reply convida endereco em texto (geocoding via
    # validate_address no turno seguinte) em vez do texto fechado anterior.
    # Frase "não consigo processar" eh exclusiva do template unsupported —
    # discrimina contra regressao que rotearia 'unsupported' pro template
    # 'location' (que tambem contem endereco+localizacao).
    reply = result["suggested_reply_pt_br"]
    assert result["media_type"] == "unsupported"
    assert "não consigo processar" in reply
    assert "endereço" in reply


def test_register_unknown_is_accepted(inbound_media_module):
    """Apex emite 'unknown' quando FileExtension nao casa whitelist OU correlacao
    falha (quarantena). MCP precisa aceitar pra registrar audit + sugerir reply."""
    register = inbound_media_module.register_inbound_media
    result = asyncio.run(
        register(
            media_type="unknown",
            user_number="5521989091014",
            content_version_id="068xxxYYY",
            file_extension="mp4",  # vídeo, nao suportado pelo BSP nem por nos
        )
    )
    assert result["status"] == "received"
    assert result["media_type"] == "unknown"
    assert "suggested_reply_pt_br" in result
    assert result["suggested_reply_pt_br"]


def test_register_document_uses_document_reply(inbound_media_module):
    """Cobertura pro path feliz de 'document' (PR #125) — faltava desde a
    introdução do tipo, o que deixou passar a regressão coberta pelo teste
    test_invalid_media_type_rejected logo abaixo."""
    register = inbound_media_module.register_inbound_media
    result = asyncio.run(
        register(
            media_type="document",
            user_number="5521989091014",
            content_version_id="068xxxYYY",
            file_extension="pdf",
        )
    )
    assert result["status"] == "received"
    assert result["media_type"] == "document"
    assert "documento" in result["suggested_reply_pt_br"]


def test_invalid_media_type_rejected(inbound_media_module):
    register = inbound_media_module.register_inbound_media
    # 'sticker' é um tipo real do WhatsApp (ver media-types.yaml), mas com
    # direction=outbound apenas — nunca foi, e não é, aceito como mídia
    # inbound por register_inbound_media. Usar um tipo já aceito (como
    # 'document' foi no passado, antes da PR #125) quebra este teste assim
    # que o tipo passa a ser suportado; 'sticker' não tem esse risco porque
    # inbound de sticker não é um caminho suportado pelo BSP/Meta.
    result = asyncio.run(register(media_type="sticker", user_number="5521989091014"))
    assert result["status"] == "rejected"
    assert "sticker" in result["error"]
    assert "accepted_types" in result
    assert set(result["accepted_types"]) == {
        "image",
        "audio",
        "video",
        "document",
        "location",
        "unsupported",
        "unknown",
    }


def test_missing_user_number_rejected(inbound_media_module):
    register = inbound_media_module.register_inbound_media
    result = asyncio.run(register(media_type="image", user_number=""))
    assert result["status"] == "rejected"
    assert "user_number" in result["error"]


def test_media_type_normalized_lowercase(inbound_media_module):
    register = inbound_media_module.register_inbound_media
    result = asyncio.run(register(media_type="  IMAGE  ", user_number="5521989091014"))
    assert result["status"] == "received"
    assert result["media_type"] == "image"


def test_user_number_trimmed(inbound_media_module):
    """user_number eh strip-ado pra evitar match com whitespace acidental no audit."""
    register = inbound_media_module.register_inbound_media
    asyncio.run(register(media_type="image", user_number="  5521989091014  "))
    logs = inbound_media_module._test_log_messages
    # Log audit deve conter o numero trimado (sem espacos)
    assert any("'user_number': '5521989091014'" in m for _, m in logs)


def test_null_fields_stripped_from_log(inbound_media_module):
    """Campos None devem ser omitidos do log audit pra reduzir ruido."""
    register = inbound_media_module.register_inbound_media
    asyncio.run(
        register(
            media_type="image",
            user_number="5521989091014",
            file_extension="png",
        )
    )
    logs = inbound_media_module._test_log_messages
    # 'latitude' nao foi passado — nao deve aparecer no log
    audit_log = next(m for _, m in logs if "receive-stub" in m)
    assert "latitude" not in audit_log
    assert "file_extension" in audit_log
