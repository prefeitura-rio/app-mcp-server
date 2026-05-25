"""
Testes pra src/tools/inbound_media_audio.py — transcricao + classificacao
de audio inbound via Gemini multimodal. Cobre paths defensivos sem chamar
Gemini real (mockamos client.aio.models.generate_content).
"""

import asyncio
import base64
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
def audio_module(monkeypatch, tmp_path):
    """Carrega src.tools.inbound_media_audio com stubs leves pra evitar pull
    de google.genai/loguru/etc. Stub do salesforce_client e do genai garantem
    isolamento puro."""

    # 1) Stub src.utils.log
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

    # 2) Stub src.config.env
    fake_env = types.ModuleType("src.config.env")
    fake_env.IS_LOCAL = False
    fake_env.GEMINI_API_KEY = "fake-key-for-tests"
    fake_env.SALESFORCE_INSTANCE_URL = None
    fake_env.SALESFORCE_CLIENT_ID = None
    fake_env.SALESFORCE_CLIENT_SECRET = None
    fake_config_pkg = types.ModuleType("src.config")
    fake_config_pkg.env = fake_env
    monkeypatch.setitem(sys.modules, "src.config", fake_config_pkg)
    monkeypatch.setitem(sys.modules, "src.config.env", fake_env)

    # 3) Stub google.genai e google.genai.types pra evitar pull do SDK real
    fake_types = types.SimpleNamespace(
        Content=lambda **kw: ("Content", kw),
        Part=lambda **kw: ("Part", kw),
        Blob=lambda **kw: ("Blob", kw),
    )
    captured_calls = {"calls": []}

    class FakeAsyncModels:
        async def generate_content(self, **kw):  # noqa: D401
            captured_calls["calls"].append(kw)
            return types.SimpleNamespace(text=captured_calls.get("response_text", ""))

    class FakeClient:
        def __init__(self, api_key=None):
            self.aio = types.SimpleNamespace(models=FakeAsyncModels())

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = FakeClient
    fake_genai.types = fake_types
    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)

    # 4) Stub src.utils.salesforce_client (download via OAuth)
    sf_calls: dict = {"paths": [], "return_bytes": None}

    async def fake_download_async(path):
        sf_calls["paths"].append(path)
        return sf_calls["return_bytes"]

    fake_sf_module = types.ModuleType("src.utils.salesforce_client")
    fake_sf_module.download_content_version_async = fake_download_async
    monkeypatch.setitem(sys.modules, "src.utils.salesforce_client", fake_sf_module)

    # 5) Asegura packages stub
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    # Force fresh re-import do shared (ele importa logger no top-level, então
    # se sobrou de fixture anterior, logger aponta pro real loguru).
    for _stale_mod in (
        "src.utils.inbound_media_shared",
        "src.utils.meta_cdn_client",
    ):
        sys.modules.pop(_stale_mod, None)

    spec = importlib.util.spec_from_file_location(
        "src.tools.inbound_media_audio",
        PROJECT_ROOT / "src" / "tools" / "inbound_media_audio.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["src.tools.inbound_media_audio"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._test_log_messages = log_messages  # type: ignore[attr-defined]
    module._test_gemini_calls = captured_calls  # type: ignore[attr-defined]
    module._test_sf_calls = sf_calls  # type: ignore[attr-defined]
    module._test_env = fake_env  # type: ignore[attr-defined]
    return module


def _run(coro):
    return asyncio.run(coro)


def test_rejected_when_extension_not_in_allowlist(audio_module):
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="webm",  # não está no allowlist Gemini
        )
    )
    assert out["status"] == "rejected"
    assert "extensão não suportada" in out["error"]
    assert "oga" in out["accepted_extensions"]
    assert "ogg" in out["accepted_extensions"]
    assert "flac" in out["accepted_extensions"]


def test_path_matches_content_version_id_accepts_15_and_18_char(audio_module):
    # Salesforce Id 15-char alfanumérico; 18-char = 15 + sufixo case-checksum
    assert (
        audio_module._path_matches_content_version_id(
            "/services/data/v62.0/sobjects/ContentVersion/068xxx0000AB1CD/VersionData",
            "068xxx0000AB1CDQRS",  # 15 vs 18 — primeiros 15 batem
        )
        is True
    )


