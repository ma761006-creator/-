import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

ELDERLY_FEMALE = {"age": 72, "gender": "female", "weight_kg": 52}
ADULT_MALE = {"age": 45, "gender": "male", "weight_kg": 70}

HYPO_URL = "/api/v1/differential/hyponatremia"
HYPER_URL = "/api/v1/differential/hypernatremia"
CALC_URL = "/api/v1/calculator/adrogue-madias"


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --------------------------------------------------------------------------
# TBW 係數
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "age,gender,weight,expected_tbw",
    [
        (45, "male", 70, 42.0),    # 非老年男性 0.6
        (72, "male", 70, 35.0),    # 老年男性 0.5
        (45, "female", 60, 30.0),  # 非老年女性 0.5
        (72, "female", 52, 23.4),  # 老年女性 0.45
    ],
)
def test_tbw_ratios(age, gender, weight, expected_tbw):
    payload = {
        "patient": {"age": age, "gender": gender, "weight_kg": weight},
        "current_na": 140,
        "infusate_type": "0.9% NaCl",
    }
    r = client.post(CALC_URL, json=payload)
    assert r.status_code == 200
    assert r.json()["tbw_liters"] == pytest.approx(expected_tbw)


# --------------------------------------------------------------------------
# 低血鈉：滲透壓分層
# --------------------------------------------------------------------------
def test_hyponatremia_hypertonic():
    payload = {
        "patient": ELDERLY_FEMALE,
        "measured_na": 128,
        "glucose": 900,
        "posm": 320,
        "volume_status": "euvolemic",
    }
    body = client.post(HYPO_URL, json=payload).json()
    assert "高滲透壓性低血鈉" in body["category"]
    # Katz: 128 + 1.6 * (900-100)/100 = 140.8
    assert body["corrected_na"] == pytest.approx(140.8)


def test_hyponatremia_pseudohyponatremia():
    payload = {
        "patient": ELDERLY_FEMALE,
        "measured_na": 130,
        "glucose": 95,
        "posm": 285,
        "volume_status": "euvolemic",
    }
    body = client.post(HYPO_URL, json=payload).json()
    assert "等滲透壓性假性低血鈉" in body["category"]
    # 血糖 <= 100 不做校正
    assert body["corrected_na"] == pytest.approx(130.0)


def test_hyponatremia_siadh():
    payload = {
        "patient": ELDERLY_FEMALE,
        "measured_na": 121,
        "glucose": 110,
        "posm": 252,
        "volume_status": "euvolemic",
        "u_na": 56,
        "u_osm": 410,
        "tsh_normal": True,
        "cortisol_normal": True,
    }
    body = client.post(HYPO_URL, json=payload).json()
    assert "正常體液容積性低血鈉" in body["category"]
    assert any("SIADH" in d for d in body["differentials"])
    assert any("ODS" in a for a in body["safety_alerts"])


def test_hyponatremia_euvolemic_endocrine_takes_priority_over_siadh():
    payload = {
        "patient": ELDERLY_FEMALE,
        "measured_na": 121,
        "glucose": 100,
        "posm": 252,
        "volume_status": "euvolemic",
        "u_osm": 410,
        "tsh_normal": False,
    }
    body = client.post(HYPO_URL, json=payload).json()
    assert any("內分泌功能異常" in d for d in body["differentials"])
    assert not any("SIADH" in d for d in body["differentials"])


def test_hyponatremia_water_intoxication():
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 118,
        "glucose": 90,
        "posm": 245,
        "volume_status": "euvolemic",
        "u_osm": 60,
    }
    body = client.post(HYPO_URL, json=payload).json()
    assert any("啤酒暴飲症" in d for d in body["differentials"])


@pytest.mark.parametrize(
    "u_na,expected",
    [(10, "腎外流失"), (45, "腎臟流失")],
)
def test_hyponatremia_hypovolemic_split_by_urine_na(u_na, expected):
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 125,
        "glucose": 90,
        "posm": 265,
        "volume_status": "hypovolemic",
        "u_na": u_na,
    }
    body = client.post(HYPO_URL, json=payload).json()
    assert any(expected in d for d in body["differentials"])


