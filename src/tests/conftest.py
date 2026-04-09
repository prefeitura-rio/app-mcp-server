"""
Configuração global de testes.

Este arquivo centraliza variáveis de ambiente compartilhadas para evitar
duplicação nos conftests por domínio (tools, interceptor, unit, etc.).
"""

import os

import pytest


TEST_ENV_DEFAULTS = {
    "VALID_TOKENS": "test-token",
    "ERROR_INTERCEPTOR_URL": "https://test.interceptor.local/api",
    "ERROR_INTERCEPTOR_TOKEN": "test-token-123",
    "TYPESENSE_ACTIVE": "false",
    "TYPESENSE_HUB_SEARCH_URL": "https://test.typesense.local/search",
    "TYPESENSE_PARAMETERS": "none",
    "GMAPS_API_TOKEN": "test-gmaps-token",
    "GOOGLE_MAPS_API_KEY": "test-google-key",
    "GOOGLE_MAPS_API_URL": "https://maps.googleapis.com/maps/api/geocode/json",
    "NOMINATIM_API_URL": "https://nominatim.openstreetmap.org/search",
    "GOOGLE_BIGQUERY_PAGE_SIZE": "100",
    "RMI_API_URL": "https://test.rmi.local/api",
    "CHATBOT_INTEGRATIONS_URL": "https://test.integrations.local/api",
    "CHATBOT_INTEGRATIONS_KEY": "test-key",
    "CHATBOT_PGM_API_URL": "https://test.pgm.local/api",
    "CHATBOT_PGM_ACCESS_KEY": "test-access-key",
    "ENVIRONMENT": "test",
    "GCP_SERVICE_ACCOUNT_CREDENTIALS": "e30=",
    "GEMINI_API_KEY": "test-gemini-key",
    "GEMINI_MODEL": "gemini-2.0-flash",
    "SURKAI_API_KEY": "test-surkai-key",
    "DATA_DIR": "/tmp",
    "EQUIPMENTS_VALID_THEMES": '["geral"]',
    "MEMORY_API_URL": "https://test.memory.local/api",
    "MEMORY_API_TOKEN": "test-memory-token",
}


for key, value in TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(key, value)


@pytest.fixture(autouse=True)
def test_env_defaults(monkeypatch):
    for key, value in TEST_ENV_DEFAULTS.items():
        monkeypatch.setenv(key, value)
