"use strict";

/* ReadHorizon 웹 화면 — 원문 | 분석 | 질의응답 3단 뷰어 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  workspace: null,
  health: null,
  meta: null,
  works: [],
  current: null, // { work, paragraphs, analyses: Map, questions }
  kind: "overview",
  citeRegistry: new Map(),
  citeSeq: 0,
  bibSelected: null,
  ocr: null, // 마지막 텍스트 추출 결과(확인 전에는 저장하지 않음)
  profile: { audience: "", lang: "ko" }, // 읽는 사람 설정(이 브라우저에만 기억)
};

/* ── 자료실 ID: 브라우저가 만든 무작위 값(계정 정보 없음) ── */
function randomId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function loadWorkspace() {
  try {
    const saved = localStorage.getItem("rh.workspace");
    if (saved) return saved;
    const created = randomId();
    localStorage.setItem("rh.workspace", created);
    return created;
  } catch (_) {
    return randomId(); // 저장소를 쓸 수 없으면 이번 방문 동안만 유지
  }
}

/* ── 읽는 사람 설정: 독자 수준·설명 언어(이 브라우저에만 기억, 서버에는 요청할 때만 보냄) ── */
function loadProfile() {
  try {
    const saved = JSON.parse(localStorage.getItem("rh.profile") || "{}");
    return {
      audience: typeof saved.audience === "string" ? saved.audience : "",
      lang: typeof saved.lang === "string" ? saved.lang : "ko",
    };
  } catch (_) {
    return { audience: "", lang: "ko" };
  }
}

function saveProfile() {
  try { localStorage.setItem("rh.profile", JSON.stringify(state.profile)); } catch (_) { /* 저장소 없음 */ }
}

// 서버의 ReaderProfile.suffix와 같은 규칙(저장된 결과를 설정별로 구분)
function profileSuffix() {
  const { audience, lang } = state.profile;
  return !audience && lang === "ko" ? "" : `@${audience || "-"}.${lang}`;
}

function renderProfileControls() {
  const audience = $("#audience");
  const lang = $("#explain-lang");
  if (!audience.options.length) {
    audience.add(new Option("지정 안 함", ""));
    for (const [value, label] of Object.entries(state.meta.audiences || {})) audience.add(new Option(label, value));
    for (const [value, label] of Object.entries(state.meta.explain_languages || {})) lang.add(new Option(label, value));
  }
  if (!Array.from(audience.options).some((o) => o.value === state.profile.audience)) state.profile.audience = "";
  if (!Array.from(lang.options).some((o) => o.value === state.profile.lang)) state.profile.lang = "ko";
  audience.value = state.profile.audience;
  lang.value = state.profile.lang;
}

function onProfileChange() {
  state.profile = { audience: $("#audience").value, lang: $("#explain-lang").value };
  saveProfile();
  if (state.current) renderAnalysis();
}

function profileLabel(p) {
  if (!p || (!p.audience && (!p.lang || p.lang === "ko"))) return "";
  const audience = p.audience ? (state.meta.audiences || {})[p.audience] || p.audience : "지정 안 함";
  const lang = (state.meta.explain_languages || {})[p.lang] || p.lang;
  return `읽는 사람 ${audience} · 설명 ${lang}`;
}

/* ── 공통 ── */
function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

let toastTimer = null;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4500);
}

async function api(path, options = {}) {
  const headers = { "X-Workspace-Id": state.workspace, ...(options.headers || {}) };
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204) return null;
  let data = null;
  try { data = await response.json(); } catch (_) { /* 본문 없음 */ }
  if (!response.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string" ? detail
      : Array.isArray(detail) ? "입력값을 확인해 주세요." : `요청에 실패했습니다(${response.status}).`;
    throw new Error(message);
  }
  return data;
}