@pytest.mark.parametrize(
    "u_na,expected",
    [(10, "有效循環血量不足"), (45, "腎臟排泄功能衰竭")],
)
def test_hyponatremia_hypervolemic_split_by_urine_na(u_na, expected):
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 125,
        "glucose": 90,
        "posm": 265,
        "volume_status": "hypervolemic",
        "u_na": u_na,
    }
    body = client.post(HYPO_URL, json=payload).json()
    assert any(expected in d for d in body["differentials"])


def test_hyponatremia_missing_urine_studies_prompts_for_them():
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 125,
        "glucose": 90,
        "posm": 265,
        "volume_status": "hypovolemic",
    }
    body = client.post(HYPO_URL, json=payload).json()
    assert any("需檢測 Urine Na" in d for d in body["differentials"])


@pytest.mark.parametrize("na", [79, 135, 140])
def test_hyponatremia_rejects_out_of_range_sodium(na):
    payload = {
        "patient": ADULT_MALE,
        "measured_na": na,
        "glucose": 90,
        "posm": 265,
        "volume_status": "euvolemic",
    }
    assert client.post(HYPO_URL, json=payload).status_code == 422


# --------------------------------------------------------------------------
# 高血鈉
# --------------------------------------------------------------------------
def test_hypernatremia_central_di():
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 154,
        "volume_status": "euvolemic",
        "u_osm": 150,
        "ddavp_u_osm_after": 520,
    }
    body = client.post(HYPER_URL, json=payload).json()
    assert any("中樞性尿崩症" in d for d in body["differentials"])
    # TBW 42 * (154/140 - 1) = 4.2 L
    assert body["free_water_deficit_l"] == pytest.approx(4.2)


def test_hypernatremia_nephrogenic_di():
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 154,
        "volume_status": "euvolemic",
        "u_osm": 150,
        "ddavp_u_osm_after": 180,
    }
    body = client.post(HYPER_URL, json=payload).json()
    assert any("腎因性尿崩症" in d for d in body["differentials"])


def test_hypernatremia_di_without_ddavp_suggests_the_test():
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 154,
        "volume_status": "euvolemic",
        "u_osm": 150,
    }
    body = client.post(HYPER_URL, json=payload).json()
    assert any("建議安排 DDAVP" in r for r in body["recommendations"])


@pytest.mark.parametrize(
    "after,expected",
    [(450, "中樞性尿崩症"), (120, "腎因性尿崩症")],
)
def test_hypernatremia_zero_baseline_u_osm_does_not_crash(after, expected):
    """U_osm = 0 曾造成除以零 (HTTP 500)，改以絕對值門檻判讀。"""
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 154,
        "volume_status": "euvolemic",
        "u_osm": 0,
        "ddavp_u_osm_after": after,
    }
    r = client.post(HYPER_URL, json=payload)
    assert r.status_code == 200
    assert any(expected in d for d in r.json()["differentials"])


def test_hypernatremia_partial_di_band():
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 150,
        "volume_status": "euvolemic",
        "u_osm": 450,
    }
    body = client.post(HYPER_URL, json=payload).json()
    assert any("部分尿崩症" in d for d in body["differentials"])


def test_hypernatremia_insensible_loss():
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 150,
        "volume_status": "euvolemic",
        "u_osm": 700,
    }
    body = client.post(HYPER_URL, json=payload).json()
    assert any("不顯性水分流失" in d for d in body["differentials"])


@pytest.mark.parametrize(
    "u_osm,expected",
    [(700, "腎外水分流失"), (350, "腎臟流失 / 滲透性利尿")],
)
def test_hypernatremia_hypovolemic_split_by_urine_osm(u_osm, expected):
    payload = {
        "patient": ADULT_MALE,
        "measured_na": 158,
        "volume_status": "hypovolemic",
        "u_osm": u_osm,
    }
    body = client.post(HYPER_URL, json=payload).json()
    assert any(expected in d for d in body["differentials"])


def test_hypernatremia_hypovolemic_without_urine_osm_prompts_for_it():
    payload = {"patient": ADULT_MALE, "measured_na": 158, "volume_status": "hypovolemic"}
    body = client.post(HYPER_URL, json=payload).json()
    assert any("需測量 Urine Osmolality" in d for d in body["differentials"])


