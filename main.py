from enum import Enum
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

app = FastAPI(
    title="Clinical Sodium Differential & Management API",
    description="提供低血鈉與高血鈉的數據化鑑別診斷決策支援、體液計算與校正速率安全警示。",
    version="1.0.0",
)

# --------------------------------------------------------------------------
# 列舉型別 (Enums)
# --------------------------------------------------------------------------
class GenderEnum(str, Enum):
    MALE = "male"
    FEMALE = "female"


class VolumeStatusEnum(str, Enum):
    HYPOVOLEMIC = "hypovolemic"    # 體液容積偏低（脫水、低血壓、心跳快）
    EUVOLEMIC = "euvolemic"        # 體液容積正常（無脫水、無水腫）
    HYPERVOLEMIC = "hypervolemic"  # 體液容積過多（水腫、腹水、JVD）


class InfusateTypeEnum(str, Enum):
    D5W = "D5W"                        # 0 mEq/L Na
    HALF_NS = "0.45% NaCl"             # 77 mEq/L Na
    NS = "0.9% NaCl"                   # 154 mEq/L Na
    THREE_PERCENT = "3% NaCl"          # 513 mEq/L Na
    RINGER_LACTATE = "Lactated Ringer"  # 130 mEq/L Na
    CUSTOM = "custom"                  # 自訂濃度


# --------------------------------------------------------------------------
# 資料結構定義 (Pydantic Schemas)
# --------------------------------------------------------------------------
class PatientProfile(BaseModel):
    age: int = Field(..., ge=0, le=130, description="病人年齡（歲）", examples=[72])
    gender: GenderEnum = Field(..., description="生理性別", examples=["female"])
    weight_kg: float = Field(..., gt=0, le=300, description="病人體重 (kg)", examples=[55.0])

    def get_tbw_ratio(self) -> float:
        is_elderly = self.age >= 65
        if self.gender == GenderEnum.MALE:
            return 0.5 if is_elderly else 0.6
        return 0.45 if is_elderly else 0.5

    def get_tbw(self) -> float:
        return round(self.weight_kg * self.get_tbw_ratio(), 2)


# --- 低血鈉請求與回應 ---
class HyponatremiaRequest(BaseModel):
    patient: PatientProfile
    measured_na: float = Field(..., ge=80, le=134.9, description="測量血鈉值 (mEq/L)", examples=[122.0])
    glucose: float = Field(..., ge=10, le=2500, description="血糖 (mg/dL)", examples=[180.0])
    posm: float = Field(..., ge=150, le=400, description="實測血漿滲透壓 (mOsm/kg)", examples=[258.0])
    volume_status: VolumeStatusEnum = Field(..., description="臨床細胞外液狀態 (ECF)", examples=["euvolemic"])
    u_na: Optional[float] = Field(None, ge=0, le=300, description="尿鈉濃度 (mEq/L)", examples=[48.0])
    u_osm: Optional[float] = Field(None, ge=0, le=1500, description="尿滲透壓 (mOsm/kg)", examples=[420.0])
    tsh_normal: bool = Field(True, description="甲狀腺功能 (TSH/Free T4) 是否正常", examples=[True])
    cortisol_normal: bool = Field(True, description="腎上腺皮質功能 (Morning Cortisol) 是否正常", examples=[True])


class HyponatremiaResponse(BaseModel):
    measured_na: float
    corrected_na: float
    category: str
    differentials: List[str]
    recommendations: List[str]
    safety_alerts: List[str]


# --- 高血鈉請求與回應 ---
class HypernatremiaRequest(BaseModel):
    patient: PatientProfile
    measured_na: float = Field(..., ge=145.1, le=200, description="測量血鈉值 (mEq/L)", examples=[156.0])
    volume_status: VolumeStatusEnum = Field(..., description="臨床細胞外液狀態 (ECF)", examples=["euvolemic"])
    u_osm: Optional[float] = Field(None, ge=0, le=1500, description="尿滲透壓 (mOsm/kg)", examples=[180.0])
    ddavp_u_osm_after: Optional[float] = Field(
        None, ge=0, le=1500, description="給予 DDAVP 後之尿滲透壓 (mOsm/kg)", examples=[450.0]
    )