function formatDate(iso) {
  try { return new Date(iso).toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" }); }
  catch (_) { return iso; }
}

function genreLabel(categoryId, genreId) {
  const cat = state.meta.categories.find((c) => c.id === categoryId);
  const genre = cat && cat.genres.find((g) => g.id === genreId);
  return cat && genre ? `${cat.label} · ${genre.label}` : `${categoryId} · ${genreId}`;
}

/* ── 마크다운 + 인용 칩 렌더링 ──
   분석 결과는 [{text, citations}] 조각 목록. 근거가 검증된 조각은 밑줄 강조(mark)하고,
   조각 뒤에 인용 칩을 붙인다. 모든 텍스트는 escape 후 제한된 마크다운만 변환한다. */
const M_OPEN = "\u0001", M_CLOSE = "\u0002", C_OPEN = "\u0003", C_CLOSE = "\u0004";
const CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;

function isGrounding(c) {
  return c.verified && c.source !== "intro" && (c.tier === null || c.tier === "A" || c.tier === "B");
}

function chipHtml(c, key) {
  let cls = "cite";
  let label;
  if (c.source === "work") { cls += " cite-work"; label = c.labels.join("–") || "원문"; }
  else if (c.source === "intro") { cls += " cite-intro"; label = "소개글"; }
  else { cls += ` cite-tier-${c.tier}`; label = `${c.tier}등급`; }
  if (!c.verified) cls += " unverified";
  const mark = c.verified ? "" : " ⚠";
  const title = c.verified ? "인용 확인됨" : "인용 위치 확인 실패";
  return `<button type="button" class="${cls}" data-cite="${key}" title="${title}">${escapeHtml(label)}${mark}</button>`;
}

function inline(text, chips) {
  return text
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replaceAll(M_OPEN, '<mark class="grounded">')
    .replaceAll(M_CLOSE, "</mark>")
    .replace(new RegExp(`${C_OPEN}(\\d+)${C_CLOSE}`, "g"), (_, i) => chips[Number(i)] || "");
}

function renderSegments(segments) {
  const block = `b${++state.citeSeq}`;
  const cites = [];
  let src = "";
  for (const seg of segments) {
    const grounded = (seg.citations || []).some(isGrounding);
    // 표시용 제어 문자(\u0001~\u0004)와 겹치지 않도록 응답 속 제어 문자를 지운다
    const text = String(seg.text || "").replace(CONTROL_RE, "");
    // 조각 끝의 줄바꿈 앞에 칩을 넣어 칩이 다음 줄로 넘어가지 않게 한다
    const trail = (text.match(/\n+$/) || [""])[0];
    const body = trail ? text.slice(0, -trail.length) : text;
    src += body.split("\n").map((p) => (grounded && p.trim() ? M_OPEN + p + M_CLOSE : p)).join("\n");
    for (const c of seg.citations || []) {
      cites.push(c);
      src += `${C_OPEN}${cites.length - 1}${C_CLOSE}`;
    }
    src += trail;
  }
  state.citeRegistry.set(block, cites);
  const chips = cites.map((c, i) => chipHtml(c, `${block}:${i}`));

  const out = [];
  let list = false;
  let para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.join("<br>")}</p>`); para = []; } };
  const closeList = () => { if (list) { out.push("</ul>"); list = false; } };

  for (const rawLine of escapeHtml(src).split("\n")) {
    const line = rawLine.trim();
    const heading = line.match(new RegExp(`^${M_OPEN}?(#{1,6})\\s+(.*?)${M_CLOSE}?$`));
    const item = line.match(new RegExp(`^(${M_OPEN}?)[-*]\\s+(.*)$`));
    const quote = line.match(new RegExp(`^(${M_OPEN}?)&gt;\\s?(.*)$`));
    if (!line) { flushPara(); closeList(); continue; }
    if (heading) {
      flushPara(); closeList();
      out.push(`<h4>${inline(heading[2].replaceAll(M_OPEN, "").replaceAll(M_CLOSE, ""), chips)}</h4>`);
    } else if (item) {
      flushPara();
      if (!list) { out.push("<ul>"); list = true; }
      out.push(`<li>${inline(item[1] + item[2], chips)}</li>`);
    } else if (quote) {
      flushPara(); closeList();
      out.push(`<p class="note">${inline(quote[1] + quote[2], chips)}</p>`);
    } else {
      closeList();
      para.push(inline(line, chips));
    }
  }
  flushPara();
  closeList();
  return `<div class="md">${out.join("")}</div>`;
}

function resultHtml(result, { showBadge = true } = {}) {
  const s = result.summary || {};
  const pct = Math.round((s.ratio || 0) * 100);
  const meta = [];
  meta.push(`인용 ${s.citation_count || 0}개 (확인 ${s.verified_count || 0})`);
  if (result.mode === "demo") meta.push("데모 응답");
  else if (result.model) meta.push(`모델 ${escapeHtml(result.model)}${result.fallback_used ? " (대체 모델)" : ""}`);
  if (result.cached) meta.push("저장된 결과");
  if (result.truncated) meta.push("응답이 길어 중간에 끊겼습니다");
  const who = profileLabel(result.profile);
  if (who) meta.push(escapeHtml(who));
  if (result.parts > 1 && result.part_range) {
    meta.push(`${Number(result.part)}/${Number(result.parts)}부분 (${escapeHtml(result.part_range.from)}~${escapeHtml(result.part_range.to)})`);
  }
  const badge = result.badge || {};
  return `
    ${showBadge ? `<div class="badge badge-${escapeHtml(badge.level)}"><strong>${escapeHtml(badge.label)}</strong><span>${escapeHtml(badge.description)}</span></div>` : ""}
    ${badge.level !== "intro" ? `<progress class="meter" max="100" value="${pct}" title="근거율 ${pct}%"></progress>` : ""}
    <div class="result-meta"><span>근거율 ${pct}%</span>${meta.map((m) => `<span>${m}</span>`).join("")}</div>
    ${checksHtml(result.checks)}
    ${renderSegments(result.segments || [])}
    <div class="cite-detail" hidden></div>`;
}

// 번역 자동 검사(누락 문단·숫자·분량). 오류 판정이 아니라 원문과 대조해 볼 곳을 알려 준다
function checksHtml(checks) {
  if (!checks || !checks.items) return "";
  const items = checks.items.map((item) => {
    const mark = item.status === "ok" ? "✓" : "⚠";
    return `<li class="check-item check-${escapeHtml(item.status)}"><strong>${mark} ${escapeHtml(item.label)}</strong> ${escapeHtml(item.detail)}</li>`;
  });
  const jump = (checks.missing_paragraphs || []).slice(0, 12).map((p) =>
    `<button type="button" class="check-para" data-index="${Number(p.index)}">${escapeHtml(p.label)}</button>`).join("");
  return `<div class="checks"><div class="checks-title">자동 검사</div><ul>${items.join("")}</ul>${jump ? `<div class="check-jump">원문에서 보기 ${jump}</div>` : ""}</div>`;
}

