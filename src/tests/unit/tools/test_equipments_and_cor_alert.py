import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


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


def passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


@pytest.mark.asyncio
async def test_cor_alert_normalization_geocode_and_create(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    saved_alerts = []
    queued_alerts = []

    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_cor_alert_in_bq_background=lambda **kwargs: asyncio.sleep(
                0, result=saved_alerts.append(kwargs)
            ),
            save_cor_alert_to_queue_background=lambda **kwargs: asyncio.sleep(
                0, result=queued_alerts.append(kwargs)
            ),
            get_datetime=lambda: "2026-04-08T10:00:00.000000",
        ),
    )
    env_module = types.SimpleNamespace(
        ENVIRONMENT="test",
        GOOGLE_MAPS_API_URL="https://maps.googleapis.com/maps/api/geocode/json",
        GOOGLE_MAPS_API_KEY="google-key",
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, params=None):
            if "latlng" in params:
                return types.SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {
                        "status": "OK",
                        "results": [
                            {
                                "address_components": [
                                    {
                                        "long_name": "Jd America",
                                        "types": ["sublocality_level_1"],
                                    }
                                ]
                            }
                        ],
                    },
                )
            return types.SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "status": "OK",
                    "results": [
                        {
                            "formatted_address": "Rua A, Rio de Janeiro - RJ",
                            "geometry": {"location": {"lat": -22.9, "lng": -43.2}},
                            "address_components": [],
                        }
                    ],
                },
            )

    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=FakeClient),
    )

    module = load_module("test_cor_alert_tools_module", "src/tools/cor_alert_tools.py")

    assert module._normalize_text("  Jd Ámerica  ") == "jd america"
    assert module.normalize_neighborhood("Jd America") == "jardim america"
    assert (
        module._extract_google_neighborhood(
            {
                "address_components": [
                    {"long_name": "Acari", "types": ["neighborhood"]},
                ]
            }
        )
        == "Acari"
    )

    coords = await module.get_coordinates_google("Rua A, 10")
    assert coords["provider"] == "Google Maps"

    coords = await module.geocode_address("Rua A, 10")
    assert coords["bairro_normalizado"] == "jardim america"

    result = await module.create_cor_alert("", "alagamento", "alta", "desc", "Rua A")
    assert result["success"] is False

    result = await module.create_cor_alert("u1", "invalido", "alta", "desc", "Rua A")
    assert result["success"] is False

    result = await module.create_cor_alert("u1", "alagamento", "alta", "desc", "Rua A")
    assert result["success"] is True
    assert saved_alerts
    assert queued_alerts

    queued_alerts.clear()
    result = await module.create_cor_alert("u1", "alagamento", "baixa", "desc", "Rua A")
    assert result["success"] is True
    assert queued_alerts == []


@pytest.mark.asyncio
async def test_equipments_tools_instructions_and_whitelist(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package(
        "src.tools.equipments", PROJECT_ROOT / "src" / "tools" / "equipments"
    )
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    monkeypatch.setitem(
        sys.modules,
        "src.tools.equipments.pluscode_service",
        types.SimpleNamespace(
            get_category_equipments=lambda: asyncio.sleep(0, result={"cats": ["A"]}),
            get_tematic_instructions_for_equipments=lambda: asyncio.sleep(
                0, result={"ok": True}
            ),
            get_pluscode_coords_equipments=lambda address, categories=None: (
                asyncio.sleep(
                    0,
                    result=[{"nome": "Equip", "categoria": "CF", "esfera": "ESTADUAL"}],
                )
            ),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_response_in_bq_background=lambda **kwargs: asyncio.sleep(0)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )
    env_module = types.SimpleNamespace(
        EQUIPMENTS_VALID_THEMES=["saude", "assistencia"],
        GOOGLE_MAPS_API_URL="https://maps.googleapis.com/maps/api/geocode/json",
        GOOGLE_MAPS_API_KEY="google-key",
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )

    monkeypatch.setitem(
        sys.modules,
        "src.tools.equipments.utils",
        types.SimpleNamespace(
            get_coords_from_google_maps_api=lambda address: {
                "lat": -22.9,
                "lng": -43.2,
                "bairro_normalizado": "outro-bairro",
            }
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.cor_alert_tools",
        types.SimpleNamespace(
            _extract_google_neighborhood=lambda result: "Acari",
            normalize_neighborhood=lambda value: "acari",
        ),
    )

    class FakeSyncClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get_sync(self, url, params=None):
            return types.SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "status": "OK",
                    "results": [{"address_components": []}],
                },
            )

    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=FakeSyncClient),
    )

    module = load_module(
        "test_equipments_tools_module", "src/tools/equipments_tools.py"
    )

    assert module.get_valid_themes() == ["saude", "assistencia"]
    assert (
        "retorne todos os equipamentos"
        in module.get_instructions_for_equipments([]).lower()
    )
    assert (
        "governo do estado do rio de janeiro"
        in module.get_instructions_for_equipments(
            [{"categoria": "CF", "esfera": "ESTADUAL"}]
        ).lower()
    )
    assert (
        "ponto de apoio"
        in module.get_instructions_for_equipments(
            [{"categoria": "PONTOS_DE_APOIO"}]
        ).lower()
    )
    assert "agendamento prévio" in module.get_instructions_for_equipments(
        [{"categoria": "CRAS"}]
    )

    monkeypatch.setattr(
        module,
        "get_equipments",
        lambda address, categories=None: asyncio.sleep(
            0, result=[{"nome": "Equip", "categoria": "CF", "esfera": "ESTADUAL"}]
        ),
    )
    result = await module.get_equipments_with_instructions("Rua A", categories=["CF"])
    assert result["equipamentos"][0]["nome"] == "Equip"
    assert "governo do estado do rio de janeiro" in result["instructions"].lower()

    result = await module.get_equipments_with_instructions(
        "Rua A", categories=["PONTOS_DE_APOIO"]
    )
    assert "defesa civil" in result["instructions"].lower()

    monkeypatch.setitem(
        sys.modules,
        "src.tools.equipments.utils",
        types.SimpleNamespace(
            get_coords_from_google_maps_api=lambda address: {
                "lat": -22.9,
                "lng": -43.2,
                "bairro_normalizado": "bairro-nao-permitido",
            }
        ),
    )
    module = load_module(
        "test_equipments_tools_module_blocked", "src/tools/equipments_tools.py"
    )
    monkeypatch.setattr(
        module,
        "get_equipments",
        lambda address, categories=None: asyncio.sleep(0, result=[]),
    )
    result = await module.get_equipments_with_instructions(
        "Rua A", categories=["PONTOS_DE_APOIO"]
    )
    assert result["equipamentos"] == []
    assert "199" in result["instructions"]

    result = await module.get_equipments_categories()
    assert result == {"cats": ["A"]}
    assert created_tasks


def test_openlocationcode_roundtrip_and_helpers():
    openlocationcode = load_module(
        "test_openlocationcode_module", "src/tools/equipments/openlocationcode.py"
    )

    code = openlocationcode.encode(47.36559, 8.524997)
    decoded = openlocationcode.decode(code)
    short = openlocationcode.shorten(code, 47.5, 8.5)

    assert openlocationcode.isValid(code)
    assert openlocationcode.isShort(short)
    assert openlocationcode.recoverNearest(short, 47.4, 8.6) == code
    assert abs(decoded.latitudeCenter - 47.36559) < 0.001
    assert abs(decoded.longitudeCenter - 8.524997) < 0.001
    assert openlocationcode.normalizeLongitude(190) == -170
