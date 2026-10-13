// 시험 일정 행(텍스트)과 희망 조건 비교 — Python exam_booker/matching.py와 같은 규칙.
// 브라우저(content script)와 node(테스트) 양쪽에서 쓴다.
(function (root) {
  "use strict";

  const WS = /\s+/g;

  // 공백·날짜 구분자 차이 제거: '2026. 11. 21' == '2026-11-21' == '11월 21일'(부분)
  function normalize(text) {
    let t = String(text || "").replace(WS, "");
    t = t.replace(/(\d)[./년-](?=\d)/g, "$1-");
    t = t.replace(/(\d)월(?=\d)/g, "$1-");
    return t.replace(/일/g, "").toLowerCase();
  }

  function keywords(pref) {
    return [pref.venue, pref.date, pref.time].map((k) => String(k || "").trim()).filter(Boolean);
  }

  function label(pref) {
    return keywords(pref).join(" / ") || "(조건 없음 — 아무 자리나)";
  }

  function rowMatches(rowText, pref) {
    const norm = normalize(rowText);
    return keywords(pref).every((k) => norm.includes(normalize(k)));
  }

  function isFull(rowText, fullKeywords) {
    const norm = normalize(rowText);
    if ((fullKeywords || []).some((k) => k && norm.includes(normalize(k)))) return true;
    // '잔여 0', '0석', '0/30' (날짜 변환 전 원문 기준)
    const compact = String(rowText || "").replace(WS, "");
    return /잔여:?0(?!\d)|(?<!\d)0석|(?<![\d.])0\/\d+/.test(compact);
  }

  // 우선순위가 가장 높은 조건에 맞고 자리가 남은 첫 행 → {index, rank, pref} | null
  function pickSlot(rows, prefs, fullKeywords) {
    for (let r = 0; r < prefs.length; r++) {
      for (let i = 0; i < rows.length; i++) {
        if (rowMatches(rows[i], prefs[r]) && !isFull(rows[i], fullKeywords)) {
          return { index: i, rank: r + 1, pref: prefs[r] };
        }
      }
    }
    return null;
  }

  // 각 행의 순위(맞는 조건 중 가장 높은 순위, 마감이면 0, 해당 없음이면 -1)
  function rankRows(rows, prefs, fullKeywords) {
    return rows.map((text) => {
      const r = prefs.findIndex((p) => rowMatches(text, p));
      if (r < 0) return -1;
      return isFull(text, fullKeywords) ? 0 : r + 1;
    });
  }

  // "시험장 | 날짜 | 시간" 줄 목록 → 조건 배열
  function parsePrefs(text) {
    return String(text || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#"))
      .map((line) => {
        const [venue = "", date = "", time = ""] = line.split("|").map((s) => s.trim());
        return { venue, date, time };
      });
  }

  // [브라우저 전용] 표 머리글에서 '잔여·여석·남은' 칸을 찾아 숫자만 적힌 칸을 '잔여 N'으로 풀어 쓴다.
  // 예: 잔여석 칸에 '0'만 있으면 행 텍스트만으로는 마감을 알 수 없기 때문.
  const SEAT_HEADER = /잔여|여석|남은/;
  const seatColCache = new WeakMap();

  function seatColumn(table) {
    if (!table) return -1;
    if (seatColCache.has(table)) return seatColCache.get(table);
    const ths = Array.from(table.querySelectorAll("thead th, thead td, tr:first-child th"));
    const th = ths.find((h) => SEAT_HEADER.test(h.textContent || ""));
    const idx = th ? th.cellIndex : -1;
    seatColCache.set(table, idx);
    return idx;
  }

  function rowTextWithSeats(tr) {
    const base = (tr.innerText || tr.textContent || "").replace(WS, " ").trim();
    const idx = seatColumn(tr.closest("table"));
    const cell = idx >= 0 ? tr.cells[idx] : null;
    if (!cell) return base;
    const m = (cell.innerText || cell.textContent || "").trim().match(/^(\d+)/);
    return m ? `${base} 잔여 ${Number(m[1])}` : base;
  }

  const api = { normalize, keywords, label, rowMatches, isFull, pickSlot, rankRows, parsePrefs, rowTextWithSeats };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ExamMatch = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