/* ── 인용 클릭: 원문 강조 또는 참고자료 표시 ── */
function onCiteClick(event) {
  const jump = event.target.closest("button.check-para");
  if (jump) { highlightParagraphs([Number(jump.dataset.index)]); return; }
  const button = event.target.closest("button.cite");
  if (!button) return;
  const [block, index] = button.dataset.cite.split(":");
  const cite = (state.citeRegistry.get(block) || [])[Number(index)];
  if (!cite) return;
  const container = button.closest(".result, .qa-item");
  const detail = container && $(".cite-detail", container);
  const status = cite.verified ? "원문 대조 확인" : "⚠ 인용 위치를 확인하지 못했습니다";

  if (cite.source === "work") {
    highlightParagraphs(cite.paragraphs);
    if (detail) {
      detail.innerHTML = `<strong>원문 ${escapeHtml(cite.labels.join("–"))}</strong> · ${status}<br><q>${escapeHtml(cite.cited_text)}</q>`;
      detail.hidden = false;
    }
  } else if (detail) {
    detail.innerHTML = `<strong>${escapeHtml(cite.tier_label)}</strong> · ${escapeHtml(cite.title || "")} · ${status}<br><q>${escapeHtml(cite.cited_text)}</q>`;
    detail.hidden = false;
  }
}

function highlightParagraphs(indices) {
  $$(".para.highlight").forEach((el) => el.classList.remove("highlight"));
  let first = null;
  for (const i of indices) {
    const el = document.getElementById(`para-${i}`);
    if (el) { el.classList.add("highlight"); first = first || el; }
  }
  if (first) {
    if (window.matchMedia("(max-width: 1180px)").matches) showPane("pane-text");
    first.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

/* ── 자료실 목록 ── */
async function refreshWorks() {
  const data = await api("/api/works");
  state.works = data.works;
  const list = $("#work-list");
  list.innerHTML = "";
  if (!state.works.length) {
    list.innerHTML = '<li class="empty">아직 등록한 작품이 없습니다.</li>';
    return;
  }
  for (const w of state.works) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.id = w.id;
    if (state.current && state.current.work.id === w.id) button.classList.add("active");
    const source = w.source_kind === "intro_only" ? "소개글만"
      : `${w.source_kind === "ocr" ? "이미지 추출 · " : ""}${w.paragraph_count}개 단락`;
    button.innerHTML = `<span class="w-title">${escapeHtml(w.title)}</span><span class="w-meta">${escapeHtml(genreLabel(w.category, w.genre))} · ${source}</span>`;
    button.addEventListener("click", () => openWork(w.id).catch((error) => toast(error.message)));
    li.appendChild(button);
    list.appendChild(li);
  }
}

/* ── 작품 화면 ── */
async function openWork(id) {
  const data = await api(`/api/works/${encodeURIComponent(id)}`);
  const analyses = new Map();
  for (const a of data.analyses) analyses.set(`${a.kind}:${a.option}`, { ...a.result, cached: true });
  state.current = {
    work: data.work, paragraphs: data.paragraphs, analyses, questions: data.questions,
    translateParts: data.translate_parts || [],
    glossaries: new Map(), // 번역 대상 → { entries, version }
    glossaryLoading: new Set(),
  };
  state.kind = data.paragraphs.length ? "overview" : "guide";
  $("#empty-state").hidden = true;
  $("#work-view").hidden = false;
  renderWork();
  await refreshWorks();
}

function renderWork() {
  const { work, paragraphs } = state.current;
  $("#work-title").textContent = work.title;
  const parts = [work.author, genreLabel(work.category, work.genre)];
  if (work.source_filename) parts.push(work.source_filename);
  if (work.source_kind === "ocr") parts.push("이미지에서 추출(확인 후 등록)");
  parts.push(`등록 ${formatDate(work.created_at)}`);
  $("#work-meta").textContent = parts.filter(Boolean).join(" · ");

  // 서지정보 · 책 소개 카드 (분석 결과와 분리해서 표시)
  const cards = [];
  if (work.bib) {
    const b = work.bib;
    const rows = [b.publisher, b.publish_date, b.isbn && `ISBN ${b.isbn}`, b.kdc && `KDC ${b.kdc}`, b.pages && `${b.pages}쪽`]
      .filter(Boolean).map(escapeHtml).join(" · ");
    cards.push(`<div class="info-card"><h3>서지정보 <span class="chip chip-work">국립중앙도서관</span></h3><p>${rows || "-"}</p></div>`);
  }
  if (work.intro_text) {
    const link = work.intro_source_url
      ? ` · <a href="${escapeHtml(work.intro_source_url)}" target="_blank" rel="noopener noreferrer nofollow">출처</a>` : "";
    cards.push(`<div class="info-card"><h3>책 소개 <span class="chip chip-intro">사용자 제공 소개 · C</span>${link}</h3><p class="clamp">${escapeHtml(work.intro_text)}</p></div>`);
  }
  $("#work-cards").innerHTML = cards.join("");

  // 원문
  const body = $("#text-body");
  body.classList.toggle("verse", Boolean(work.verse));
  if (!paragraphs.length) {
    body.innerHTML = '<p class="no-text">원문이 없습니다. 소개글만으로는 읽기 가이드만 제공합니다. 작품 분석이 필요하면 원문을 올려 새로 등록해 주세요.</p>';
  } else {
    const html = [];
    let section = null;
    for (const p of paragraphs) {
      if (p.section && p.section !== section) {
        html.push(`<h3 class="section-title">${escapeHtml(p.section)}</h3>`);
        section = p.section;
      }
      const stanzaStart = work.verse && p.index > 0 && /(^|\s)1행$/.test(p.label) ? " stanza-start" : "";
      html.push(`<p class="para${stanzaStart}" id="para-${p.index}"><span class="para-label">${escapeHtml(p.label)}</span><span class="para-text">${escapeHtml(p.text)}</span></p>`);
    }
    body.innerHTML = html.join("");
  }

  renderKindTabs();
  renderAnalysis();
  renderQuestions();
  showPane("pane-text");
}

function renderKindTabs() {
  const hasText = state.current.paragraphs.length > 0;
  const tabs = $("#kind-tabs");
  tabs.innerHTML = "";
  for (const [kind, label] of Object.entries(state.meta.kinds)) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.setAttribute("role", "tab");
    const disabled = !hasText && kind !== "guide";
    button.disabled = disabled;
    if (disabled) button.title = "원문이 있어야 할 수 있는 분석입니다";
    if (kind === state.kind) { button.classList.add("active"); button.setAttribute("aria-selected", "true"); }
    button.addEventListener("click", () => { state.kind = kind; renderKindTabs(); renderAnalysis(); });
    tabs.appendChild(button);
  }
  const select = $("#translate-target");
  select.hidden = state.kind !== "translate";
  if (!select.options.length) {
    for (const [value, label] of Object.entries(state.meta.translate_targets)) {
      select.add(new Option(label, value));
    }
  }
  renderPartSelect();
}