def test_hypernatremia_salt_overload():
    payload = {"patient": ADULT_MALE, "measured_na": 158, "volume_status": "hypervolemic"}
    body = client.post(HYPER_URL, json=payload).json()
    assert "鹽分過剩" in body["category"]
    assert any("Loop 利尿劑" in r for r in body["recommendations"])


def test_hypernatremia_alerts_on_cerebral_edema():
    payload = {"patient": ADULT_MALE, "measured_na": 158, "volume_status": "hypervolemic"}
    body = client.post(HYPER_URL, json=payload).json()
    assert any("腦水腫" in a for a in body["safety_alerts"])


@pytest.mark.parametrize("na", [140, 145, 201])
def test_hypernatremia_rejects_out_of_range_sodium(na):
    payload = {"patient": ADULT_MALE, "measured_na": na, "volume_status": "euvolemic"}
    assert client.post(HYPER_URL, json=payload).status_code == 422


# --------------------------------------------------------------------------
# Adrogué-Madias
# --------------------------------------------------------------------------
def test_adrogue_madias_three_percent():
    payload = {
        "patient": ADULT_MALE,
        "current_na": 118,
        "infusate_type": "3% NaCl",
    }
    body = client.post(CALC_URL, json=payload).json()
    # (513 + 0 - 118) / (42 + 1) = 9.19
    assert body["delta_na_per_liter"] == pytest.approx(9.19)
    assert body["infusate_na_meq_l"] == 513.0


def test_adrogue_madias_d5w_lowers_sodium():
    payload = {"patient": ADULT_MALE, "current_na": 160, "infusate_type": "D5W"}
    body = client.post(CALC_URL, json=payload).json()
    # (0 + 0 - 160) / 43 = -3.72
    assert body["delta_na_per_liter"] == pytest.approx(-3.72)


def test_adrogue_madias_ringer_lactate_includes_potassium():
    payload = {"patient": ADULT_MALE, "current_na": 140, "infusate_type": "Lactated Ringer"}
    body = client.post(CALC_URL, json=payload).json()
    # (130 + 4 - 140) / 43 = -0.14
    assert body["delta_na_per_liter"] == pytest.approx(-0.14)


def test_adrogue_madias_custom_infusate():
    payload = {
        "patient": ADULT_MALE,
        "current_na": 120,
        "infusate_type": "custom",
        "custom_infusate_na": 154,
        "custom_infusate_k": 40,
    }
    body = client.post(CALC_URL, json=payload).json()
    # (154 + 40 - 120) / 43 = 1.72
    assert body["delta_na_per_liter"] == pytest.approx(1.72)


def test_adrogue_madias_custom_requires_sodium():
    payload = {"patient": ADULT_MALE, "current_na": 120, "infusate_type": "custom"}
    assert client.post(CALC_URL, json=payload).status_code == 422


def test_adrogue_madias_explicit_null_potassium_is_treated_as_zero():
    """custom_infusate_k=null 曾造成 None 參與運算 (HTTP 500)。"""
    payload = {
        "patient": ADULT_MALE,
        "current_na": 120,
        "infusate_type": "custom",
        "custom_infusate_na": 154,
        "custom_infusate_k": None,
    }
    r = client.post(CALC_URL, json=payload)
    assert r.status_code == 422


def test_adrogue_madias_rejects_invalid_patient():
    payload = {
        "patient": {"age": 45, "gender": "male", "weight_kg": 0},
        "current_na": 120,
        "infusate_type": "D5W",
    }
    assert client.post(CALC_URL, json=payload).status_code == 422


def test_openapi_schema_builds():
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert HYPO_URL in r.json()["paths"]


def test_root_serves_the_web_ui():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "血鈉鑑別決策台" in r.text


def test_engine_script_is_served():
    r = client.get("/sodium-engine.js")
    assert r.status_code == 200
    assert "evaluateHyponatremia" in r.text


def test_static_mount_does_not_shadow_the_api():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/openapi.json").status_code == 200