class HypernatremiaResponse(BaseModel):
    measured_na: float
    free_water_deficit_l: float
    category: str
    differentials: List[str]
    recommendations: List[str]
    safety_alerts: List[str]


# --- Adrogué-Madias 輸液計算請求與回應 ---
class InfusionCalculationRequest(BaseModel):
    patient: PatientProfile
    current_na: float = Field(..., ge=80, le=200, description="當前血鈉 (mEq/L)", examples=[118.0])
    infusate_type: InfusateTypeEnum = Field(..., description="輸液種類", examples=["3% NaCl"])
    custom_infusate_na: Optional[float] = Field(
        None, ge=0, le=1000, description="若為自訂輸液，鈉離子濃度 (mEq/L)"
    )
    custom_infusate_k: float = Field(
        0.0, ge=0, le=200, description="輸液中鉀離子濃度 (mEq/L)"
    )

    @model_validator(mode="after")
    def check_custom_infusate(self):
        if self.infusate_type == InfusateTypeEnum.CUSTOM and self.custom_infusate_na is None:
            raise ValueError("當輸液類型為 custom 時，必須指定 custom_infusate_na")
        return self


class InfusionCalculationResponse(BaseModel):
    tbw_liters: float
    infusate_name: str
    infusate_na_meq_l: float
    delta_na_per_liter: float
    clinical_note: str


# --------------------------------------------------------------------------
# 輔助計算函數
# --------------------------------------------------------------------------
def get_infusate_concentrations(
    infusate: InfusateTypeEnum, custom_na: Optional[float], custom_k: Optional[float]
):
    lookup = {
        InfusateTypeEnum.D5W: (0.0, 0.0),
        InfusateTypeEnum.HALF_NS: (77.0, 0.0),
        InfusateTypeEnum.NS: (154.0, 0.0),
        InfusateTypeEnum.THREE_PERCENT: (513.0, 0.0),
        InfusateTypeEnum.RINGER_LACTATE: (130.0, 4.0),
    }
    if infusate == InfusateTypeEnum.CUSTOM:
        return (custom_na or 0.0, custom_k or 0.0)
    return lookup[infusate]


# --------------------------------------------------------------------------
# API 端點 (Endpoints)
# --------------------------------------------------------------------------
@app.get("/health", summary="服務健康檢查", tags=["System"])
def health_check():
    return {"status": "ok", "service": app.title, "version": app.version}