// 긴 글의 번역은 서버가 나눈 부분 단위로 요청한다(한 번의 응답 한도 안에서 빠짐없이 옮기기 위해)
function renderPartSelect() {
  const select = $("#translate-part");
  const parts = state.current.translateParts;
  select.hidden = state.kind !== "translate" || parts.length < 2;
  if (select.dataset.work === state.current.work.id) return;
  select.dataset.work = state.current.work.id;
  select.innerHTML = "";
  for (const p of parts) {
    const range = p.from === p.to ? p.from : `${p.from}~${p.to}`;
    select.add(new Option(`${p.part}/${parts.length}부분 (${range})`, String(p.part)));
  }
}

function currentTarget() {
  return $("#translate-target").value || "easy";
}

function analysisKey() {
  const option = state.kind === "translate" ? `${currentTarget()}:${$("#translate-part").value || "1"}` : "";
  return { key: `${state.kind}:${option}${profileSuffix()}`, option };
}

function renderAnalysis() {
  const { key } = analysisKey();
  const result = state.current.analyses.get(key);
  syncGlossary();
  const body = $("#analysis-body");
  $("#refresh-analysis").hidden = !result;
  $("#run-analysis").hidden = Boolean(result);
  if (!result) {
    const label = state.meta.kinds[state.kind];
    body.innerHTML = `<p class="loading">'${escapeHtml(label)}'을(를) 실행하면 원문 인용과 함께 결과가 표시됩니다.</p>`;
    return;
  }
  body.innerHTML = `<div class="result">${glossaryNote(result)}${resultHtml(result)}</div>`;
}