def test_path_matches_content_version_id_rejects_mismatch(audio_module):
    assert (
        audio_module._path_matches_content_version_id(
            "/services/data/v62.0/sobjects/ContentVersion/068xxx0000AB1CD/VersionData",
            "068DIFFERENTAB1",  # primeiros 15 diferentes
        )
        is False
    )


def test_skip_download_when_content_version_id_missing(audio_module):
    """Defesa: sem content_version_id no marker, não fazemos download pra
    evitar fetch de arquivo arbitrário a partir de path injectado."""
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            salesforce_download_path=(
                "/services/data/v62.0/sobjects/ContentVersion/068xxx0000AB1CD/VersionData"
            ),
            # NÃO passa content_version_id
        )
    )
    # Sem fonte válida + sem cross-check + sem fallback bytes → deferred
    assert out["status"] == "deferred"
    assert audio_module._test_sf_calls["paths"] == []  # nada baixou
    logs = audio_module._test_log_messages
    assert any("sem content_version_id" in m for _, m in logs)


def test_skip_download_when_path_id_mismatches_marker(audio_module):
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            salesforce_download_path=(
                "/services/data/v62.0/sobjects/ContentVersion/068XYZ0000Diverg/VersionData"
            ),
            content_version_id="068ABC0000Differ",
        )
    )
    assert out["status"] == "deferred"
    assert audio_module._test_sf_calls["paths"] == []
    logs = audio_module._test_log_messages
    assert any("Id mismatch" in m for _, m in logs)


def test_oversized_base64_rejected(audio_module):
    """Anti-OOM: bytes inline maior que limite → status rejected + suggested_reply."""
    blob = b"x" * (audio_module._MAX_BYTES + 10)
    encoded = base64.b64encode(blob).decode()
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            audio_bytes_base64=encoded,
        )
    )
    assert out["status"] == "rejected"
    assert "excede" in out["error"]
    assert "suggested_reply_pt_br" in out


def test_happy_path_transcribed_with_reparo_luminaria(audio_module):
    """Cidadão fala áudio sobre luminária → tool transcreve + sugere workflow.

    Endereço mencionado no áudio → reply NÃO pede endereço de novo
    (atalho de UX — anti pedir-repetir).
    """
    audio_module._test_gemini_calls["response_text"] = (
        '{"transcricao": "tem uma luminaria queimada na rua das laranjeiras",'
        ' "resumo": "Reporte de luminária queimada",'
        ' "idioma_detectado": "pt-br",'
        ' "intencao_detectada": true,'
        ' "categoria": "luminaria_publica",'
        ' "endereco_mencionado": "rua das laranjeiras",'
        ' "workflow_sugerido": "reparo_luminaria",'
        ' "confianca": "alta"}'
    )
    fake_bytes = b"OggS" + (b"A" * 200)
    audio_module._test_sf_calls["return_bytes"] = fake_bytes

    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            salesforce_download_path=(
                "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3T/VersionData"
            ),
            content_version_id="0688800000Bgd3T",
            message_id="entry-uuid-test",
        )
    )
    assert out["status"] == "transcribed"
    assert out["analysis"]["workflow_sugerido"] == "reparo_luminaria"
    assert out["analysis"]["parsed"] is True
    reply = out["suggested_reply_pt_br"]
    assert "luminária" in reply or "luminaria" in reply.lower()
    # Reply deve ecoar o endereço extraído, e NÃO pedir rua/número/bairro de novo
    assert "rua das laranjeiras" in reply.lower()
    assert "(rua, número, bairro)" not in reply
    # Verifica que mandou bytes via inline_data
    call = audio_module._test_gemini_calls["calls"][0]
    contents = call["contents"]
    assert contents and isinstance(contents, list)


