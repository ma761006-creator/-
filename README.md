# Clinical Sodium Differential & Management API

以 FastAPI + Pydantic v2 建構的血鈉（Sodium）鑑別診斷與臨床計算 RESTful API。

涵蓋：

- **低血鈉鑑別決策引擎** — 以血漿滲透壓分層（高張／等張／低張），再依體液容積狀態、尿鈉、尿滲透壓與內分泌檢驗結果逐層鑑別。
- **高血鈉鑑別決策引擎** — 依體液容積狀態與尿滲透壓鑑別腎外流失、滲透性利尿與尿崩症；併入 DDAVP 試驗結果可區分 Central DI 與 Nephrogenic DI。
- **自由水缺乏量（Free Water Deficit）** 計算。
- **Adrogué-Madias 輸液公式** — 計算每輸注 1 L 特定輸液的預期血鈉變化。
- **安全警示** — 所有鑑別回應皆附帶校正速率上限提醒（低血鈉 ODS、高血鈉腦水腫）。

> ⚠️ 本 API 僅作為臨床決策**輔助**與教學用途，輸出內容不構成醫療處方。所有治療決策應由具處方權之醫師依病人完整臨床情境判斷。

## 安裝

```bash
pip install -r requirements.txt
```

## 啟動

```bash
uvicorn main:app --reload --port 8000
```

- 網頁介面：<http://127.0.0.1:8000/>
- 互動式文件（Swagger UI）：<http://127.0.0.1:8000/docs>

## 網頁介面

`static/index.html` 是一頁式的操作介面：上方共用病人基本資料（並即時顯示 TBW 與其係數依據），
下方三個分頁分別對應三個端點，輸入即時運算，結果含分類、鑑別診斷、待補檢驗、建議處置與安全警示；
輸液計算另有一條量尺，把每公升的 ΔNa 對照 24 小時 8 mEq/L 的校正上限。

判讀邏輯由 `static/sodium-engine.js` 在瀏覽器端執行，**不呼叫後端**，因此這個頁面也可以直接用
瀏覽器開啟 `static/index.html` 單機使用，數值不離開該裝置、也不寫入任何儲存空間。

這代表同一套臨床邏輯有 Python 與 JavaScript 兩份實作。兩者由
`tests/test_web_parity.py` 對拍把關：同一批輸入（約 1.8 萬組）同時餵給兩邊逐欄位比對，
另外也比對 JS 的輸入邊界表與 OpenAPI schema 的 `Field(...)` 約束、以及輸液濃度表與 enum。
**修改任一邊的閾值、分支或文字時請同步另一邊**，否則測試會失敗。

### 產生 Claude Artifact 版本

```bash
python tools/build_artifact.py artifact-page.html
```

Artifact 發佈時會自行包上 `<!doctype>`/`<html>`/`<head>`/`<body>`，腳本依 `index.html` 裡的
`ARTIFACT:HEAD` / `ARTIFACT:BODY` 標記切出可發佈的片段，發佈時一併帶上 `sodium-engine.js`。

## 端點

| Method | Path | 說明 |
| ------ | ---- | ---- |
| `GET`  | `/health` | 服務健康檢查 |
| `POST` | `/api/v1/differential/hyponatremia` | 低血鈉數據化鑑別診斷 |
| `POST` | `/api/v1/differential/hypernatremia` | 高血鈉鑑別診斷與缺水量評估 |
| `POST` | `/api/v1/calculator/adrogue-madias` | Adrogué-Madias 輸液血鈉變化計算器 |

## 臨床邏輯摘要

### 全身體液量（TBW）係數

| | < 65 歲 | ≥ 65 歲 |
| --- | --- | --- |
| 男性 | 0.6 | 0.5 |
| 女性 | 0.5 | 0.45 |

### 低血鈉分層

1. `P_osm > 295` → 高滲透壓性低血鈉（高血糖、Mannitol、顯影劑）。
2. `275 ≤ P_osm ≤ 295` → 等滲透壓性假性低血鈉（高三酸甘油脂、高丙球蛋白）。
3. `P_osm < 275` → 真性低滲透壓低血鈉，再依體液容積分流：
   - **Hypovolemic**：`U_Na < 20` 腎外流失／`U_Na ≥ 20` 腎臟流失。
   - **Euvolemic**：`U_osm < 100` 水中毒；`U_osm ≥ 100` 且甲狀腺／皮質醇異常 → 內分泌病因；皆正常 → SIADH。
   - **Hypervolemic**：`U_Na < 20` 有效循環血量不足（CHF／肝硬化／腎病症候群）／`U_Na ≥ 20` 腎衰竭。

血糖校正採 Katz 公式：`corrected_Na = measured_Na + 1.6 × (glucose − 100) / 100`。

### 高血鈉分層

- **Hypervolemic** → 鹽分過剩（醫源性高張輸液、原發性醛固酮增多症、Cushing）。
- **Hypovolemic** → `U_osm > 600` 腎外流失；否則滲透性利尿。
- **Euvolemic** → `U_osm > 600` 不顯性流失；`U_osm < 300` 尿崩症（DDAVP 後尿滲透壓上升 ≥ 50% 判為 Central DI，否則 Nephrogenic DI）；300–600 部分 DI。

自由水缺乏量：`FWD = TBW × (Na / 140 − 1)`。

### Adrogué-Madias

```
Δ[Na] = ( [Na]infusate + [K]infusate − [Na]serum ) / ( TBW + 1 )
```

內建輸液濃度（mEq/L Na）：D5W 0、0.45% NaCl 77、0.9% NaCl 154、3% NaCl 513、Lactated Ringer 130（含 K 4）。`custom` 可自訂 Na／K 濃度。

## 請求範例

低血鈉（SIADH 情境）：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/differential/hyponatremia \
  -H 'Content-Type: application/json' \
  -d '{
    "patient": {"age": 72, "gender": "female", "weight_kg": 52},
    "measured_na": 121, "glucose": 110, "posm": 252,
    "volume_status": "euvolemic", "u_na": 56, "u_osm": 410,
    "tsh_normal": true, "cortisol_normal": true
  }'
```

高血鈉（Central DI 情境）：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/differential/hypernatremia \
  -H 'Content-Type: application/json' \
  -d '{
    "patient": {"age": 45, "gender": "male", "weight_kg": 70},
    "measured_na": 154, "volume_status": "euvolemic",
    "u_osm": 150, "ddavp_u_osm_after": 520
  }'
```

回應會計算 `free_water_deficit_l` 約 4.2 L，並標註為中樞性尿崩症。

## 測試

```bash
pip install -r requirements-dev.txt
python -m pytest
```

對拍測試需要 `node`；環境中沒有 node 時該檔會自動跳過（其餘測試照常執行）。

## 輸入邊界（防呆）

| 欄位 | 範圍 |
| --- | --- |
| `age` | 0–130 歲 |
| `weight_kg` | > 0，≤ 300 kg |
| 低血鈉 `measured_na` | 80–134.9 mEq/L |
| 高血鈉 `measured_na` | 145.1–200 mEq/L |
| `glucose` | 10–2500 mg/dL |
| `posm` | 150–400 mOsm/kg |
| `u_na` | 0–300 mEq/L |
| `u_osm` | 0–1500 mOsm/kg |

超出範圍一律回傳 `422 Unprocessable Entity`。