async function runAnalysis(refresh) {
  // 요청 중에 다른 작품·분석 종류로 바꿔도 결과가 원래 작품에 저장되도록 시작 시점의 값을 고정한다
  const current = state.current;
  const kind = state.kind;
  const { key, option } = analysisKey();
  const profile = { ...state.profile };
  const stillShowing = () => state.current === current && analysisKey().key === key;
  const buttons = [$("#run-analysis"), $("#refresh-analysis")];
  buttons.forEach((b) => { b.disabled = true; });
  $("#analysis-body").innerHTML = '<p class="loading">분석 중입니다… 긴 작품은 1~2분 걸릴 수 있어요.</p>';
  try {
    const result = await api(`/api/works/${current.work.id}/analyze`, {
      method: "POST",
      json: { kind, option, refresh, audience: profile.audience, lang: profile.lang },
    });
    current.analyses.set(key, result);
    if (stillShowing()) renderAnalysis();
  } catch (error) {
    if (stillShowing()) $("#analysis-body").innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
    else toast(error.message);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

/* ── 번역 용어집: 번역 대상마다 저장, 저장하지 않은 수정은 다시 그리지 않고 유지 ── */
async function loadGlossary(target) {
  const current = state.current;
  if (current.glossaryLoading.has(target)) return;
  current.glossaryLoading.add(target);
  try {
    const data = await api(`/api/works/${current.work.id}/glossary?target=${encodeURIComponent(target)}`);
    current.glossaries.set(target, data);
    if (state.current === current) renderAnalysis();
  } catch (error) {
    // 실패한 대상은 빈 용어집으로 두어 다시 그릴 때마다 요청·알림이 반복되지 않게 한다(작품을 다시 열면 재시도)
    current.glossaries.set(target, { entries: [], version: null, updated_at: null });
    toast(error.message);
  } finally {
    current.glossaryLoading.delete(target);
  }
}

function syncGlossary() {
  const panel = $("#glossary-panel");
  const show = state.kind === "translate" && state.current.paragraphs.length > 0;
  panel.hidden = !show;
  if (!show) return;
  const target = currentTarget();
  const glossary = state.current.glossaries.get(target);
  if (!glossary) {
    $("#glossary-count").textContent = "";
    loadGlossary(target);
    return;
  }
  const shown = `${state.current.work.id}:${target}:${glossary.version}`;
  if (panel.dataset.shown === shown) return; // 저장하지 않은 수정을 지우지 않는다
  panel.dataset.shown = shown;
  panel.dataset.dirty = "";
  $("#glossary-count").textContent = `${glossary.entries.length}개`;
  const rows = $("#glossary-rows");
  rows.innerHTML = glossary.entries.length ? "" : '<p class="hint">아직 정한 용어가 없습니다.</p>';
  glossary.entries.forEach((entry) => addGlossaryRow(entry));
}

function addGlossaryRow(entry = { term: "", rendering: "", note: "", kind: "기타", source: "user" }) {
  const rows = $("#glossary-rows");
  const empty = $(".hint", rows);
  if (empty) empty.remove();
  const row = document.createElement("div");
  row.className = "glossary-row";
  row.dataset.kind = entry.kind || "기타";
  row.dataset.source = entry.source || "user";
  row.innerHTML = `<input class="g-term" maxlength="60" placeholder="원문 용어" aria-label="원문 용어">
    <input class="g-rendering" maxlength="120" placeholder="번역어" aria-label="번역어">
    <input class="g-note" maxlength="200" placeholder="메모(뜻·옮긴 이유)" aria-label="메모">
    <button type="button" class="btn ghost g-del" aria-label="이 용어 지우기" title="지우기">✕</button>`;
  $(".g-term", row).value = entry.term || "";
  $(".g-rendering", row).value = entry.rendering || "";
  $(".g-note", row).value = entry.note || "";
  if (entry.source === "auto") row.classList.add("auto");
  rows.appendChild(row);
  return row;
}

function glossaryRowsData() {
  return $$("#glossary-rows .glossary-row")
    .map((row) => ({
      term: $(".g-term", row).value.trim(),
      rendering: $(".g-rendering", row).value.trim(),
      note: $(".g-note", row).value.trim(),
      kind: row.dataset.kind,
      source: row.dataset.source,
    }))
    .filter((e) => e.term && e.rendering);
}

async function saveGlossary({ quiet = false } = {}) {
  const current = state.current;
  const target = currentTarget();
  const button = $("#glossary-save");
  button.disabled = true;
  try {
    const data = await api(`/api/works/${current.work.id}/glossary?target=${encodeURIComponent(target)}`, {
      method: "PUT",
      json: { entries: glossaryRowsData() },
    });
    current.glossaries.set(target, data);
    if (state.current === current) renderAnalysis();
    if (!quiet) toast(`용어집을 저장했습니다(${data.entries.length}개).`);
    return true;
  } catch (error) {
    toast(error.message);
    return false;
  } finally {
    button.disabled = false;
  }
}

async function extractGlossary() {
  const current = state.current;
  const target = currentTarget();
  const part = Number($("#translate-part").value || "1");
  const button = $("#glossary-extract");
  if ($("#glossary-panel").dataset.dirty && !(await saveGlossary({ quiet: true }))) return;
  button.disabled = true;
  button.textContent = "용어를 뽑는 중…";
  try {
    const data = await api(`/api/works/${current.work.id}/glossary/extract`, { method: "POST", json: { target, part } });
    current.glossaries.set(target, data);
    if (state.current === current) renderAnalysis();
    toast(data.added
      ? `새 용어 ${data.added}개를 추가했습니다. 번역어를 확인하고 필요하면 고쳐서 저장하세요.`
      : "새로 추가할 용어가 없습니다.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "이 부분에서 용어 뽑기";
  }
}

function glossaryNote(result) {
  if (result.kind !== "translate") return "";
  const glossary = state.current.glossaries.get(result.target || currentTarget());
  if (!glossary) return "";
  const used = (result.glossary && result.glossary.version) || null;
  if ((glossary.version || null) === used) return "";
  return '<p class="note stale">용어집이 이 결과를 만든 뒤 바뀌었습니다. \'다시 분석\'을 누르면 지금 용어집으로 옮깁니다.</p>';
}

/* ── 질의응답 ── */
function questionHtml(q) {
  return `<div class="qa-item"><div class="q">${escapeHtml(q.question)}</div><div class="a">${resultHtml(q.result, { showBadge: true })}</div></div>`;
}

function renderQuestions() {
  const list = $("#qa-list");
  const questions = state.current.questions;
  list.innerHTML = questions.length
    ? questions.map(questionHtml).join("")
    : '<p class="qa-empty">원문에 대해 궁금한 점을 물어보세요. 답에는 원문 인용이 붙고, 근거가 없으면 없다고 답합니다.</p>';
}

async function submitQuestion(event) {
  event.preventDefault();
  const input = $("#qa-input");
  const question = input.value.trim();
  if (!question) return;
  const current = state.current; // 답을 기다리는 동안 작품을 바꿔도 원래 작품에 기록한다
  const button = $("#qa-form button[type=submit]");
  button.disabled = true;
  const list = $("#qa-list");
  if (!current.questions.length) list.innerHTML = "";
  const pending = document.createElement("div");
  pending.className = "qa-item";
  pending.innerHTML = `<div class="q">${escapeHtml(question)}</div><p class="loading">답변을 만드는 중입니다…</p>`;
  list.appendChild(pending);
  pending.scrollIntoView({ behavior: "smooth", block: "end" });
  try {
    const data = await api(`/api/works/${current.work.id}/ask`, {
      method: "POST",
      json: { question, audience: state.profile.audience, lang: state.profile.lang },
    });
    current.questions.push({ question: data.question, result: data.result });
    if (pending.isConnected) pending.outerHTML = questionHtml({ question: data.question, result: data.result });
    if (state.current === current) input.value = "";
  } catch (error) {
    const loading = pending.querySelector(".loading");
    if (pending.isConnected && loading) loading.outerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
    else toast(error.message);
  } finally {
    button.disabled = false;
  }
}

/* ── 모바일 보기 전환 ── */
function showPane(id) {
  $$(".pane").forEach((p) => p.classList.toggle("active", p.id === id));
  $$(".pane-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.pane === id));
}

/* ── 새 작품 등록 ── */
function renderCategoryOptions() {
  const box = $("#category-options");
  box.innerHTML = "";
  state.meta.categories.forEach((cat, i) => {
    const label = document.createElement("label");
    label.innerHTML = `<input type="radio" name="category" value="${escapeHtml(cat.id)}"${i === 0 ? " checked" : ""}>${escapeHtml(cat.label)}`;
    if (i === 0) label.classList.add("active");
    label.title = cat.description;
    label.querySelector("input").addEventListener("change", () => {
      $$("#category-options label").forEach((l) => l.classList.toggle("active", l === label));
      renderGenreOptions(cat.id);
    });
    box.appendChild(label);
  });
  renderGenreOptions(state.meta.categories[0].id);
}

function renderGenreOptions(categoryId, preferred) {
  const select = $("#genre-select");
  select.innerHTML = "";
  const cat = state.meta.categories.find((c) => c.id === categoryId);
  for (const g of cat.genres) select.add(new Option(`${g.label} — ${g.description}`, g.id));
  if (preferred && cat.genres.some((g) => g.id === preferred)) select.value = preferred;
}

function selectCategory(categoryId, genreId) {
  const input = $(`#category-options input[value="${categoryId}"]`);
  if (!input) return;
  input.checked = true;
  $$("#category-options label").forEach((l) => l.classList.toggle("active", l.contains(input)));
  renderGenreOptions(categoryId, genreId);
}

function setSource(kind) {
  $$("[data-source]").forEach((b) => b.classList.toggle("active", b.dataset.source === kind));
  $("#source-file").hidden = kind !== "file";
  $("#source-paste").hidden = kind !== "paste";
  $("#source-ocr").hidden = kind !== "ocr";
}

function currentSource() {
  return $("[data-source].active")?.dataset.source || "file";
}

/* ── 이미지·스캔본 텍스트 추출(OCR) ──
   사진은 브라우저에서 긴 변 2576px·약 375만 화소 이하로 줄이고 JPEG로 다시 그려 보낸다.
   다시 그리는 과정에서 위치 정보(EXIF) 같은 메타데이터가 빠진다(서버에서도 한 번 더 지운다). */
const OCR_MAX_EDGE = 2576;
const OCR_MAX_PIXELS = 3750000;

async function decodeImage(file) {
  try {
    return await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch (error) {
    if (error instanceof TypeError) return createImageBitmap(file);
    throw error;
  }
}

async function downscaleImage(file) {
  const bitmap = await decodeImage(file);
  const scale = Math.min(
    1,
    OCR_MAX_EDGE / Math.max(bitmap.width, bitmap.height),
    Math.sqrt(OCR_MAX_PIXELS / (bitmap.width * bitmap.height)),
  );
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, width, height);
  context.drawImage(bitmap, 0, 0, width, height);
  bitmap.close();
  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("이미지를 변환하지 못했습니다."))), "image/jpeg", 0.92);
  });
  return new File([blob], `${file.name.replace(/\.[^.]+$/, "") || "page"}.jpg`, { type: "image/jpeg" });
}

