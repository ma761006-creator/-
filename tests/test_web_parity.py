"""網頁版決策引擎 (static/sodium-engine.js) 與 Python API 的對拍測試。

網頁版把同一套臨床邏輯用 JS 重寫一份，好讓頁面不需後端即可單機運作。
這份測試把同一批輸入同時餵給兩邊，逐欄位比對輸出，任何一邊的閾值、
分支或文字改動都會在這裡失敗。
"""

import json
import shutil
import subprocess
from itertools import product
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

ENGINE = Path(__file__).resolve().parent.parent / "static" / "sodium-engine.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="需要 node 才能執行網頁版引擎")

HYPO_URL = "/api/v1/differential/hyponatremia"
HYPER_URL = "/api/v1/differential/hypernatremia"
CALC_URL = "/api/v1/calculator/adrogue-madias"

PATIENTS = [
    {"age": 45, "gender": "male", "weight_kg": 70},
    {"age": 72, "gender": "female", "weight_kg": 52.5},
    {"age": 64, "gender": "female", "weight_kg": 48},
    {"age": 65, "gender": "male", "weight_kg": 81.3},
]

RUNNER = """
const engine = require(process.argv[1]);
const fnName = process.argv[2];
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map((c) => engine[fnName](c))));
"""


def run_engine(fn_name, cases):
    proc = subprocess.run(
        [NODE, "-e", RUNNER, "--", str(ENGINE), fn_name],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def assert_parity(url, fn_name, cases):
    js_results = run_engine(fn_name, cases)
    assert len(js_results) == len(cases)
    for case, js in zip(cases, js_results):
        response = client.post(url, json=case)
        assert response.status_code == 200, case
        py = response.json()
        assert set(py) == set(js), case
        for key, py_value in py.items():
            js_value = js[key]
            if isinstance(py_value, float):
                assert js_value == pytest.approx(py_value), (key, case)
            else:
                assert js_value == py_value, (key, case)


def test_hyponatremia_parity():
    cases = []
    for patient, posm, volume, u_na, u_osm, endo in product(
        PATIENTS,
        [250, 260, 274.9, 275, 285, 295, 295.1, 320],
        ["hypovolemic", "euvolemic", "hypervolemic"],
        [None, 0, 19.9, 20, 56, 300],
        [None, 0, 99.9, 100, 410, 1500],
        [(True, True), (False, True), (True, False), (False, False)],
    ):
        cases.append(
            {
                "patient": patient,
                "measured_na": 121.5,
                "glucose": 110,
                "posm": posm,
                "volume_status": volume,
                "u_na": u_na,
                "u_osm": u_osm,
                "tsh_normal": endo[0],
                "cortisol_normal": endo[1],
            }
        )
    assert len(cases) > 4000
    assert_parity(HYPO_URL, "evaluateHyponatremia", cases)


def test_hyponatremia_glucose_correction_parity():
    cases = [
        {
            "patient": PATIENTS[0],
            "measured_na": na,
            "glucose": glucose,
            "posm": 260,
            "volume_status": "euvolemic",
            "u_osm": 410,
        }
        for na, glucose in product(
            [80, 99.5, 113.7, 125, 134.9],
            [10, 99, 100, 100.1, 125, 180, 333, 900, 1750, 2500],
        )
    ]
    assert_parity(HYPO_URL, "evaluateHyponatremia", cases)


def test_hypernatremia_parity():
    cases = []
    for patient, na, volume, u_osm, ddavp in product(
        PATIENTS,
        [145.1, 150, 156, 178.4, 200],
        ["hypovolemic", "euvolemic", "hypervolemic"],
        [None, 0, 150, 299.9, 300, 600, 600.1, 1500],
        [None, 0, 120, 299, 300, 450, 520, 1500],
    ):
        cases.append(
            {
                "patient": patient,
                "measured_na": na,
                "volume_status": volume,
                "u_osm": u_osm,
                "ddavp_u_osm_after": ddavp,
            }
        )
    assert len(cases) > 3000
    assert_parity(HYPER_URL, "evaluateHypernatremia", cases)


def test_adrogue_madias_parity():
    cases = []
    for patient, na, infusate in product(
        PATIENTS,
        [80, 118, 125.5, 140, 156, 200],
        ["D5W", "0.45% NaCl", "0.9% NaCl", "3% NaCl", "Lactated Ringer"],
    ):
        cases.append({"patient": patient, "current_na": na, "infusate_type": infusate})
    for patient, custom_na, custom_k in product(PATIENTS, [0, 77, 154, 513, 1000], [0, 4, 40, 200]):
        cases.append(
            {
                "patient": patient,
                "current_na": 120,
                "infusate_type": "custom",
                "custom_infusate_na": custom_na,
                "custom_infusate_k": custom_k,
            }
        )
    assert_parity(CALC_URL, "calculateAdrogueMadias", cases)


def test_js_limits_match_openapi_field_constraints():
    """網頁表單的輸入邊界必須與 API 的 Field(...) 約束一致。"""
    js_limits = json.loads(
        subprocess.run(
            [NODE, "-e", "console.log(JSON.stringify(require(process.argv[1]).LIMITS))", "--", str(ENGINE)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )

    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    def prop(schema_name, field):
        return schemas[schema_name]["properties"][field]

    def constraints(schema):
        # Optional 欄位在 OpenAPI 中是 anyOf: [{...}, {type: null}]
        if "anyOf" in schema:
            schema = next(s for s in schema["anyOf"] if s.get("type") != "null")
        return {
            k: schema[k]
            for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
            if k in schema
        }

    def as_js(c):
        mapping = {
            "minimum": "ge",
            "maximum": "le",
            "exclusiveMinimum": "gt",
            "exclusiveMaximum": "lt",
        }
        return {mapping[k]: v for k, v in c.items()}

    expected = {
        "age": prop("PatientProfile", "age"),
        "weight_kg": prop("PatientProfile", "weight_kg"),
        "hyponatremia_na": prop("HyponatremiaRequest", "measured_na"),
        "hypernatremia_na": prop("HypernatremiaRequest", "measured_na"),
        "glucose": prop("HyponatremiaRequest", "glucose"),
        "posm": prop("HyponatremiaRequest", "posm"),
        "u_na": prop("HyponatremiaRequest", "u_na"),
        "u_osm": prop("HyponatremiaRequest", "u_osm"),
        "ddavp_u_osm_after": prop("HypernatremiaRequest", "ddavp_u_osm_after"),
        "current_na": prop("InfusionCalculationRequest", "current_na"),
        "custom_infusate_na": prop("InfusionCalculationRequest", "custom_infusate_na"),
        "custom_infusate_k": prop("InfusionCalculationRequest", "custom_infusate_k"),
    }

    assert set(js_limits) == set(expected)
    for name, schema in expected.items():
        assert js_limits[name] == as_js(constraints(schema)), name


def test_js_infusate_table_matches_api():
    js_table = json.loads(
        subprocess.run(
            [NODE, "-e", "console.log(JSON.stringify(require(process.argv[1]).INFUSATES))", "--", str(ENGINE)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    enum_values = client.get("/openapi.json").json()["components"]["schemas"]["InfusateTypeEnum"]["enum"]
    assert set(js_table) | {"custom"} == set(enum_values)