def test_workflow_without_endereco_asks_for_address(audio_module):
    """Workflow detectado mas sem endereco_mencionado → reply pergunta se deseja abrir chamado."""
    audio_module._test_gemini_calls["response_text"] = (
        '{"transcricao": "minha rua tá com luminária queimada",'
        ' "resumo": "Reporte luminária",'
        ' "intencao_detectada": true,'
        ' "categoria": "luminaria_publica",'
        ' "endereco_mencionado": "",'
        ' "workflow_sugerido": "reparo_luminaria",'
        ' "confianca": "alta"}'
    )
    audio_module._test_sf_calls["return_bytes"] = b"OggS" + (b"A" * 200)
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            salesforce_download_path=(
                "/services/data/v62.0/sobjects/ContentVersion/0688800000NoAddr/VersionData"
            ),
            content_version_id="0688800000NoAddr",
        )
    )
    # Nova mensagem não pede endereço detalhado, pergunta se deseja abrir chamado
    assert (
        "Você deseja abrir um chamado de reparo de luminária?"
        in out["suggested_reply_pt_br"]
    )


def test_workflow_none_does_not_ask_to_retype(audio_module):
    """workflow_sugerido='nenhum' com transcrição válida → reply só dá ack;
    NÃO pede pra cidadão repetir em texto. Codex review P2 fix."""
    audio_module._test_gemini_calls["response_text"] = (
        '{"transcricao": "queria saber sobre os horarios da clinica da familia",'
        ' "resumo": "Pergunta sobre horários da clínica da família",'
        ' "intencao_detectada": true,'
        ' "categoria": "duvida_geral",'
        ' "endereco_mencionado": "",'
        ' "workflow_sugerido": "nenhum",'
        ' "confianca": "alta"}'
    )
    audio_module._test_sf_calls["return_bytes"] = b"OggS" + (b"A" * 200)
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            salesforce_download_path=(
                "/services/data/v62.0/sobjects/ContentVersion/0688800000Duvida/VersionData"
            ),
            content_version_id="0688800000Duvida",
        )
    )
    reply = out["suggested_reply_pt_br"]
    # Reply ecoa o resumo mas NÃO pede texto
    assert "horários" in reply.lower() or "horarios" in reply.lower()
    assert "confirmar em texto" not in reply.lower()
    assert "descrever em texto" not in reply.lower()


def test_inintelligible_audio_returns_fallback(audio_module):
    """Quando intencao_detectada=false (audio ruidoso), reply pede texto."""
    audio_module._test_gemini_calls["response_text"] = (
        '{"transcricao": "", "intencao_detectada": false, "categoria": "nao_aplica",'
        ' "workflow_sugerido": "nenhum", "confianca": "baixa"}'
    )
    audio_module._test_sf_calls["return_bytes"] = b"OggS" + (b"A" * 200)
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            salesforce_download_path=(
                "/services/data/v62.0/sobjects/ContentVersion/0688800000NoiseTest/VersionData"
            ),
            content_version_id="0688800000NoiseTest",
        )
    )
    assert out["status"] == "transcribed"
    assert out["analysis"]["intencao_detectada"] is False
    assert "descrever em texto" in out["suggested_reply_pt_br"]


def test_mime_from_extension_maps_allowlist_formats(audio_module):
    """Allowlist tem que bater 1:1 com o que Gemini audio input documenta:
    WAV, MP3, AIFF, AAC, OGG, FLAC. M4A/AMR ficam de fora (Gemini não
    aceita) — qualquer chamada com essas extensões é rejected antes do
    mime mapping."""
    assert audio_module._mime_from_extension("oga") == "audio/ogg"
    assert audio_module._mime_from_extension("OGG") == "audio/ogg"
    assert audio_module._mime_from_extension("mp3") == "audio/mpeg"
    assert audio_module._mime_from_extension("aac") == "audio/aac"
    assert audio_module._mime_from_extension("wav") == "audio/wav"
    assert audio_module._mime_from_extension("flac") == "audio/flac"
    assert audio_module._mime_from_extension("aiff") == "audio/aiff"
    # default razoável pro WhatsApp PTT quando algo estranho chega
    assert audio_module._mime_from_extension(None) == "audio/ogg"


