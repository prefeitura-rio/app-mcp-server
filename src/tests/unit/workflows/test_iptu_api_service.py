import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest

from src.tools.multi_step_service.core.models import ServiceState


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def ensure_package(name: str, path: Path):
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg
    return pkg


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def prepare_service_module(monkeypatch, module_name="test_iptu_api_service_module"):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package(
        "src.tools.multi_step_service",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service",
    )
    ensure_package(
        "src.tools.multi_step_service.workflows",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "workflows",
    )
    ensure_package(
        "src.tools.multi_step_service.workflows.iptu_pagamento",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "iptu_pagamento",
    )
    ensure_package(
        "src.tools.multi_step_service.workflows.iptu_pagamento.core",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "iptu_pagamento"
        / "core",
    )
    ensure_package(
        "src.tools.multi_step_service.workflows.iptu_pagamento.api",
        PROJECT_ROOT
        / "src"
        / "tools"
        / "multi_step_service"
        / "workflows"
        / "iptu_pagamento"
        / "api",
    )

    env_module = types.SimpleNamespace(
        IPTU_API_URL="https://iptu.example",
        IPTU_API_TOKEN="token-123",
        PROXY_URL="https://proxy.example",
        WA_IPTU_URL="https://wa.example",
        WA_IPTU_PUBLIC_KEY="public-key",
        WA_IPTU_TOKEN="wa-token",
        GCP_SERVICE_ACCOUNT_CREDENTIALS="e30=",
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=None),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=lambda *a, **k: lambda f: f),
    )
    monkeypatch.setitem(
        sys.modules,
        "google.cloud",
        types.SimpleNamespace(storage=types.SimpleNamespace(Client=object)),
    )
    monkeypatch.setitem(
        sys.modules,
        "google.cloud.storage",
        types.SimpleNamespace(Client=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "google.oauth2",
        types.SimpleNamespace(
            service_account=types.SimpleNamespace(Credentials=object)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "google.oauth2.service_account",
        types.SimpleNamespace(Credentials=object),
    )

    load_module(
        "src.tools.multi_step_service.workflows.iptu_pagamento.core.models",
        "src/tools/multi_step_service/workflows/iptu_pagamento/core/models.py",
    )
    load_module(
        "src.tools.multi_step_service.workflows.iptu_pagamento.api.exceptions",
        "src/tools/multi_step_service/workflows/iptu_pagamento/api/exceptions.py",
    )
    return load_module(
        module_name,
        "src/tools/multi_step_service/workflows/iptu_pagamento/api/api_service.py",
    )


iptu_utils = sys.modules[
    "src.tools.multi_step_service.workflows.iptu_pagamento.helpers.utils"
]
iptu_state_helpers = sys.modules[
    "src.tools.multi_step_service.workflows.iptu_pagamento.helpers.state_helpers"
]
iptu_models = sys.modules[
    "src.tools.multi_step_service.workflows.iptu_pagamento.core.models"
]


class FakeApiService:
    def parse_brazilian_currency(self, value_str: str) -> float:
        return float(value_str.replace(".", "").replace(",", "."))


def _make_cota(
    numero_cota: str,
    esta_paga: bool,
    valor: str,
    vencimento: str,
    esta_vencida: bool = False,
    valor_numerico: float = 0.0,
):
    return iptu_models.Cota(
        Situacao={"codigo": "02"},
        NCota=numero_cota,
        ValorCota=valor,
        DataVencimento=vencimento,
        ValorPago="0,00",
        DataPagamento="",
        QuantDiasEmAtraso="0",
        esta_paga=esta_paga,
        esta_vencida=esta_vencida,
        valor_numerico=valor_numerico,
    )


def test_inscricao_imobiliaria_padding():
    payload = iptu_models.InscricaoImobiliariaPayload.model_validate(
        {"inscricao_imobiliaria": "1234"}
    )
    assert payload.inscricao_imobiliaria == "00001234"


def test_inscricao_imobiliaria_rejects_too_long_value():
    with pytest.raises(ValueError, match="não pode ter mais de 8"):
        iptu_models.InscricaoImobiliariaPayload.model_validate(
            {"inscricao_imobiliaria": "123456789"}
        )


def test_ano_exercicio_accepts_string():
    payload = iptu_models.EscolhaAnoPayload.model_validate({"ano_exercicio": "2025"})
    assert payload.ano_exercicio == 2025


def test_ano_exercicio_rejects_out_of_range():
    with pytest.raises(ValueError, match="Ano de exercício inválido"):
        iptu_models.EscolhaAnoPayload.model_validate({"ano_exercicio": 1999})


def test_darm_accepts_real_api_qrcodepix_key():
    darm = iptu_models.Darm.model_validate(
        {
            "Cotas": [{"ncota": "01", "valor": "89,44"}],
            "Inscricao": "12345678",
            "Exercicio": "2026",
            "NGuia": "00",
            "Tipo": "ORDINÁRIA",
            "DataVencimento": "30/04/2026",
            "ValorIPTUOriginal": "100,00",
            "ValorDARM": "89,44",
            "ValorDescCotaUnica": "0,00",
            "CreditoNotaCarioca": "0,00",
            "CreditoDECAD": "0,00",
            "CreditoIsencao": "0,00",
            "CreditoEmissao": "0,00",
            "ValorAPagar": "89,44",
            "SequenciaNumerica": "123.456 789",
            "ChavePix": "pix-copia-e-cola",
            "QrCodePix": "base64-real-api",
            "DescricaoDARM": "DARM",
            "CodReceita": "310-7",
            "DesReceita": "RECEITA",
            "Endereco": None,
            "Nome": None,
        }
    )

    assert darm.qr_code_pix == "base64-real-api"


def test_formatar_valor_brl():
    assert iptu_utils.formatar_valor_brl(None) == "R$ 0,00"
    assert iptu_utils.formatar_valor_brl(1234.56) == "R$ 1.234,56"


def test_preparar_dados_guias_para_template():
    dados = {
        "guias": [
            {
                "numero_guia": "00",
                "tipo": "iptu",
                "valor_iptu_original_guia": "1.234,56",
                "situacao": {"descricao": "EM ABERTO"},
                "esta_em_aberto": True,
            }
        ]
    }

    result = iptu_utils.preparar_dados_guias_para_template(dados, FakeApiService())

    assert result == [
        {
            "numero_guia": "00",
            "tipo": "IPTU",
            "valor_original": 1234.56,
            "situacao": "EM ABERTO",
            "esta_em_aberto": True,
        }
    ]


def test_preparar_dados_cotas_para_template():
    cotas = [
        _make_cota("01", False, "100,00", "01/01/2025", False, 100.0),
        _make_cota("02", True, "200,00", "01/02/2025", False, 200.0),
    ]
    dados = iptu_models.DadosCotas(
        inscricao_imobiliaria="123",
        exercicio="2025",
        numero_guia="00",
        tipo_guia="IPTU",
        cotas=cotas,
    )

    result = iptu_utils.preparar_dados_cotas_para_template(dados)

    assert len(result) == 1
    assert result[0]["numero_cota"] == "01"
    assert result[0]["valor_numerico"] == 100.0


def test_preparar_dados_boletos_para_template_adds_pdf_key():
    guias = [{"tipo": "darm", "numero_guia": "00", "cotas": "01"}]

    result = iptu_utils.preparar_dados_boletos_para_template(guias)

    assert result[0]["pdf"] == "Não disponível"


def test_tem_mais_cotas_disponiveis():
    state = ServiceState(user_id="u1", service_name="iptu_pagamento")
    state.data["dados_cotas"] = {"cotas": [1, 2, 3]}
    state.data["cotas_escolhidas"] = ["1", "2"]

    assert iptu_utils.tem_mais_cotas_disponiveis(state) is True

    state.data["cotas_escolhidas"] = ["1", "2", "3"]
    assert iptu_utils.tem_mais_cotas_disponiveis(state) is False


def test_tem_outras_guias_disponiveis():
    state = ServiceState(user_id="u1", service_name="iptu_pagamento")
    state.data["dados_guias"] = {"guias": [1, 2]}

    assert iptu_utils.tem_outras_guias_disponiveis(state) is True

    state.data["dados_guias"] = {"guias": [1]}
    assert iptu_utils.tem_outras_guias_disponiveis(state) is False


def test_validar_dados_obrigatorios():
    state = ServiceState(user_id="u1", service_name="iptu_pagamento")
    state.data = {"inscricao_imobiliaria": "123", "ano_exercicio": 2025}

    assert (
        iptu_state_helpers.validar_dados_obrigatorios(
            state, ["inscricao_imobiliaria", "ano_exercicio"]
        )
        is None
    )
    assert (
        iptu_state_helpers.validar_dados_obrigatorios(
            state, ["inscricao_imobiliaria", "guia_escolhida"]
        )
        == "guia_escolhida"
    )


def test_reset_para_selecao_cotas():
    state = ServiceState(user_id="u1", service_name="iptu_pagamento")
    state.data = {
        "inscricao_imobiliaria": "123",
        "cotas_escolhidas": ["01"],
        "dados_darm": {"foo": "bar"},
    }
    state.internal = {"darm_separado": True, "dados_confirmados": True}

    iptu_state_helpers.reset_para_selecao_cotas(state)

    assert state.data["inscricao_imobiliaria"] == "123"
    assert "cotas_escolhidas" not in state.data
    assert "dados_darm" not in state.data
    assert "darm_separado" not in state.internal
    assert "dados_confirmados" not in state.internal


@pytest.mark.asyncio
async def test_iptu_api_service_request_variants(monkeypatch):
    module = prepare_service_module(monkeypatch)
    service = module.IPTUAPIService(user_id="u1")

    class FakeClient:
        def __init__(self, response=None, error=None):
            self.response = response
            self.error = error

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, params=None):
            if self.error:
                raise self.error
            return self.response

    monkeypatch.setattr(
        module,
        "InterceptedHTTPClient",
        lambda **kwargs: FakeClient(FakeResponse(200, {"ok": True}, "plain")),
    )
    result = await service._make_api_request("ConsultarGuias", {"inscricao": "123"})
    assert result == {"ok": True}

    monkeypatch.setattr(
        module,
        "InterceptedHTTPClient",
        lambda **kwargs: FakeClient(FakeResponse(200, {"ok": True}, "raw-text")),
    )
    result = await service._make_api_request(
        "DownloadPdfDARM", {"inscricao": "123"}, expect_json=False
    )
    assert result == "raw-text"

    for status_code, exc_type in [
        (404, module.DataNotFoundError),
        (401, module.AuthenticationError),
        (500, module.APIUnavailableError),
        (503, module.APIUnavailableError),
        (418, module.APIUnavailableError),
    ]:

        def make_client(_status_code=status_code, **kwargs):
            return FakeClient(FakeResponse(_status_code, text="erro"))

        monkeypatch.setattr(
            module,
            "InterceptedHTTPClient",
            make_client,
        )
        with pytest.raises(exc_type):
            await service._make_api_request("ConsultarGuias", {"inscricao": "123"})

    monkeypatch.setattr(
        module,
        "InterceptedHTTPClient",
        lambda **kwargs: FakeClient(error=httpx.TimeoutException("timeout")),
    )
    with pytest.raises(module.APIUnavailableError, match="não respondeu"):
        await service._make_api_request("ConsultarGuias", {"inscricao": "123"})

    monkeypatch.setattr(
        module,
        "InterceptedHTTPClient",
        lambda **kwargs: FakeClient(error=RuntimeError("boom")),
    )
    with pytest.raises(module.APIUnavailableError, match="boom"):
        await service._make_api_request("ConsultarGuias", {"inscricao": "123"})


@pytest.mark.asyncio
async def test_iptu_api_service_helpers_and_parsing(monkeypatch):
    module = prepare_service_module(monkeypatch, "test_iptu_api_service_helpers_module")
    service = module.IPTUAPIService(user_id="u2")

    assert service._limpar_inscricao("12.345-6") == "123456"
    assert service.parse_brazilian_currency("1.234,56") == 1234.56
    assert service.parse_brazilian_currency(None) == 0.0
    assert service.parse_brazilian_currency("inválido") == 0.0

    async def fake_request(endpoint, params, expect_json=True):
        if endpoint == "ConsultarGuias":
            return [
                {
                    "Situacao": {"codigo": "01", "descricao": "EM ABERTO"},
                    "Inscricao": "12345678",
                    "Exercicio": "2025",
                    "NGuia": "00",
                    "Tipo": "ORDINÁRIA",
                    "ValorIPTUOriginalGuia": "1.234,56",
                    "DataVenctoDescCotaUnica": "07/02/2025",
                    "QuantDiasEmAtraso": "0",
                    "PercentualDescCotaUnica": "00007",
                    "ValorIPTUDescontoAvista": "1.200,00",
                    "ValorParcelas": "100,00",
                    "CreditoNotaCarioca": "0,00",
                    "CreditoDECAD": "0,00",
                    "CreditoIsencao": "0,00",
                    "CreditoCotaUnica": "0,00",
                    "ValorQuitado": "0,00",
                    "DataQuitacao": "",
                    "Deposito": "N",
                },
                {"inválido": True},
            ]
        if endpoint == "ConsultarCotas":
            return {
                "Cotas": [
                    {
                        "Situacao": {"codigo": "03", "descricao": "VENCIDA"},
                        "NCota": "01",
                        "ValorCota": "89,44",
                        "DataVencimento": "07/11/2025",
                        "ValorPago": "0,00",
                        "DataPagamento": "",
                        "QuantDiasEmAtraso": "10",
                    },
                    {"inválido": True},
                ]
            }
        if endpoint == "ConsultarDARM":
            return {
                "Cotas": [{"ncota": "01", "valor": "89,44"}],
                "Inscricao": "12345678",
                "Exercicio": "2025",
                "NGuia": "00",
                "Tipo": "ORDINÁRIA",
                "DataVencimento": "30/04/2026",
                "ValorIPTUOriginal": "100,00",
                "ValorDARM": "89,44",
                "ValorDescCotaUnica": "0,00",
                "CreditoNotaCarioca": "0,00",
                "CreditoDECAD": "0,00",
                "CreditoIsencao": "0,00",
                "CreditoEmissao": "0,00",
                "ValorAPagar": "89,44",
                "SequenciaNumerica": "123.456 789",
                "ChavePix": "pix-copia-e-cola",
                "DescricaoDARM": "DARM",
                "CodReceita": "310-7",
                "DesReceita": "RECEITA",
                "Endereco": None,
                "Nome": None,
            }
        if endpoint == "DownloadPdfDARM":
            return "JVBERi0xLjQK"
        return None

    monkeypatch.setattr(service, "_make_api_request", fake_request)

    async def fake_upload_base64_to_gcs(base64_content):
        return "signed-url"

    async def fake_get_short_url(url, **_kwargs):
        return "short-url"

    monkeypatch.setattr(service, "upload_base64_to_gcs", fake_upload_base64_to_gcs)
    monkeypatch.setattr(service, "get_short_url", fake_get_short_url)

    dados_guias = await service.consultar_guias("12.345.678", 2025)
    assert dados_guias.inscricao_imobiliaria == "12345678"
    assert dados_guias.total_guias == 1
    assert dados_guias.guias[0].esta_em_aberto is True

    dados_cotas = await service.obter_cotas("12.345.678", 2025, "00")
    assert dados_cotas.total_cotas == 1
    assert dados_cotas.cotas[0].esta_vencida is True
    assert dados_cotas.valor_total == 89.44

    dados_darm = await service.consultar_darm("12.345.678", 2025, "00", ["01"])
    assert dados_darm.darm.codigo_barras == "123456789"
    assert dados_darm.darm.chave_pix == "pix-copia-e-cola"

    pdf_url = await service.download_pdf_darm("12.345.678", 2025, "00", ["01"])
    assert pdf_url == "short-url"


def test_pix_page_helpers_build_copy_page():
    pix_page = load_module(
        "test_iptu_pix_page_module",
        "src/tools/multi_step_service/workflows/iptu_pagamento/pix_page.py",
    )

    png_base64 = "iVBORw0KGgo="
    assert pix_page.normalize_base64_image(f"data:image/png;base64,{png_base64}") == (
        png_base64
    )
    assert pix_page.normalize_base64_image(png_base64) == png_base64

    assert pix_page.image_content_type(b"\x89PNG\r\n\x1a\nabc") == "image/png"
    assert pix_page.image_content_type(b"\xff\xd8\xffabc") == "image/jpeg"
    assert pix_page.image_content_type(b"GIF87aabc") == "image/gif"
    assert pix_page.image_content_type(b"RIFFabcdWEBPabc") == "image/webp"
    assert pix_page.image_content_type(b"unknown") == "image/png"

    html_page = pix_page.build_pix_copy_page(
        qr_code_pix=png_base64,
        pix_code='pix<code>"&',
    )

    assert "Pix IPTU" in html_page
    assert "data:image/png;base64,iVBORw0KGgo=" in html_page
    assert "pix&lt;code&gt;&quot;&amp;" in html_page
    assert 'pix<code>\\"&' in html_page
    assert "Código Pix copiado." in html_page

    expired_page = pix_page.build_expired_pix_page()
    assert "Link expirado" in expired_page


@pytest.mark.asyncio
async def test_pix_page_service_uploads_and_shortens_url(monkeypatch):
    pix_page = load_module(
        "src.tools.multi_step_service.workflows.iptu_pagamento.pix_page",
        "src/tools/multi_step_service/workflows/iptu_pagamento/pix_page.py",
    )
    module = load_module(
        "test_iptu_pix_page_service_module",
        "src/tools/multi_step_service/workflows/iptu_pagamento/pix_page_service.py",
    )

    env_module = types.SimpleNamespace(
        WORKFLOWS_GCP_SERVICE_ACCOUNT="e30=",
        WORKFLOWS_GCS_BUCKET="bucket-name",
        SHORT_API_URL="https://pref.rio",
        SHORT_API_TOKEN="short-token",
    )
    monkeypatch.setattr(module, "env", env_module)
    monkeypatch.setattr(
        module.service_account.Credentials,
        "from_service_account_info",
        lambda info: "credentials",
    )

    class FakeBlob:
        def __init__(self):
            self.content = None
            self.content_type = None

        def upload_from_string(self, content, content_type):
            self.content = content
            self.content_type = content_type

        def generate_signed_url(self, expiration):
            assert expiration == module.dt.timedelta(hours=module.PIX_PAGE_TTL_HOURS)
            return "https://storage.example/signed"

    class FakeBucket:
        def __init__(self):
            self.blob_value = FakeBlob()
            self.blob_name = None

        def blob(self, name):
            self.blob_name = name
            return self.blob_value

    fake_bucket = FakeBucket()

    service = module.IPTUPixPageService(user_id="user-1")
    monkeypatch.setattr(service, "_get_workflows_gcs_bucket", lambda: fake_bucket)

    signed_url = await service.upload_pix_copy_page_to_gcs(
        qr_code_pix="iVBORw0KGgo=",
        pix_code="pix-code",
    )

    assert signed_url == "https://storage.example/signed"
    assert fake_bucket.blob_name.startswith("iptu/qrcode-pix/")
    assert fake_bucket.blob_name.endswith(".html")
    assert fake_bucket.blob_value.content_type == "text/html; charset=utf-8"
    assert "pix-code" in fake_bucket.blob_value.content
    assert pix_page.build_expired_pix_page()

    captured = {}

    class FakeShortenerResponse:
        status_code = 201

        def json(self):
            return {"short_path": "abc123"}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["payload"] = json
            captured["headers"] = headers
            return FakeShortenerResponse()

    monkeypatch.setattr(module, "InterceptedHTTPClient", FakeClient)

    short_url = await service.get_short_url(
        url="https://storage.example/signed",
        title="Titulo",
        description="Descricao",
        expires_at="2026-05-11T12:00:00Z",
        image_url="https://example.com/image.png",
        short_path="meu-link",
    )

    assert short_url == "https://pref.rio/link/abc123"
    assert captured["url"] == "https://pref.rio/link/api/urls"
    assert captured["headers"]["Authorization"] == "Bearer short-token"
    assert captured["payload"] == {
        "description": "Descricao",
        "destination": "https://storage.example/signed",
        "expires_at": "2026-05-11T12:00:00Z",
        "image_url": "https://example.com/image.png",
        "short_path": "meu-link",
        "title": "Titulo",
    }


@pytest.mark.asyncio
async def test_pix_page_service_shortener_fallbacks(monkeypatch):
    module = load_module(
        "test_iptu_pix_page_service_fallback_module",
        "src/tools/multi_step_service/workflows/iptu_pagamento/pix_page_service.py",
    )
    env_module = types.SimpleNamespace(
        SHORT_API_URL="https://pref.rio",
        SHORT_API_TOKEN="short-token",
    )
    monkeypatch.setattr(module, "env", env_module)

    class FakeResponse:
        status_code = 500

    class FakeClient:
        def __init__(self, response=None, error=None, **kwargs):
            self.response = response or FakeResponse()
            self.error = error

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json=None, headers=None):
            if self.error:
                raise self.error
            return self.response

    service = module.IPTUPixPageService(user_id="user-1")

    monkeypatch.setattr(module, "InterceptedHTTPClient", lambda **kwargs: FakeClient())
    assert await service.get_short_url("url", "title", "description") is None

    monkeypatch.setattr(
        module,
        "InterceptedHTTPClient",
        lambda **kwargs: FakeClient(error=module.httpx.TimeoutException("timeout")),
    )
    assert await service.get_short_url("url", "title", "description") is None

    monkeypatch.setattr(
        module,
        "InterceptedHTTPClient",
        lambda **kwargs: FakeClient(error=RuntimeError("boom")),
    )
    assert await service.get_short_url("url", "title", "description") is None

    async def fake_upload(qr_code_pix, pix_code):
        return "signed-url"

    async def fake_shortener(**kwargs):
        return None

    monkeypatch.setattr(service, "upload_pix_copy_page_to_gcs", fake_upload)
    monkeypatch.setattr(service, "get_short_url", fake_shortener)

    assert (
        await service.create_pix_copy_page_url(
            qr_code_pix="iVBORw0KGgo=",
            pix_code="pix-code",
        )
        == "signed-url"
    )


def test_pix_page_service_formats_expiration_as_utc():
    module = load_module(
        "test_iptu_pix_page_service_format_module",
        "src/tools/multi_step_service/workflows/iptu_pagamento/pix_page_service.py",
    )
    expiration = module.dt.datetime(
        2026,
        5,
        11,
        9,
        30,
        15,
        123456,
        tzinfo=module.dt.timezone(module.dt.timedelta(hours=-3)),
    )

    assert module.format_expires_at(expiration) == "2026-05-11T12:30:15Z"
