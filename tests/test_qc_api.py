from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.qc_api import get_store
from app.qc_store import QcStore

INSTRUMENT = "ICP-01"
TEST_ITEM = "lead"
BASE = f"/api/v1/qc/{INSTRUMENT}/{TEST_ITEM}"


@pytest.fixture()
def client(tmp_path):
    store = QcStore(tmp_path / "api-qc.db")
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def add_batch(client, revision, uid, measured, ids=("W1",), stddev="0.20"):
    return client.post(
        BASE + "/batches",
        json={
            "revision": revision,
            "batch_uid": uid,
            "water_result_ids": list(ids),
            "measured": measured,
            "target": "10.00",
            "stddev": stddev,
        },
    )


def test_query_returns_records_conclusions_and_revision_in_one_call(client):
    response = client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "instrument": INSTRUMENT,
        "test_item": TEST_ITEM,
        "revision": 0,
        "batches": [],
        "conclusions": [],
    }


def test_full_add_correct_backfill_flow(client):
    # 1) 新增：z=4.1 → 暂停。
    created = add_batch(client, 0, "B1", "10.82", ids=("W1", "W2"))
    assert created.status_code == 201
    body = created.json()
    assert body["revision"] == 1
    assert body["conclusion"]["status"] == "paused"
    assert body["conclusion"]["z"] == "4.1"
    assert body["changed_water_results"] == [
        {
            "water_result_id": "W1",
            "seq": 1,
            "batch_uid": "B1",
            "before_status": None,
            "after_status": "paused",
        },
        {
            "water_result_id": "W2",
            "seq": 1,
            "batch_uid": "B1",
            "before_status": None,
            "after_status": "paused",
        },
    ]

    # 2) 再新增一批 z=2.5：与前一批同号且都 >2 → 暂停。
    second = add_batch(client, 1, "B2", "10.50", ids=("W3",))
    assert second.json()["conclusion"]["status"] == "paused"
    assert second.json()["revision"] == 2

    # 3) 更正 B1 实测值为 10.10（z=0.5）：两批都回放行。
    corrected = client.put(
        BASE + "/batches/1/correction",
        json={"revision": 2, "measured": "10.10"},
    )
    assert corrected.status_code == 200
    cbody = corrected.json()
    assert cbody["revision"] == 3
    assert cbody["conclusion"]["z"] == "0.5"
    changed_ids = {c["water_result_id"]: c for c in cbody["changed_water_results"]}
    assert changed_ids["W1"]["before_status"] == "paused"
    assert changed_ids["W1"]["after_status"] == "released"
    assert changed_ids["W3"]["after_status"] == "released"

    # 4) 补录空缺序号 3，z=2.6；其前一批序号 2 的 z=2.5，同号且都 >2 → 补录批暂停。
    backfilled = client.post(
        BASE + "/backfill",
        json={
            "revision": 3,
            "seq": 3,
            "batch_uid": "B0",
            "water_result_ids": ["W0"],
            "measured": "10.52",
            "target": "10.00",
            "stddev": "0.20",
        },
    )
    assert backfilled.status_code == 201
    bbody = backfilled.json()
    assert [b["seq"] for b in bbody["batches"]] == [1, 2, 3]
    statuses = {c["seq"]: c["status"] for c in bbody["conclusions"]}
    assert statuses == {1: "released", 2: "released", 3: "paused"}
    # 补录批次自身的水样结果以 before_status=null 列出。
    assert bbody["changed_water_results"] == [
        {
            "water_result_id": "W0",
            "seq": 3,
            "batch_uid": "B0",
            "before_status": None,
            "after_status": "paused",
        }
    ]


def test_stale_revision_returns_409_without_side_effects(client):
    add_batch(client, 0, "B1", "10.20")
    stale = add_batch(client, 0, "B2", "10.20")
    assert stale.status_code == 409
    assert "修订号" in stale.json()["detail"]

    snapshot = client.get(BASE).json()
    assert snapshot["revision"] == 1
    assert len(snapshot["batches"]) == 1


def test_correction_unknown_seq_returns_404(client):
    response = client.put(
        BASE + "/batches/99/correction",
        json={"revision": 0, "measured": "10.10"},
    )
    assert response.status_code == 404


def test_backfill_occupied_seq_returns_409(client):
    add_batch(client, 0, "B1", "10.20")
    response = client.post(
        BASE + "/backfill",
        json={
            "revision": 1,
            "seq": 1,
            "batch_uid": "B9",
            "water_result_ids": ["W9"],
            "measured": "10.20",
            "target": "10.00",
            "stddev": "0.20",
        },
    )
    assert response.status_code == 409


def test_duplicate_batch_uid_returns_409(client):
    add_batch(client, 0, "B1", "10.20")
    response = add_batch(client, 1, "B1", "10.20")
    assert response.status_code == 409


def test_invalid_decimal_strings_return_422(client):
    bad_payloads = [
        {"measured": "1e3", "target": "10.00", "stddev": "0.20"},
        {"measured": "NaN", "target": "10.00", "stddev": "0.20"},
        {"measured": "10.00", "target": "10.00", "stddev": "0"},
        {"measured": "10.00", "target": "10.00", "stddev": "-0.20"},
        {"measured": " 10.0", "target": "10.00", "stddev": "0.20"},
    ]
    for payload in bad_payloads:
        response = client.post(
            BASE + "/batches",
            json={
                "revision": 0,
                "batch_uid": "BX",
                "water_result_ids": ["W1"],
                **payload,
            },
        )
        assert response.status_code == 422, payload


def test_duplicate_water_result_ids_in_batch_return_422(client):
    response = client.post(
        BASE + "/batches",
        json={
            "revision": 0,
            "batch_uid": "B1",
            "water_result_ids": ["W1", "W1"],
            "measured": "10.20",
            "target": "10.00",
            "stddev": "0.20",
        },
    )
    assert response.status_code == 422


def test_docs_include_correction_and_backfill_examples(client):
    schema = client.get("/openapi.json").json()
    correction_path = (
        "/api/v1/qc/{instrument}/{test_item}/batches/{seq}/correction"
    )
    backfill_path = "/api/v1/qc/{instrument}/{test_item}/backfill"
    correction = schema["paths"][correction_path]["put"]
    backfill = schema["paths"][backfill_path]["post"]
    assert "示例" in correction["summary"] or "更正" in correction["summary"]
    correction_example = correction["requestBody"]["content"]["application/json"][
        "examples"
    ]
    backfill_example = backfill["requestBody"]["content"]["application/json"][
        "examples"
    ]
    assert correction_example["default"]["value"]["measured"] == "10.10"
    assert backfill_example["default"]["value"]["seq"] == 1


def test_existing_matcher_api_still_works(client):
    assert client.get("/health").status_code == 200
    response = client.post(
        "/api/v1/check",
        json={"submissions": [], "instruments": []},
    )
    assert response.status_code == 200
    assert response.json()["summary"]["max_matched_pairs"] == 0