def test_m4a_and_amr_rejected_upstream(audio_module):
    """M4A/AMR não estão no allowlist do Gemini audio input — rejeitados
    antes de chegar ao mime mapping (não passam pro Gemini só pra falhar)."""
    for unsupported in ("m4a", "amr"):
        out = _run(
            audio_module.analyze_inbound_audio(
                user_number="5521989091014",
                file_extension=unsupported,
            )
        )
        assert out["status"] == "rejected", (
            f"esperava reject pra extensao {unsupported!r}, "
            f"mas status veio {out['status']!r}"
        )


def test_oversized_base64_rejected_before_decode(audio_module):
    """Anti-DoS: rejeitar pelo comprimento da string ANTES de decodificar
    o base64 (decode aloca 3/4 do tamanho da string em memória — sem o
    early reject um atacante poderia forçar o servidor a alocar centenas
    de MB só pra rejeitar em seguida)."""
    # String base64 grande (sem ser bytes reais) — só comprimento importa
    huge_b64 = "A" * (audio_module._MAX_BYTES * 2)  # ~ 1.5x _MAX_BYTES decoded
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            audio_bytes_base64=huge_b64,
        )
    )
    assert out["status"] == "rejected"
    logs = audio_module._test_log_messages
    assert any("abort pre-decode" in m for _, m in logs)


def test_deferred_when_no_bytes_source(audio_module):
    """Sem path + sem base64 + sem local → deferred com suggested_reply."""
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
        )
    )
    assert out["status"] == "deferred"
    assert "suggested_reply_pt_br" in out


def test_rejects_when_bytes_are_image_not_audio(audio_module):
    """Anti-hallucination: se MCP baixou JPG mas declared file_extension='oga'
    (Apex misclassified ou prompt injection), Gemini não recebe os bytes — tool
    rejeita ANTES da chamada. Reproduzido em smoke 2026-05-14."""
    fake_jpg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + (b"x" * 200)
    audio_module._test_sf_calls["return_bytes"] = fake_jpg_bytes
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            salesforce_download_path=(
                "/services/data/v62.0/sobjects/ContentVersion/0688800000Bgd3TAA/VersionData"
            ),
            content_version_id="0688800000Bgd3TAA",
        )
    )
    assert out["status"] == "rejected"
    assert "detected='jpeg'" in out["error"]
    assert "declared='oga'" in out["error"]
    # Gemini NÃO foi chamado
    assert audio_module._test_gemini_calls["calls"] == []
    logs = audio_module._test_log_messages
    assert any("subtype dos magic bytes" in m for _, m in logs)


def test_rejects_when_bytes_are_pdf_or_garbage(audio_module):
    """Bytes corrompidos ou tipo não-mídia → rejeita sem chamar Gemini."""
    pdf_bytes = b"%PDF-1.7\n" + (b"x" * 200)
    audio_module._test_sf_calls["return_bytes"] = pdf_bytes
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            salesforce_download_path=(
                "/services/data/v62.0/sobjects/ContentVersion/0688800000PdfCorr/VersionData"
            ),
            content_version_id="0688800000PdfCorr",
        )
    )
    assert out["status"] == "rejected"
    assert audio_module._test_gemini_calls["calls"] == []


def test_deferred_when_gemini_api_key_missing(audio_module):
    """Sem GEMINI_API_KEY: deferred + suggested_reply educado."""
    audio_module._test_env.GEMINI_API_KEY = ""
    audio_module._test_sf_calls["return_bytes"] = b"OggS" + (b"A" * 200)
    out = _run(
        audio_module.analyze_inbound_audio(
            user_number="5521989091014",
            file_extension="oga",
            salesforce_download_path=(
                "/services/data/v62.0/sobjects/ContentVersion/0688800000NoKey/VersionData"
            ),
            content_version_id="0688800000NoKey",
        )
    )
    assert out["status"] == "deferred"
    assert "GEMINI_API_KEY" in out["error"]
