import datetime
import json

import pytest

from src.utils import bigquery as bigquery_module
from src.utils.json_utils import CustomJSONEncoder


@pytest.fixture(autouse=True)
def _no_real_gcp_credentials(monkeypatch):
    monkeypatch.setattr(bigquery_module, "get_bigquery_client", lambda: None)


def test_save_response_in_bq_serializes_datetime_time_payload(monkeypatch):
    """Regression test for CHATR-100/CHATR-106.

    A payload containing a raw datetime.time value (as returned by BigQuery
    for TIME columns, e.g. horario_funcionamento) must not raise TypeError
    when save_response_in_bq serializes it for storage.
    """
    captured = {}

    class FakeClient:
        def insert_rows_json(self, table_full_name, json_data):
            captured["table_full_name"] = table_full_name
            captured["json_data"] = json_data
            return []

    monkeypatch.setattr(bigquery_module, "get_bigquery_client", lambda: FakeClient())

    payload = {
        "equipamento": "UPA Copacabana",
        "horario_funcionamento": datetime.time(8, 30, 0),
        "atualizado_em": datetime.datetime(2026, 4, 8, 9, 0, 0, tzinfo=datetime.UTC),
        "inaugurado_em": datetime.date(2010, 1, 1),
    }

    bigquery_module.save_response_in_bq(
        data=payload,
        endpoint="/equipamentos/upa",
        dataset_id="test_dataset",
        table_id="test_table",
        environment="test",
    )

    saved_data_str = captured["json_data"][0]["data"]
    decoded = json.loads(saved_data_str)

    assert decoded["horario_funcionamento"] == "08:30:00"
    assert decoded["atualizado_em"] == "2026-04-08T09:00:00+00:00"
    assert decoded["inaugurado_em"] == "2010-01-01"


def test_get_bigquery_result_converts_time_column_to_iso_string(monkeypatch):
    """Regression test for CHATR-108.

    get_bigquery_result must convert datetime.time values (BigQuery TIME
    columns) to ISO strings, not just datetime/date, since this is the
    origin of the object that later breaks JSON serialization downstream.
    """

    class FakeRow:
        def __init__(self, data):
            self._data = data

        def items(self):
            return self._data.items()

    class FakeQueryJob:
        def result(self, page_size=None):
            return [
                FakeRow(
                    {
                        "nome": "UPA Copacabana",
                        "horario_funcionamento": datetime.time(8, 30, 0),
                        "criado_em": datetime.datetime(
                            2026, 4, 8, 9, 0, 0, tzinfo=datetime.UTC
                        ),
                        "inaugurado_em": datetime.date(2010, 1, 1),
                    }
                )
            ]

    class FakeClient:
        def query(self, query):
            return FakeQueryJob()

    monkeypatch.setattr(bigquery_module, "get_bigquery_client", lambda: FakeClient())

    rows = bigquery_module.get_bigquery_result("SELECT * FROM equipamentos")

    assert rows == [
        {
            "nome": "UPA Copacabana",
            "horario_funcionamento": "08:30:00",
            "criado_em": "2026-04-08T09:00:00+00:00",
            "inaugurado_em": "2010-01-01",
        }
    ]


def test_custom_json_encoder_handles_date_time_and_datetime_types():
    payload = {
        "d": datetime.date(2026, 1, 1),
        "t": datetime.time(14, 30, 0),
        "dt": datetime.datetime(2026, 1, 1, 14, 30, 0, tzinfo=datetime.UTC),
    }

    encoded = json.dumps(payload, cls=CustomJSONEncoder)
    decoded = json.loads(encoded)

    assert decoded == {
        "d": "2026-01-01",
        "t": "14:30:00",
        "dt": "2026-01-01T14:30:00+00:00",
    }