function isPdf(file) {
  return file.type === "application/pdf" || /\.pdf$/i.test(file.name);
}

function resetOcr() {
  state.ocr = null;
  $("#ocr-status").textContent = "";
  $("#ocr-result").hidden = true;
  $("#ocr-text").value = "";
  $("#ocr-confirm").checked = false;
  $("#ocr-summary").innerHTML = "";
  $("#ocr-uncertain").innerHTML = "";
}

function showOcrResult(result) {
  state.ocr = result;
  const status = $("#ocr-status");
  if (!result.has_text) {
    status.textContent = "글자를 찾지 못했습니다. 더 밝고 선명하게 찍은 이미지로 다시 시도해 주세요.";
    $("#ocr-result").hidden = true;
    return;
  }
  const chips = [`<span class="chip chip-work">${result.pages}쪽</span>`];
  chips.push(
    result.uncertain_count
      ? `<span class="chip chip-warn">불확실 ${result.uncertain_count}곳</span>`
      : '<span class="chip chip-ok">불확실 표시 없음</span>',
  );
  const maskedTotal = Object.values(result.masked || {}).reduce((a, b) => a + b, 0);
  if (maskedTotal) chips.push(`<span class="chip chip-intro">개인정보 ${maskedTotal}건 가림</span>`);
  if (result.mode === "demo") chips.push('<span class="chip chip-warn">데모 응답</span>');
  const notes = [result.layout, ...(result.notes || [])].filter(Boolean).map(escapeHtml);
  $("#ocr-summary").innerHTML = chips.join("") + notes.map((n) => `<span class="note">${n}</span>`).join("");
  $("#ocr-uncertain").innerHTML = (result.uncertain_snippets || [])
    .map((snippet) => `<li><code>${escapeHtml(snippet)}</code></li>`)
    .join("");
  $("#ocr-text").value = result.text;
  $("#ocr-confirm").checked = false;
  $("#ocr-result").hidden = false;
  status.textContent = result.uncertain_count
    ? "[?] 표시가 있는 곳을 원본과 대조해 고친 뒤 확인란을 체크하고 등록해 주세요."
    : "추출한 텍스트를 원본과 대조해 확인한 뒤 등록해 주세요.";
}

