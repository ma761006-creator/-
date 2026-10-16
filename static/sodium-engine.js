/*
 * sodium-engine.js — 血鈉鑑別決策引擎（瀏覽器 / Node 共用）
 *
 * 本檔是 main.py 中三個端點決策邏輯的逐條對應實作，讓網頁版不需要後端
 * 即可單機運作。兩份實作以 tests/test_web_parity.py 對拍，任何一邊的
 * 閾值、分支或文字改動都會讓測試失敗。修改時請同步兩邊。
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.SodiumEngine = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // 模擬 Python round() 的 half-to-even，讓兩邊輸出字串完全一致
  function pyRound(x, n) {
    var f = Math.pow(10, n || 0);
    var y = x * f;
    var frac = y - Math.trunc(y);
    if (Math.abs(Math.abs(frac) - 0.5) < 1e-9) {
      var t = Math.trunc(y);
      if (t % 2 !== 0) t += Math.sign(y);
      return t / f;
    }
    return Math.round(y) / f;
  }

  // 與 main.py 的 Field(...) 邊界一致；test_web_parity.py 會比對 OpenAPI schema
  var LIMITS = {
    age: { ge: 0, le: 130 },
    weight_kg: { gt: 0, le: 300 },
    hyponatremia_na: { ge: 80, le: 134.9 },
    hypernatremia_na: { ge: 145.1, le: 200 },
    glucose: { ge: 10, le: 2500 },
    posm: { ge: 150, le: 400 },
    u_na: { ge: 0, le: 300 },
    u_osm: { ge: 0, le: 1500 },
    ddavp_u_osm_after: { ge: 0, le: 1500 },
    current_na: { ge: 80, le: 200 },
    custom_infusate_na: { ge: 0, le: 1000 },
    custom_infusate_k: { ge: 0, le: 200 }
  };

  var INFUSATES = {
    "D5W": [0.0, 0.0],
    "0.45% NaCl": [77.0, 0.0],
    "0.9% NaCl": [154.0, 0.0],
    "3% NaCl": [513.0, 0.0],
    "Lactated Ringer": [130.0, 4.0]
  };

  var HYPO_ALERT =
    "慢性低血鈉校正安全速度限制：前 24 小時不應超過 8-10 mEq/L" +
    "（高風險族群如末期肝病、低血鉀、嚴重營養不良者應限制在 <= 6-8 mEq/L），" +
    "以防滲透性去髓鞘症候群 (ODS)。";

  var HYPER_ALERT =
    "慢性高血鈉矯正安全速度限制：降鈉速率每小時以 0.5 mEq/L 為原則，24 小時內下降幅度不可超過 8-10 mEq/L，" +
    "避免水分快速進入腦細胞導致致命性腦水腫及抽搐。";

  function tbwRatio(patient) {
    var elderly = patient.age >= 65;
    if (patient.gender === "male") return elderly ? 0.5 : 0.6;
    return elderly ? 0.45 : 0.5;
  }

  function getTbw(patient) {
    return pyRound(patient.weight_kg * tbwRatio(patient), 2);
  }

  function isNil(v) {
    return v === null || v === undefined || v === "";
  }

  function getInfusateConcentrations(type, customNa, customK) {
    if (type === "custom") return [customNa || 0.0, customK || 0.0];
    return INFUSATES[type];
  }

  // ------------------------------------------------------------------
  // 低血鈉
  // ------------------------------------------------------------------
  function evaluateHyponatremia(req) {
    var correctedNa = req.measured_na;
    if (req.glucose > 100) {
      correctedNa = req.measured_na + (1.6 * (req.glucose - 100)) / 100.0;
    }
    correctedNa = pyRound(correctedNa, 1);

    var differentials = [];
    var recommendations = [];
    var safetyAlerts = [HYPO_ALERT];
    var category;

    if (req.posm > 295) {
      category = "高滲透壓性低血鈉 (Hypertonic Hyponatremia)";
      differentials = [
        "嚴重高血糖 (Hyperglycemia)",
        "使用甘露醇 (Mannitol)",
        "放射線顯影劑滲透效應"
      ];
      recommendations.push("針對高血糖或外源性高張物質處置，校正原發原因即可，不需補高張食鹽水。");
      return {
        measured_na: req.measured_na,
        corrected_na: correctedNa,
        category: category,
        differentials: differentials,
        recommendations: recommendations,
        safety_alerts: safetyAlerts
      };
    }

    if (req.posm >= 275 && req.posm <= 295) {
      category = "等滲透壓性假性低血鈉 (Isotonic / Pseudohyponatremia)";
      differentials = [
        "嚴重高三酸甘油脂血症 (Severe Hypertriglyceridemia)",
        "高丙種球蛋白血症 (如 Multiple Myeloma, 巨球蛋白血症)"
      ];
      recommendations.push(
        "常規間接離子選擇電極受固體成分干擾。建議使用動脈血液氣體分析 (ABG) 的直接電極法進行複檢。"
      );
      return {
        measured_na: req.measured_na,
        corrected_na: correctedNa,
        category: category,
        differentials: differentials,
        recommendations: recommendations,
        safety_alerts: safetyAlerts
      };
    }

    if (req.volume_status === "hypovolemic") {
      category = "低體液容積性低血鈉 (Hypovolemic Hypotonia)";
      if (isNil(req.u_na)) {
        differentials.push("需檢測 Urine Na 以區分腎臟或腎外流失。");
      } else if (req.u_na < 20) {
        differentials.push("腎外流失：腸胃道流失（嘔吐、腹瀉）、第三空間體液蓄積、大量出汗、大面積燒傷。");
        recommendations.push(
          "以等張生理食鹽水 (0.9% NaCl) 恢復有效動脈血容積；容積恢復後 ADH 分泌減少可能引發自發性水利尿，" +
            "需密切監測血鈉防超速。"
        );
      } else {
        differentials.push(
          "腎臟流失：利尿劑使用、大腦鹽分流失症候群 (CSW)、鹽分流失性腎病、" +
            "原發性腎上腺功能不全 (Addison's Disease)。"
        );
        recommendations.push("評估用藥史（利尿劑），檢測皮質醇及腎素/醛固酮。");
      }
    } else if (req.volume_status === "euvolemic") {
      category = "正常體液容積性低血鈉 (Euvolemic Hypotonia)";
      if (isNil(req.u_osm)) {
        differentials.push("需檢測 Urine Osmolality 以確認下視丘-腦下垂體 ADH 軸抑制狀況。");
      } else if (req.u_osm < 100) {
        differentials.push(
          "水中毒 / ADH 適當抑制：心因性多渴症 (Psychogenic Polydipsia)、啤酒暴飲症 (Beer Potomania)、" +
            "極低溶質飲食 (Tea and Toast)。"
        );
        recommendations.push(
          "限制水分攝取；啤酒暴飲者給予溶質（食鹽或蛋白質）後常迅速發生排尿利尿，需高度提防血鈉快速回升。"
        );
      } else if (req.tsh_normal === false || req.cortisol_normal === false) {
        differentials.push(
          "內分泌功能異常：甲狀腺功能低下 (Hypothyroidism) 或 續發性腎上腺皮質功能低下 " +
            "(Secondary Adrenal Insufficiency)。"
        );
        recommendations.push("優先評估並補充荷爾蒙替代療法，避免單純限制水分無效。");
      } else {
        differentials.push("抗利尿激素分泌不當症候群 (SIADH)。");
        recommendations.push(
          "SIADH 為排除性診斷（需腎功能正常、無利尿劑、甲狀腺及皮質醇正常）。" +
            "治療首選限制水分；必要時可使用高鹽飲食或口服尿素。"
        );
      }
    } else {
      category = "高體液容積性低血鈉 (Hypervolemic Hypotonia)";
      if (isNil(req.u_na)) {
        differentials.push("需檢測 Urine Na 以區分器官衰竭之循環不足或原發腎衰竭。");
      } else if (req.u_na < 20) {
        differentials.push(
          "有效循環血量不足：充血性心臟衰竭 (CHF)、肝硬化 (Cirrhosis)、腎病症候群 (Nephrotic Syndrome)。"
        );
        recommendations.push("以限制水分與鹽分攝取為主，合併 Loop 利尿劑治療。");
      } else {
        differentials.push("腎臟排泄功能衰竭：急性腎損傷 (AKI) 或 慢性末期腎病 (ESRD)。");
        recommendations.push("限制水分，評估是否需進行透析治療。");
      }
    }

    return {
      measured_na: req.measured_na,
      corrected_na: correctedNa,
      category: category,
      differentials: differentials,
      recommendations: recommendations,
      safety_alerts: safetyAlerts
    };
  }

  // ------------------------------------------------------------------
  // 高血鈉
  // ------------------------------------------------------------------
  function evaluateHypernatremia(req) {
    var tbw = getTbw(req.patient);
    var fwd = tbw * (req.measured_na / 140.0 - 1.0);
    fwd = pyRound(Math.max(fwd, 0.0), 2);

    var differentials = [];
    var recommendations = [];
    var safetyAlerts = [HYPER_ALERT];
    var category;

    if (req.volume_status === "hypervolemic") {
      category = "高體液容積性高血鈉 (Hypervolemic Hypernatremia - 鹽分過剩)";
      differentials = [
        "醫源性輸注過多高張溶液 (如 3% NaCl, NaHCO3)",
        "原發性醛固酮增多症",
        "庫欣氏症 (Cushing's Syndrome)"
      ];
      recommendations.push(
        "停止外源性含鈉輸液；給予 Loop 利尿劑促進排鈉，並以口服水或 D5W 補充利尿流失之水分。"
      );
    } else if (req.volume_status === "hypovolemic") {
      category = "低體液容積性高血鈉 (Hypovolemic Hypernatremia - 失水大於失鈉)";
      if (isNil(req.u_osm)) {
        differentials.push("需測量 Urine Osmolality 以鑑別水分是否經腎臟流失。");
      } else if (req.u_osm > 600) {
        differentials.push("腎外水分流失：大量出汗、大面積燒傷、滲透性腹瀉。腎臟濃縮功能正常。");
        recommendations.push(
          "若有休克或顯著低血壓，先以等張生理食鹽水 (0.9% NaCl) 復甦血容積；" +
            "血壓穩定後改為低張溶液 (Half-N/S 或 D5W) 校正自由水。"
        );
      } else {
        differentials.push("腎臟流失 / 滲透性利尿：高血糖高滲透壓狀態 (HHS)、高尿素、使用 Loop 利尿劑。");
        recommendations.push("校正血糖與滲透性溶質來源，同步補充水分。");
      }
    } else {
      category = "正常體液容積性高血鈉 (Euvolemic Hypernatremia - 純水分流失)";
      if (isNil(req.u_osm)) {
        differentials.push("需測量 Urine Osmolality 以鑑別尿崩症。");
      } else if (req.u_osm > 600) {
        differentials.push("不顯性水分流失：發燒、過度換氣、呼吸器通氣，或伴隨口渴中樞受損 (Hypodipsia)。");
        recommendations.push("計算自由水缺乏量，以口服飲水或靜脈輸注 D5W 補足。");
      } else if (req.u_osm < 300) {
        if (isNil(req.ddavp_u_osm_after)) {
          differentials.push("尿崩症 (Diabetes Insipidus)：高血鈉下尿液濃縮不全 (U_osm < 300)。");
          recommendations.push("建議安排 DDAVP (Desmopressin) 試驗以鑑別 Central DI 與 Nephrogenic DI。");
        } else if (req.u_osm <= 0) {
          if (req.ddavp_u_osm_after >= 300) {
            differentials.push(
              "中樞性尿崩症 (Central DI)：基礎尿滲透壓無法計算變化率，" +
                "但給予 DDAVP 後尿滲透壓升至 " +
                fmtNum(req.ddavp_u_osm_after) +
                " mOsm/kg。"
            );
            recommendations.push(
              "給予外源性 Desmopressin (DDAVP) 治療，並排查頭部外傷、神經外科手術或鞍部腫瘤。"
            );
          } else {
            differentials.push(
              "腎因性尿崩症 (Nephrogenic DI)：給予 DDAVP 後尿滲透壓僅 " +
                fmtNum(req.ddavp_u_osm_after) +
                " mOsm/kg，反應不足。"
            );
            recommendations.push(
              "排查鋰鹽 (Lithium) 藥物史、高血鈣 (Hypercalcemia)、嚴重低血鉀 (Hypokalemia) 或慢性腎病。"
            );
          }
        } else {
          var ratio = (req.ddavp_u_osm_after - req.u_osm) / req.u_osm;
          if (ratio >= 0.5) {
            differentials.push(
              "中樞性尿崩症 (Central DI)：給予 DDAVP 後尿滲透壓顯著上升 (上升 " +
                fmtNum(pyRound(ratio * 100, 1)) +
                "%)。"
            );
            recommendations.push(
              "給予外源性 Desmopressin (DDAVP) 治療，並排查頭部外傷、神經外科手術或鞍部腫瘤。"
            );
          } else {
            differentials.push(
              "腎因性尿崩症 (Nephrogenic DI)：給予 DDAVP 後反應不足 (上升 " +
                fmtNum(pyRound(ratio * 100, 1)) +
                "%)。"
            );
            recommendations.push(
              "排查鋰鹽 (Lithium) 藥物史、高血鈣 (Hypercalcemia)、嚴重低血鉀 (Hypokalemia) 或慢性腎病。"
            );
          }
        }
      } else {
        differentials.push("部分尿崩症 (Partial DI) 或 輕度滲透性利尿。");
      }
    }

    return {
      measured_na: req.measured_na,
      free_water_deficit_l: fwd,
      category: category,
      differentials: differentials,
      recommendations: recommendations,
      safety_alerts: safetyAlerts
    };
  }

  // ------------------------------------------------------------------
  // Adrogué-Madias
  // ------------------------------------------------------------------
  function calculateAdrogueMadias(req) {
    var tbw = getTbw(req.patient);
    var conc = getInfusateConcentrations(
      req.infusate_type,
      req.custom_infusate_na,
      req.custom_infusate_k
    );
    var naInf = conc[0];
    var kInf = conc[1];

    var deltaNa = pyRound((naInf + kInf - req.current_na) / (tbw + 1.0), 2);

    var note =
      "輸注 1000 mL 之 " +
      req.infusate_type +
      " 預期使血鈉變動 " +
      fmtSigned(deltaNa) +
      " mEq/L。" +
      "臨床實際輸注應每 2~4 小時複查電解質，並隨時依據排尿量調整輸液速率。";

    return {
      tbw_liters: tbw,
      infusate_name: req.infusate_type,
      infusate_na_meq_l: naInf,
      delta_na_per_liter: deltaNa,
      clinical_note: note
    };
  }

  // Python f-string 的 float 顯示：整數值保留 .0
  function fmtNum(x) {
    return Number.isInteger(x) ? x.toFixed(1) : String(x);
  }

  // Python 的 {x:+} 格式
  function fmtSigned(x) {
    return (x >= 0 ? "+" : "") + fmtNum(x);
  }

  return {
    LIMITS: LIMITS,
    INFUSATES: INFUSATES,
    getTbw: getTbw,
    pyRound: pyRound,
    evaluateHyponatremia: evaluateHyponatremia,
    evaluateHypernatremia: evaluateHypernatremia,
    calculateAdrogueMadias: calculateAdrogueMadias
  };
});