@app.post(
    "/api/v1/differential/hyponatremia",
    response_model=HyponatremiaResponse,
    summary="低血鈉數據化鑑別診斷",
    tags=["Differential Diagnosis"],
)
def evaluate_hyponatremia(req: HyponatremiaRequest):
    """
    依據血漿滲透壓、血糖校正值、體液容積狀態（Volume Status）、尿鈉（U_Na）與尿滲透壓（U_osm）進行階層式鑑別診斷。
    """
    # 血糖校正血鈉 (Katz formula)
    corrected_na = req.measured_na
    if req.glucose > 100:
        corrected_na = req.measured_na + (1.6 * (req.glucose - 100) / 100.0)
    corrected_na = round(corrected_na, 1)

    differentials: List[str] = []
    recommendations: List[str] = []
    safety_alerts: List[str] = [
        "慢性低血鈉校正安全速度限制：前 24 小時不應超過 8-10 mEq/L"
        "（高風險族群如末期肝病、低血鉀、嚴重營養不良者應限制在 <= 6-8 mEq/L），"
        "以防滲透性去髓鞘症候群 (ODS)。"
    ]

    # 1. 滲透壓分類
    if req.posm > 295:
        category = "高滲透壓性低血鈉 (Hypertonic Hyponatremia)"
        differentials = ["嚴重高血糖 (Hyperglycemia)", "使用甘露醇 (Mannitol)", "放射線顯影劑滲透效應"]
        recommendations.append("針對高血糖或外源性高張物質處置，校正原發原因即可，不需補高張食鹽水。")
        return HyponatremiaResponse(
            measured_na=req.measured_na,
            corrected_na=corrected_na,
            category=category,
            differentials=differentials,
            recommendations=recommendations,
            safety_alerts=safety_alerts,
        )

    if 275 <= req.posm <= 295:
        category = "等滲透壓性假性低血鈉 (Isotonic / Pseudohyponatremia)"
        differentials = [
            "嚴重高三酸甘油脂血症 (Severe Hypertriglyceridemia)",
            "高丙種球蛋白血症 (如 Multiple Myeloma, 巨球蛋白血症)",
        ]
        recommendations.append(
            "常規間接離子選擇電極受固體成分干擾。建議使用動脈血液氣體分析 (ABG) 的直接電極法進行複檢。"
        )
        return HyponatremiaResponse(
            measured_na=req.measured_na,
            corrected_na=corrected_na,
            category=category,
            differentials=differentials,
            recommendations=recommendations,
            safety_alerts=safety_alerts,
        )

    # 2. 低滲透壓真實低血鈉 (P_osm < 275)
    if req.volume_status == VolumeStatusEnum.HYPOVOLEMIC:
        category = "低體液容積性低血鈉 (Hypovolemic Hypotonia)"
        if req.u_na is None:
            differentials.append("需檢測 Urine Na 以區分腎臟或腎外流失。")
        elif req.u_na < 20:
            differentials.append("腎外流失：腸胃道流失（嘔吐、腹瀉）、第三空間體液蓄積、大量出汗、大面積燒傷。")
            recommendations.append(
                "以等張生理食鹽水 (0.9% NaCl) 恢復有效動脈血容積；容積恢復後 ADH 分泌減少可能引發自發性水利尿，"
                "需密切監測血鈉防超速。"
            )
        else:
            differentials.append(
                "腎臟流失：利尿劑使用、大腦鹽分流失症候群 (CSW)、鹽分流失性腎病、"
                "原發性腎上腺功能不全 (Addison's Disease)。"
            )
            recommendations.append("評估用藥史（利尿劑），檢測皮質醇及腎素/醛固酮。")

    elif req.volume_status == VolumeStatusEnum.EUVOLEMIC:
        category = "正常體液容積性低血鈉 (Euvolemic Hypotonia)"
        if req.u_osm is None:
            differentials.append("需檢測 Urine Osmolality 以確認下視丘-腦下垂體 ADH 軸抑制狀況。")
        elif req.u_osm < 100:
            differentials.append(
                "水中毒 / ADH 適當抑制：心因性多渴症 (Psychogenic Polydipsia)、啤酒暴飲症 (Beer Potomania)、"
                "極低溶質飲食 (Tea and Toast)。"
            )
            recommendations.append(
                "限制水分攝取；啤酒暴飲者給予溶質（食鹽或蛋白質）後常迅速發生排尿利尿，需高度提防血鈉快速回升。"
            )
        else:
            # U_osm >= 100
            if not req.tsh_normal or not req.cortisol_normal:
                differentials.append(
                    "內分泌功能異常：甲狀腺功能低下 (Hypothyroidism) 或 續發性腎上腺皮質功能低下 "
                    "(Secondary Adrenal Insufficiency)。"
                )
                recommendations.append("優先評估並補充荷爾蒙替代療法，避免單純限制水分無效。")
            else:
                differentials.append("抗利尿激素分泌不當症候群 (SIADH)。")
                recommendations.append(
                    "SIADH 為排除性診斷（需腎功能正常、無利尿劑、甲狀腺及皮質醇正常）。"
                    "治療首選限制水分；必要時可使用高鹽飲食或口服尿素。"
                )

    else:  # VolumeStatusEnum.HYPERVOLEMIC
        category = "高體液容積性低血鈉 (Hypervolemic Hypotonia)"
        if req.u_na is None:
            differentials.append("需檢測 Urine Na 以區分器官衰竭之循環不足或原發腎衰竭。")
        elif req.u_na < 20:
            differentials.append(
                "有效循環血量不足：充血性心臟衰竭 (CHF)、肝硬化 (Cirrhosis)、腎病症候群 (Nephrotic Syndrome)。"
            )
            recommendations.append("以限制水分與鹽分攝取為主，合併 Loop 利尿劑治療。")
        else:
            differentials.append("腎臟排泄功能衰竭：急性腎損傷 (AKI) 或 慢性末期腎病 (ESRD)。")
            recommendations.append("限制水分，評估是否需進行透析治療。")

    return HyponatremiaResponse(
        measured_na=req.measured_na,
        corrected_na=corrected_na,
        category=category,
        differentials=differentials,
        recommendations=recommendations,
        safety_alerts=safety_alerts,
    )


