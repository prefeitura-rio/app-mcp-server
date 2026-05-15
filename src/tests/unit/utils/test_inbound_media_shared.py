"""
Tests pra src/utils/inbound_media_shared.py — helpers compartilhados
entre vision/audio (e futuros tipos analyzable).

Mocks meta_cdn_client e salesforce_client; sem rede real.
"""

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
def shared_module(monkeypatch):
    """Carrega shared module com env + log stubs."""
    log_msgs: list = []
    fake_logger = types.SimpleNamespace(
        info=lambda msg: log_msgs.append(("info", msg)),
        warning=lambda msg: log_msgs.append(("warning", msg)),
        error=lambda msg: log_msgs.append(("error", msg)),
        debug=lambda msg: log_msgs.append(("debug", msg)),
    )
    fake_log_module = types.ModuleType("src.utils.log")
    fake_log_module.logger = fake_logger
    monkeypatch.setitem(sys.modules, "src.utils.log", fake_log_module)

    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    sys.modules.pop("src.utils.inbound_media_shared", None)
    spec = importlib.util.spec_from_file_location(
        "src.utils.inbound_media_shared",
        PROJECT_ROOT / "src" / "utils" / "inbound_media_shared.py",
    )
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module — @dataclass(frozen=True) resolve types via
    # cls.__module__ during decorator execution, então o módulo precisa estar
    # em sys.modules antes do exec.
    sys.modules["src.utils.inbound_media_shared"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._test_log_messages = log_msgs  # type: ignore[attr-defined]
    return module


# ----------------------------------------------------------------------------
# path_matches_content_version_id
# ----------------------------------------------------------------------------


def test_path_match_accepts_15_char(shared_module):
    assert shared_module.path_matches_content_version_id(
        "/services/data/v62.0/sobjects/ContentVersion/068xxx0000AB1CD/VersionData",
        "068xxx0000AB1CD",
    )


def test_path_match_accepts_18_char_vs_15_path(shared_module):
    """15-char Id é prefix do 18-char (sufixo é checksum)."""
    assert shared_module.path_matches_content_version_id(
        "/services/data/v62.0/sobjects/ContentVersion/068xxx0000AB1CD/VersionData",
        "068xxx0000AB1CDQRS",  # 15 vs 18
    )


def test_path_match_rejects_mismatch(shared_module):
    assert not shared_module.path_matches_content_version_id(
        "/services/data/v62.0/sobjects/ContentVersion/068xxx0000AB1CD/VersionData",
        "068DIFFERENTAB1",
    )


def test_path_match_rejects_empty_inputs(shared_module):
    assert not shared_module.path_matches_content_version_id("", "anyid")
    assert not shared_module.path_matches_content_version_id("/p/q/r", "")


def test_path_match_rejects_path_without_contentversion(shared_module):
    assert not shared_module.path_matches_content_version_id(
        "/services/data/v62.0/sobjects/Other/068xxx0000AB1CD/VersionData",
        "068xxx0000AB1CD",
    )


# ----------------------------------------------------------------------------
# parse_analysis_json
# ----------------------------------------------------------------------------


def test_parse_clean_json(shared_module):
    text = '{"a": 1, "b": "x"}'
    out = shared_module.parse_analysis_json(text)
    assert out == {"a": 1, "b": "x", "parsed": True}


def test_parse_strips_markdown_fence(shared_module):
    text = '```json\n{"a": 1}\n```'
    out = shared_module.parse_analysis_json(text)
    assert out == {"a": 1, "parsed": True}


def test_parse_strips_bare_fence(shared_module):
    text = '```\n{"a": 1}\n```'
    out = shared_module.parse_analysis_json(text)
    assert out == {"a": 1, "parsed": True}


def test_parse_falls_back_to_regex_when_prosed(shared_module):
    text = 'Aqui está minha análise: {"a": 1, "b": 2}. Espero ter ajudado.'
    out = shared_module.parse_analysis_json(text)
    assert out == {"a": 1, "b": 2, "parsed": True}


def test_parse_returns_raw_when_unparseable(shared_module):
    text = "completamente prosa sem JSON"
    out = shared_module.parse_analysis_json(text)
    assert out["parsed"] is False
    assert out["raw"] == text


def test_parse_empty_text(shared_module):
    out = shared_module.parse_analysis_json("")
    assert out == {"raw": "", "parsed": False}


# ----------------------------------------------------------------------------
# Response builders
# ----------------------------------------------------------------------------


def test_deferred_no_bytes_image(shared_module):
    r = shared_module.deferred_no_bytes("image")
    assert r["status"] == "deferred"
    assert "imagem" in r["suggested_reply_pt_br"].lower()


def test_deferred_no_bytes_audio(shared_module):
    r = shared_module.deferred_no_bytes("audio")
    assert r["status"] == "deferred"
    assert "áudio" in r["suggested_reply_pt_br"].lower()


def test_deferred_no_bytes_unknown_domain(shared_module):
    r = shared_module.deferred_no_bytes("video")
    assert r["status"] == "deferred"
    assert "suggested_reply_pt_br" in r


def test_rejected_subtype_mismatch_image(shared_module):
    r = shared_module.rejected_subtype_mismatch(
        detected="jpeg", declared="ogg", message_id="m1", media_domain="image"
    )
    assert r["status"] == "rejected"
    assert "detected='jpeg'" in r["error"]
    assert "declared='ogg'" in r["error"]
    assert "imagem" in r["suggested_reply_pt_br"].lower()


def test_rejected_subtype_mismatch_audio(shared_module):
    r = shared_module.rejected_subtype_mismatch(
        detected="png", declared="oga", message_id=None, media_domain="audio"
    )
    assert r["status"] == "rejected"
    assert "áudio" in r["suggested_reply_pt_br"].lower()


def test_deferred_no_gemini_key(shared_module):
    r = shared_module.deferred_no_gemini_key()
    assert r["status"] == "deferred"
    assert "GEMINI_API_KEY" in r["error"]


def test_error_gemini_failed_image(shared_module):
    r = shared_module.error_gemini_failed("TimeoutError: ...", "image")
    assert r["status"] == "error"
    assert "TimeoutError" in r["error"]
    assert "suggested_reply_pt_br" in r


# ----------------------------------------------------------------------------
# MediaTypeConfig dataclass
# ----------------------------------------------------------------------------


def test_media_type_config_is_immutable(shared_module):
    """Frozen dataclass — não dá pra mutar campos após criar."""
    config = shared_module.MediaTypeConfig(
        domain="image",
        accepted_extensions=frozenset({"jpg", "png"}),
        mime_to_extension={"image/jpeg": "jpg"},
        extension_to_mime=lambda ext: f"image/{ext or 'jpeg'}",
        max_bytes=1024,
        gemini_model="gemini-2.5-flash",
        analysis_prompt="prompt placeholder",
        suggested_reply_builder=lambda a: "reply placeholder",
    )
    with pytest.raises(Exception):  # FrozenInstanceError ou AttributeError
        config.domain = "audio"


def test_media_type_config_fields_callable(shared_module):
    """Validação semântica: callable e dict são consumíveis."""
    config = shared_module.MediaTypeConfig(
        domain="image",
        accepted_extensions=frozenset({"jpg"}),
        mime_to_extension={"image/jpeg": "jpg"},
        extension_to_mime=lambda ext: "image/jpeg",
        max_bytes=1024,
        gemini_model="m",
        analysis_prompt="p",
        suggested_reply_builder=lambda a: a.get("descricao", ""),
    )
    assert config.extension_to_mime("jpg") == "image/jpeg"
    assert config.mime_to_extension["image/jpeg"] == "jpg"
    assert config.suggested_reply_builder({"descricao": "foo"}) == "foo"
