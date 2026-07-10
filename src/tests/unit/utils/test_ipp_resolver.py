"""Testes do resolve_ipp_codes (#D-2): re-derivação determinística dos códigos IPP
do SGRC a partir do endereço COMPLETO, com fallback (bounded) pros códigos do LLM.
`asyncio_mode = "auto"` dispensa marker.
"""

import asyncio

from src.utils import ipp_resolver
from src.utils.ipp_resolver import resolve_ipp_codes


class _FakeAddressService:
    """address_service mockado — captura a query e controla geo/ipp/latência."""

    def __init__(self, geo=None, ipp=None, raise_on_geo=False, geo_sleep=0.0):
        self._geo = geo or {}
        self._ipp = ipp or {}
        self._raise_on_geo = raise_on_geo
        self._geo_sleep = geo_sleep
        self.last_query = None
        self.geo_calls = 0
        self.ipp_calls = 0

    async def google_geolocator(self, address):
        self.geo_calls += 1
        self.last_query = address
        if self._geo_sleep:
            await asyncio.sleep(self._geo_sleep)
        if self._raise_on_geo:
            raise RuntimeError("geocode fora do ar")
        return self._geo

    async def get_endereco_info(self, **_kwargs):
        self.ipp_calls += 1
        return self._ipp


_VALID_GEO = {"valid": True, "latitude": -22.9, "longitude": -43.1}


async def test_rederives_overriding_llm_codes():
    svc = _FakeAddressService(
        geo=_VALID_GEO, ipp={"logradouro_id": "111", "bairro_id": "222"}
    )
    out = await resolve_ipp_codes(
        svc, "Rua X", "100", "Centro", "20000-000", "999", "888"
    )
    assert out == ("111", "222")  # re-derivado sobrepõe o LLM


async def test_query_includes_full_address():
    # P1 do review: a query precisa levar bairro + CEP (senão rua homônima resolve
    # outro ponto e sobrescreve código correto com errado).
    svc = _FakeAddressService(
        geo=_VALID_GEO, ipp={"logradouro_id": "111", "bairro_id": "222"}
    )
    await resolve_ipp_codes(svc, "Rua X", "100", "Catete", "22220-000", "9", "8")
    assert "Catete" in svc.last_query
    assert "22220-000" in svc.last_query
    assert "100" in svc.last_query


async def test_fallback_when_geo_invalid():
    svc = _FakeAddressService(geo={"valid": False})
    out = await resolve_ipp_codes(svc, "Rua X", "100", "Centro", "", "999", "888")
    assert out == ("999", "888")  # mantém o do LLM
    assert svc.ipp_calls == 0  # nem chega no get_endereco_info


async def test_fallback_on_exception():
    svc = _FakeAddressService(raise_on_geo=True)
    out = await resolve_ipp_codes(svc, "Rua X", "100", "Centro", "", "999", "888")
    assert out == ("999", "888")  # exceção → mantém o do LLM


async def test_fallback_on_timeout(monkeypatch):
    # Serviço travado além do timeout → cai no fallback (não segura o chamado).
    monkeypatch.setattr(ipp_resolver, "_IPP_LOOKUP_TIMEOUT_SECONDS", 0.05)
    svc = _FakeAddressService(geo=_VALID_GEO, geo_sleep=1.0)
    out = await resolve_ipp_codes(svc, "Rua X", "100", "Centro", "", "999", "888")
    assert out == ("999", "888")  # timeout → mantém o do LLM


async def test_keeps_llm_when_derived_is_zero():
    # "0" = "não achei" no get_endereco_info → não sobrescreve.
    svc = _FakeAddressService(
        geo=_VALID_GEO, ipp={"logradouro_id": "0", "bairro_id": "0"}
    )
    out = await resolve_ipp_codes(svc, "Rua X", "100", "Centro", "", "999", "888")
    assert out == ("999", "888")


async def test_partial_override():
    # logradouro derivado válido, bairro "0" → só o logradouro é sobrescrito.
    svc = _FakeAddressService(
        geo=_VALID_GEO, ipp={"logradouro_id": "111", "bairro_id": "0"}
    )
    out = await resolve_ipp_codes(svc, "Rua X", "100", "Centro", "", "999", "888")
    assert out == ("111", "888")