@app.post(
    "/api/v1/differential/hypernatremia",
    response_model=HypernatremiaResponse,
    summary="高血鈉鑑別診斷與缺水量評估",
    tags=["Differential Diagnosis"],
)
def evaluate_hypernatremia(req: HypernatremiaRequest):
    """
    評估高血鈉（Na > 145 mEq/L）、計算自由水缺乏量（FWD），並依體液狀態及尿滲透壓鑑別尿崩症（DI）。
    """
    tbw = req.patient.get_tbw()
    # 自由水缺乏量計算：FWD = TBW * (Na / 140 - 1)
    fwd = tbw * ((req.measured_na / 140.0) - 1.0)
    fwd = round(max(fwd, 0.0), 2)

    differentials: List[str] = []
    recommendations: List[str] = []
    safety_alerts: List[str] = [
        "慢性高血鈉矯正安全速度限制：降鈉速率每小時以 0.5 mEq/L 為原則，24 小時內下降幅度不可超過 8-10 mEq/L，"
        "避免水分快速進入腦細胞導致致命性腦水腫及抽搐。"
    ]

    if req.volume_status == VolumeStatusEnum.HYPERVOLEMIC:
        category = "高體液容積性高血鈉 (Hypervolemic Hypernatremia - 鹽分過剩)"
        differentials = [
            "醫源性輸注過多高張溶液 (如 3% NaCl, NaHCO3)",
            "原發性醛固酮增多症",
            "庫欣氏症 (Cushing's Syndrome)",
        ]
        recommendations.append(
            "停止外源性含鈉輸液；給予 Loop 利尿劑促進排鈉，並以口服水或 D5W 補充利尿流失之水分。"
        )

    elif req.volume_status == VolumeStatusEnum.HYPOVOLEMIC:
        category = "低體液容積性高血鈉 (Hypovolemic Hypernatremia - 失水大於失鈉)"
        if req.u_osm is None:
            differentials.append("需測量 Urine Osmolality 以鑑別水分是否經腎臟流失。")
        elif req.u_osm > 600:
            differentials.append("腎外水分流失：大量出汗、大面積燒傷、滲透性腹瀉。腎臟濃縮功能正常。")
            recommendations.append(
                "若有休克或顯著低血壓，先以等張生理食鹽水 (0.9% NaCl) 復甦血容積；"
                "血壓穩定後改為低張溶液 (Half-N/S 或 D5W) 校正自由水。"
            )
        else:
            differentials.append(
                "腎臟流失 / 滲透性利尿：高血糖高滲透壓狀態 (HHS)、高尿素、使用 Loop 利尿劑。"
            )
            recommendations.append("校正血糖與滲透性溶質來源，同步補充水分。")

    else:  # VolumeStatusEnum.EUVOLEMIC
        category = "正常體液容積性高血鈉 (Euvolemic Hypernatremia - 純水分流失)"
        if req.u_osm is None:
            differentials.append("需測量 Urine Osmolality 以鑑別尿崩症。")
        elif req.u_osm > 600:
            differentials.append(
                "不顯性水分流失：發燒、過度換氣、呼吸器通氣，或伴隨口渴中樞受損 (Hypodipsia)。"
            )
            recommendations.append("計算自由水缺乏量，以口服飲水或靜脈輸注 D5W 補足。")
        elif req.u_osm < 300:
            # 高血鈉狀態下 U_osm 未濃縮，確立為尿崩症
            if req.ddavp_u_osm_after is None:
                differentials.append("尿崩症 (Diabetes Insipidus)：高血鈉下尿液濃縮不全 (U_osm < 300)。")
                recommendations.append(
                    "建議安排 DDAVP (Desmopressin) 試驗以鑑別 Central DI 與 Nephrogenic DI。"
                )
            elif req.u_osm <= 0:
                # 無尿或基礎尿滲透壓為 0 時無法計算上升百分比，改以絕對值判讀
                if req.ddavp_u_osm_after >= 300:
                    differentials.append(
                        f"中樞性尿崩症 (Central DI)：基礎尿滲透壓無法計算變化率，"
                        f"但給予 DDAVP 後尿滲透壓升至 {req.ddavp_u_osm_after} mOsm/kg。"
                    )
                    recommendations.append(
                        "給予外源性 Desmopressin (DDAVP) 治療，並排查頭部外傷、神經外科手術或鞍部腫瘤。"
                    )
                else:
                    differentials.append(
                        f"腎因性尿崩症 (Nephrogenic DI)：給予 DDAVP 後尿滲透壓僅 "
                        f"{req.ddavp_u_osm_after} mOsm/kg，反應不足。"
                    )
                    recommendations.append(
                        "排查鋰鹽 (Lithium) 藥物史、高血鈣 (Hypercalcemia)、嚴重低血鉀 (Hypokalemia) 或慢性腎病。"
                    )
            else:
                # 計算 DDAVP 後之上升幅度
                ratio = (req.ddavp_u_osm_after - req.u_osm) / req.u_osm
                if ratio >= 0.5:
                    differentials.append(
                        f"中樞性尿崩症 (Central DI)：給予 DDAVP 後尿滲透壓顯著上升 (上升 {round(ratio * 100, 1)}%)。"
                    )
                    recommendations.append(
                        "給予外源性 Desmopressin (DDAVP) 治療，並排查頭部外傷、神經外科手術或鞍部腫瘤。"
                    )
                else:
                    differentials.append(
                        f"腎因性尿崩症 (Nephrogenic DI)：給予 DDAVP 後反應不足 (上升 {round(ratio * 100, 1)}%)。"
                    )
                    recommendations.append(
                        "排查鋰鹽 (Lithium) 藥物史、高血鈣 (Hypercalcemia)、嚴重低血鉀 (Hypokalemia) 或慢性腎病。"
                    )
        else:
            differentials.append("部分尿崩症 (Partial DI) 或 輕度滲透性利尿。")

    return HypernatremiaResponse(
        measured_na=req.measured_na,
        free_water_deficit_l=fwd,
        category=category,
        differentials=differentials,
        recommendations=recommendations,
        safety_alerts=safety_alerts,
    )


