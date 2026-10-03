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

互動式文件（Swagger UI）：<http://127.0.0.1:8000/docs>

## PDF 轉 Markdown 工具（`pdf2md`）

附帶一個獨立的 PDF → Markdown 轉換器（以 PyMuPDF 讀取版面），可把指引、衛教單張、論文等 PDF
轉成可編輯、可放進筆記或交給 LLM 的 Markdown。

```bash
python -m pdf2md 指引.pdf                    # 輸出 指引.md（與 PDF 同資料夾）
python -m pdf2md 指引.pdf -o out.md --images # 另存圖片到 out_images/ 並插入連結
python -m pdf2md *.pdf -o markdown/          # 批次轉換到資料夾
python -m pdf2md 指引.pdf -p 1-3,5 -o -      # 只轉指定頁，印到標準輸出
```

| 參數 | 說明 |
| --- | --- |
| `-o, --output` | 輸出檔；多個輸入時為資料夾；`-` 印到標準輸出 |
| `-p, --pages` | 頁碼範圍，如 `1-3,5`、`4-` |
| `--images` | 另存圖片並插入 `![](...)` 連結 |
| `--page-breaks` | 每頁開頭插入 `<!-- page N -->` |
| `--keep-headers` | 保留頁首、頁尾與頁碼（預設移除） |
| `--no-tables` | 不偵測表格 |
| `--password` | 加密 PDF 的密碼 |

也可在程式中使用：

```python
from pdf2md import convert

result = convert("指引.pdf", pages="1-3")
print(result.markdown, result.warnings)
```

會還原的結構：

- **標題**：字級較大者依大小分級；與內文同字級、但用獨特字型的短行也視為標題（期刊常這樣排，
  字型名稱還常被混淆成 `AdvTT3e3c8cd7`），依首次出現順序往下排，並從同一區塊的段落中切出來。
- **段落**：跨行、跨區塊、跨頁接回（中文不補空格、英文斷字接回、`evidence-to-` 這類複合詞保留連字號）。
- **清單**：項目符號與巢狀清單、數字編號。
- **表格**：有框線的表格；以及「TABLE n」標題下、只靠對齊排版的無框線表格（依欄位位置與懸掛縮排重組，
  橫印旋轉 90° 的表格也可）。排版用的外框（如摘要側欄）不會被當成表格。
- **圖**：`--images` 時另存點陣圖；「FIGURE n」上方以向量繪製的流程圖、統計圖會整塊轉成 PNG，
  圖內散落的文字標籤不再混進內文。
- 等寬字型的程式碼、粗體／斜體；重複出現的頁首頁尾與頁碼會被移除。

### 網頁版（不需安裝、檔案不上傳）

`pdf2md/web/index.html` 用 pdf.js 在瀏覽器裡轉檔，PDF 不會離開該裝置，也沒有檔案大小限制；
直接用瀏覽器開啟即可。轉換規則由 `pdf2md/web/pdf2md-core.js` 移植自 Python 版，
`tests/test_pdf2md_web.py` 以同一批 PDF 對拍兩邊的標題、段落、清單與無框線表格
（需要 node：`npm install --prefix pdf2md/web`，缺少時該檔自動跳過）。
網頁版不做有框線表格與圖片擷取。

在 Claude 上發佈用的檔案由 `node pdf2md/web/build_artifact.js` 產生到 `pdf2md/web/dist/`
（CMap 會打包成 `cmaps.js`，因為發佈平台不提供二進位檔）。

限制：

- **掃描檔**沒有文字層，無法擷取，會出現「需先做 OCR」的警告。
- **沒有「TABLE n」標題的無框線表格**仍以一般文字輸出。
- **多欄排版**（如期刊雙欄內文）依由上而下、由左而右排序，兩欄段落可能交錯。
- 數學公式以 PDF 中的字元原樣輸出，不轉成 LaTeX。

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
