// 접수 보조(content script)
// 원칙: 화면을 '읽고 표시'만 한다. 클릭·입력·새로고침·로그인·제출을 하지 않는다.
//       보안문자·대기열은 감지해 안내만 한다. 입력칸의 값은 읽지 않는다.
(function () {
  "use strict";
  const M = globalThis.ExamMatch;
  const S = globalThis.ExamSites;
  const CONSENT_VERSION = 1;
  const SCAN_MS = 400;

  const siteKey = S.siteForHost(location.hostname);
  if (!siteKey) return;

  let settings = null;
  let site = null;
  let prefs = [];
  let offsetMs = 0; // 서버 시각 - PC 시각
  let lastBestKey = "";
  let notifiedOpen = false;
  let timer = null;

  // ---- 서버 시각 보정: 같은 사이트에 HEAD 1회 ---------------------------------
  async function measureOffset() {
    try {
      const t0 = Date.now();
      const res = await fetch(location.origin + "/", { method: "HEAD", cache: "no-store", credentials: "omit" });
      const t1 = Date.now();
      const d = res.headers.get("Date");
      if (!d) return 0;
      return Date.parse(d) + 500 - (t0 + t1) / 2; // Date 헤더는 초 단위로 잘려 있음
    } catch (_) {
      return 0;
    }
  }

  // ---- 화면 표시(오버레이, Shadow DOM으로 사이트 스타일과 분리) ----------------
  const host = document.createElement("div");
  host.id = "exam-assist-overlay";
  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = `
    <style>
      .box{position:fixed;top:12px;right:12px;z-index:2147483647;width:300px;font:14px/1.45 sans-serif;
           background:#111827;color:#f9fafb;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.35);padding:12px 14px}
      .t{font-weight:700;font-size:13px;opacity:.8}
      .clock{font-size:26px;font-weight:700;margin:4px 0;font-variant-numeric:tabular-nums}
      .msg{margin-top:6px;padding:6px 8px;border-radius:6px;background:#374151;white-space:pre-line}
      .go{background:#dc2626}.ok{background:#15803d}.warn{background:#b45309}
      .small{font-size:12px;opacity:.75;margin-top:6px}
      button{all:unset;cursor:pointer;float:right;opacity:.6}
    </style>
    <div class="box">
      <button id="x" title="숨기기">✕</button>
      <div class="t">시험 접수 보조 · ${site_name()}</div>
      <div class="clock" id="clock">--:--:--</div>
      <div class="msg" id="msg">준비 중…</div>
      <div class="small" id="sub"></div>
      <div class="small">이 도구는 클릭·입력을 하지 않습니다. 버튼은 직접 누르세요.</div>
    </div>`;
  function site_name() {
    return S.SITES[siteKey].name;
  }
  shadow.getElementById("x").addEventListener("click", () => (host.style.display = "none"));

  const style = document.createElement("style");
  style.textContent = `
    [data-eb-rank]{outline-offset:-2px !important}
    [data-eb-rank="best"]{outline:4px solid #16a34a !important;background:rgba(22,163,74,.12) !important}
    [data-eb-rank="match"]{outline:2px dashed #16a34a !important}
    [data-eb-rank="full"]{outline:2px dashed #9ca3af !important;opacity:.6}
    [data-eb-apply]{outline:4px solid #f59e0b !important}`;

  function setMsg(text, cls) {
    const el = shadow.getElementById("msg");
    if (el.textContent !== text) el.textContent = text;
    el.className = "msg " + (cls || "");
  }

  function notify(title, body) {
    try {
      chrome.runtime.sendMessage({ type: "notify", title, body });
    } catch (_) {
      /* 확장 재시작 직후 등 */
    }
  }

  // ---- 화면 읽기 ---------------------------------------------------------------
  const visible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);

  function rowText(tr) {
    // 입력칸 값은 읽지 않는다(innerText는 input 값을 포함하지 않음). 잔여석 칸 숫자는 '잔여 N'으로 보강
    return M.rowTextWithSeats(tr);
  }

  function findBlocker() {
    for (const sel of site.blockers) {
      try {
        const el = document.querySelector(sel);
        if (el && visible(el)) return sel;
      } catch (_) {
        /* 잘못된 선택자 무시 */
      }
    }
    return null;
  }

  function markApplyButtons() {
    const texts = site.applyTexts;
    document.querySelectorAll("[data-eb-apply]").forEach((e) => e.removeAttribute("data-eb-apply"));
    const cands = document.querySelectorAll("a, button, input[type=button], input[type=submit], input[type=image]");
    let n = 0;
    for (const e of cands) {
      const t = (e.innerText || e.value || e.alt || e.title || "").trim();
      if (t && texts.some((k) => t.includes(k)) && visible(e) && !e.disabled) {
        e.setAttribute("data-eb-apply", "");
        n++;
      }
    }
    return n;
  }

  function scanSlots() {
    let rows = [];
    try {
      rows = Array.from(document.querySelectorAll(site.slotRow)).filter(visible);
    } catch (_) {
      return { rows: 0, best: null };
    }
    const texts = rows.map(rowText);
    const ranks = M.rankRows(texts, prefs, site.fullKeywords);
    const best = M.pickSlot(texts, prefs, site.fullKeywords);
    rows.forEach((tr, i) => {
      const want = best && i === best.index ? "best" : ranks[i] > 0 ? "match" : ranks[i] === 0 ? "full" : null;
      if (want) tr.setAttribute("data-eb-rank", want);
      else tr.removeAttribute("data-eb-rank");
    });
    if (best) {
      const key = best.rank + "|" + texts[best.index];
      if (key !== lastBestKey) {
        lastBestKey = key;
        rows[best.index].scrollIntoView({ block: "center", behavior: "instant" });
        notify(`${best.rank}순위 빈자리`, `${M.label(best.pref)} — 초록색 줄의 '선택'을 직접 누르세요`);
      }
    } else {
      lastBestKey = "";
    }
    return { rows: rows.length, matches: ranks.filter((r) => r > 0).length, best, texts };
  }

  // ---- 주기적 갱신 -------------------------------------------------------------
  function fmt(ms) {
    const s = Math.max(0, Math.ceil(ms / 1000));
    const h = String(Math.floor(s / 3600)).padStart(2, "0");
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    return `${h}:${m}:${String(s % 60).padStart(2, "0")}`;
  }

  function tick() {
    const openAt = Date.parse(settings.openAt);
    const remain = openAt - (Date.now() + offsetMs);
    const clock = shadow.getElementById("clock");
    clock.textContent = Number.isFinite(remain) ? (remain > 0 ? fmt(remain) : "접수 시작") : "시각 미설정";

    const blocker = findBlocker();
    const slots = scanSlots();
    const applyCount = markApplyButtons();

    if (remain <= 0 && !notifiedOpen && Number.isFinite(remain)) {
      notifiedOpen = true;
      notify("접수 시작", "지금 접수 화면을 새로고침하고 '원서접수'를 직접 누르세요");
    }

    if (blocker) setMsg("보안문자·대기열 화면입니다. 직접 처리해 주세요.", "warn");
    else if (slots.best) setMsg(`${slots.best.rank}순위 빈자리: ${M.label(slots.best.pref)}\n→ 초록색 줄의 '선택'을 누르세요`, "ok");
    else if (slots.matches === 0 && slots.rows > 0 && prefs.length && slots.texts.some((t) => prefs.some((p) => M.rowMatches(t, p))))
      setMsg("희망 조건은 모두 마감입니다. 목록을 다시 조회해 취소 자리를 확인하세요.", "warn");
    else if (remain > 0) setMsg(remain < 60000 ? "곧 시작합니다. 접수 화면에서 대기하세요." : "접수 시작을 기다리는 중", remain < 60000 ? "go" : "");
    else if (applyCount) setMsg("주황색 버튼(원서접수)을 직접 누르세요.", "go");
    else setMsg("접수 화면으로 이동하거나 새로고침(F5)하세요.", "go");

    shadow.getElementById("sub").textContent =
      `희망 ${prefs.length}개 · 일정 ${slots.rows}행 · 서버 보정 ${(offsetMs / 1000).toFixed(1)}초`;
  }

  async function start() {
    const data = await chrome.storage.local.get("settings");
    settings = data.settings;
    if (!settings || !settings.enabled) return;
    if (!settings.consent || settings.consent.version !== CONSENT_VERSION) return; // 동의 전에는 동작 안 함
    site = { ...S.SITES[siteKey], ...((settings.overrides || {})[siteKey] || {}) };
    prefs = M.parsePrefs(settings.prefsText);
    (document.head || document.documentElement).appendChild(style);
    document.documentElement.appendChild(host);
    offsetMs = await measureOffset();
    tick();
    timer = setInterval(tick, SCAN_MS);
  }

  chrome.storage.onChanged.addListener((changes) => {
    if (!changes.settings) return;
    if (timer) clearInterval(timer);
    timer = null;
    host.remove();
    style.remove();
    lastBestKey = "";
    start();
  });

  start();
})();