async function runOcr() {
  const files = Array.from($("#ocr-input").files);
  const status = $("#ocr-status");
  if (!files.length) {
    status.textContent = "이미지나 스캔 PDF를 먼저 선택해 주세요.";
    return;
  }
  const button = $("#ocr-run");
  button.disabled = true;
  status.textContent = "이미지를 준비하는 중…";
  try {
    const data = new FormData();
    for (const file of files) {
      let upload = file;
      if (!isPdf(file)) {
        try {
          upload = await downscaleImage(file);
        } catch (_) {
          upload = file; // 브라우저가 못 여는 형식은 서버가 안내 메시지를 돌려준다
        }
      }
      data.append("files", upload, upload.name);
    }
    data.set("scope", $("#ocr-scope").value);
    data.set("genre", $("#genre-select").value);
    status.textContent = "텍스트를 추출하는 중… 쪽수가 많으면 1~2분 걸릴 수 있어요.";
    showOcrResult(await api("/api/ocr", { method: "POST", body: data }));
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function openNewWork() {
  const form = $("#new-work-form");
  form.reset();
  state.bibSelected = null;
  $("#bib-results").innerHTML = "";
  $("#bib-selected").hidden = true;
  resetOcr();
  renderCategoryOptions();
  setSource("file");
  $("#new-work-dialog").showModal();
}

async function searchBib() {
  const query = $("#bib-query").value.trim();
  if (!query) return;
  const digits = query.replace(/[^0-9Xx]/g, "");
  const params = new URLSearchParams(digits.length === 10 || digits.length === 13 ? { isbn: query } : { q: query });
  const list = $("#bib-results");
  list.innerHTML = '<li class="hint">검색 중…</li>';
  try {
    const data = await api(`/api/bib/search?${params}`);
    list.innerHTML = "";
    if (!data.results.length) { list.innerHTML = '<li class="hint">검색 결과가 없습니다.</li>'; return; }
    for (const r of data.results) {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = `<strong>${escapeHtml(r.title)}</strong> · ${escapeHtml(r.author || "저자 미상")} · ${escapeHtml(r.publisher || "")}`;
      button.addEventListener("click", () => {
        state.bibSelected = r;
        const form = $("#new-work-form");
        form.elements.title.value = r.title;
        if (r.author) form.elements.author.value = r.author;
        if (r.suggested_category) selectCategory(r.suggested_category, r.suggested_genre);
        $("#bib-selected").textContent = `선택: ${r.title}${r.kdc ? ` (KDC ${r.kdc} → 장르 자동 추천)` : ""}`;
        $("#bib-selected").hidden = false;
        list.innerHTML = "";
      });
      li.appendChild(button);
      list.appendChild(li);
    }
  } catch (error) {
    list.innerHTML = `<li class="error">${escapeHtml(error.message)}</li>`;
  }
}

async function submitWork(event) {
  event.preventDefault();
  const form = $("#new-work-form");
  const data = new FormData();
  const source = currentSource();
  data.set("category", form.elements.category.value);
  data.set("genre", form.elements.genre.value);
  data.set("title", form.elements.title.value);
  data.set("author", form.elements.author.value);
  data.set("intro_text", form.elements.intro_text.value);
  data.set("intro_source_url", form.elements.intro_source_url.value);
  if (state.bibSelected) data.set("bib_json", JSON.stringify(state.bibSelected));
  const file = $("#file-input").files[0];
  if (source === "file" && file) data.set("file", file);
  if (source === "paste") data.set("text", form.elements.text.value);
  if (source === "ocr") {
    const text = $("#ocr-text").value.trim();
    if (text && !$("#ocr-confirm").checked) {
      toast("추출한 텍스트를 원본과 대조해 확인한 뒤 확인란을 체크해 주세요.");
      $("#ocr-confirm").focus();
      return;
    }
    data.set("text", text);
    data.set("origin", "ocr");
  }

  const button = $("#submit-work");
  button.disabled = true;
  button.textContent = "등록 중…";
  try {
    const created = await api("/api/works", { method: "POST", body: data });
    $("#new-work-dialog").close();
    toast("등록했습니다.");
    await openWork(created.work.id);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "등록";
  }
}

/* ── 개인정보 고지 ── */
const NOTICE_INTERVAL_MS = 30 * 24 * 60 * 60 * 1000;

function markNoticeSeen() {
  try { localStorage.setItem("rh.noticeSeenAt", String(Date.now())); } catch (_) { /* 저장소 없음 */ }
  $("#notice-banner").hidden = true;
}

function noticeDue() {
  try {
    const seen = Number(localStorage.getItem("rh.noticeSeenAt") || 0);
    return Date.now() - seen >= NOTICE_INTERVAL_MS;
  } catch (_) {
    return false;
  }
}

async function openPrivacy() {
  markNoticeSeen();
  const body = $("#privacy-body");
  body.innerHTML = '<p class="loading">불러오는 중…</p>';
  $("#privacy-dialog").showModal();
  try {
    const n = await api("/api/privacy/notice");
    const rows = (items, cols) => items.map((it) => `<tr>${cols.map((c) => `<td>${escapeHtml(it[c])}</td>`).join("")}</tr>`).join("");
    const detected = n.masking_detected.length
      ? `<ul>${n.masking_detected.map((d) => `<li>${escapeHtml(d.label)}: ${d.count}건 자동 마스킹</li>`).join("")}</ul>`
      : "<p>최근 30일 동안 마스킹된 항목이 없습니다.</p>";
    body.innerHTML = `
      <h3>수집 항목</h3>
      <table><thead><tr><th>항목</th><th>목적</th><th>보관</th></tr></thead>
      <tbody>${rows(n.collected_items, ["item", "purpose", "retention"])}</tbody></table>
      <h3>수집하지 않는 정보</h3><p>${n.not_collected.map(escapeHtml).join(", ")}</p>
      <h3>최근 ${n.masking_period_days}일 민감정보 마스킹 (항목·개수만 표시)</h3>${detected}
      <p class="hint">자동 마스킹 대상: ${n.masking_rules.map(escapeHtml).join(", ")}</p>
      <h3>외부 처리</h3>
      ${n.external_processing.length
        ? `<table><thead><tr><th>받는 곳</th><th>항목</th><th>목적</th></tr></thead><tbody>${rows(n.external_processing, ["recipient", "items", "purpose"])}</tbody></table>`
        : "<p>현재 외부로 전송하는 정보가 없습니다(데모 모드).</p>"}
      <h3>운영 원칙</h3><ul>${n.policies.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`;
  } catch (error) {
    body.innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
  }
}

/* ── 시작 ── */
async function init() {
  state.workspace = loadWorkspace();
  try {
    [state.health, state.meta] = await Promise.all([api("/api/health"), api("/api/categories")]);
  } catch (error) {
    toast(`서버에 연결할 수 없습니다: ${error.message}`);
    return;
  }
  const badge = $("#mode-badge");
  badge.hidden = false;
  if (state.health.mode === "demo") {
    badge.textContent = "데모 모드";
    badge.className = "mode-badge demo";
    badge.title = "API 키가 없어 실제 분석 대신 화면 흐름만 보여 줍니다";
  } else {
    badge.textContent = "실시간 분석";
    badge.className = "mode-badge live";
    badge.title = state.health.model || "";
  }
  state.profile = loadProfile();
  renderProfileControls();
  $("#bib-section").hidden = !state.health.bib_enabled;
  $("#sidebar-note").textContent =
    `자료는 이 브라우저의 자료실에만 연결되며, 마지막 이용 후 ${state.health.retention_days}일이 지나면 자동 삭제됩니다.`;

  $("#new-work-btn").addEventListener("click", openNewWork);
  $("#new-work-form").addEventListener("submit", submitWork);
  $("#bib-search-btn").addEventListener("click", searchBib);
  $("#ocr-run").addEventListener("click", runOcr);
  $("#ocr-input").addEventListener("change", () => { if (state.ocr) resetOcr(); });
  $("#bib-query").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); searchBib(); } });
  $$("[data-source]").forEach((b) => b.addEventListener("click", () => setSource(b.dataset.source)));
  $$("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));
  $("#privacy-btn").addEventListener("click", openPrivacy);
  $("#notice-open").addEventListener("click", openPrivacy);
  $("#notice-dismiss").addEventListener("click", markNoticeSeen);
  // 30일마다 수집 항목 고지를 다시 안내한다(첫 방문 포함)
  $("#notice-banner").hidden = !noticeDue();
  $("#run-analysis").addEventListener("click", () => runAnalysis(false));
  $("#refresh-analysis").addEventListener("click", () => runAnalysis(true));
  $("#translate-target").addEventListener("change", renderAnalysis);
  $("#translate-part").addEventListener("change", renderAnalysis);
  $("#audience").addEventListener("change", onProfileChange);
  $("#explain-lang").addEventListener("change", onProfileChange);
  $("#glossary-add").addEventListener("click", () => {
    $("#glossary-panel").dataset.dirty = "1";
    $(".g-term", addGlossaryRow()).focus();
  });
  $("#glossary-save").addEventListener("click", () => saveGlossary());
  $("#glossary-extract").addEventListener("click", extractGlossary);
  $("#glossary-rows").addEventListener("input", (event) => {
    $("#glossary-panel").dataset.dirty = "1";
    const row = event.target.closest(".glossary-row");
    if (row) { row.dataset.source = "user"; row.classList.remove("auto"); }
  });
  $("#glossary-rows").addEventListener("click", (event) => {
    const del = event.target.closest(".g-del");
    if (!del) return;
    $("#glossary-panel").dataset.dirty = "1";
    del.closest(".glossary-row").remove();
  });
  $("#qa-form").addEventListener("submit", submitQuestion);
  $("#qa-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("#qa-form").requestSubmit();
  });
  $("#analysis-body").addEventListener("click", onCiteClick);
  $("#qa-list").addEventListener("click", onCiteClick);
  $$(".pane-tabs button").forEach((b) => b.addEventListener("click", () => showPane(b.dataset.pane)));
  $("#delete-work-btn").addEventListener("click", async () => {
    if (!state.current || !confirm(`'${state.current.work.title}'을(를) 삭제할까요? 분석 결과와 질문 기록도 함께 삭제됩니다.`)) return;
    try {
      await api(`/api/works/${state.current.work.id}`, { method: "DELETE" });
      state.current = null;
      $("#work-view").hidden = true;
      $("#empty-state").hidden = false;
      await refreshWorks();
      toast("삭제했습니다.");
    } catch (error) {
      toast(error.message);
    }
  });

  try {
    await refreshWorks();
  } catch (error) {
    toast(error.message);
  }
}

document.addEventListener("DOMContentLoaded", init);