@app.post(
    "/api/v1/calculator/adrogue-madias",
    response_model=InfusionCalculationResponse,
    summary="Adrogué-Madias 輸液血鈉變化計算器",
    tags=["Clinical Calculations"],
)
def calculate_adrogue_madias(req: InfusionCalculationRequest):
    """
    計算每輸入 1 公升特定靜脈輸液後，病人血鈉的預期變化值 (Delta Na)。
    公式：Δ[Na] = ( [Na]infusate + [K]infusate - [Na]serum ) / ( TBW + 1 )
    """
    tbw = req.patient.get_tbw()
    na_inf, k_inf = get_infusate_concentrations(
        req.infusate_type, req.custom_infusate_na, req.custom_infusate_k
    )

    delta_na = (na_inf + k_inf - req.current_na) / (tbw + 1.0)
    delta_na = round(delta_na, 2)

    note = (
        f"輸注 1000 mL 之 {req.infusate_type.value} 預期使血鈉變動 {delta_na:+} mEq/L。"
        "臨床實際輸注應每 2~4 小時複查電解質，並隨時依據排尿量調整輸液速率。"
    )

    return InfusionCalculationResponse(
        tbw_liters=tbw,
        infusate_name=req.infusate_type.value,
        infusate_na_meq_l=na_inf,
        delta_na_per_liter=delta_na,
        clinical_note=note,
    )


# --------------------------------------------------------------------------
# 網頁介面
# --------------------------------------------------------------------------
# 掛載在最後，讓上面宣告的 API 路由與 /docs 優先比對；其餘路徑才交給靜態檔案。
# 網頁本身以 static/sodium-engine.js 在瀏覽器端運算，不需呼叫下列端點，
# 兩份實作由 tests/test_web_parity.py 對拍確保一致。
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="web")
