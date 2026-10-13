"use strict";
const CONSENT_VERSION = 1;
const SITE_KEYS = ["qnet", "kca", "kisa", "mock"];
const $ = (id) => document.getElementById(id);
const csv = (s) => String(s || "").split(",").map((x) => x.trim()).filter(Boolean);

function setStatus(text, ok) {
  $("status").textContent = text;
  $("status").className = ok ? "ok" : "err";
}

// 개인정보로 보이는 값(주민번호·전화번호·이메일)이 희망 조건에 섞이면 저장 거부 — 최소 수집
function looksPersonal(text) {
  return /\d{6}\s*-\s*[1-8]\d{6}|01[016789]-?\d{3,4}-?\d{4}|[\w.+-]+@[\w-]+\.[\w.]+/.test(text);
}

async function load() {
  const { settings } = await chrome.storage.local.get("settings");
  if (!settings) return;
  $("enabled").checked = !!settings.enabled;
  $("openAt").value = (settings.openAt || "").replace(/\+09:00$/, "").slice(0, 19);
  $("prefs").value = settings.prefsText || "";
  const o = (settings.overrides || {}).qnet || {};
  $("slotRow").value = o.slotRow || "";
  $("applyTexts").value = (o.applyTexts || []).join(", ");
  $("fullKeywords").value = (o.fullKeywords || []).join(", ");
  const agreed = settings.consent && settings.consent.version === CONSENT_VERSION;
  document.querySelectorAll("[data-c]").forEach((c) => (c.checked = agreed));
}

async function save() {
  const allAgreed = [...document.querySelectorAll("[data-c]")].every((c) => c.checked);
  if (!allAgreed) return setStatus("동의 항목을 모두 체크해 주세요.", false);
  const prefsText = $("prefs").value;
  const prefs = ExamMatch.parsePrefs(prefsText);
  if (!prefs.length) return setStatus("희망 조건을 1개 이상 적어 주세요.", false);
  if (looksPersonal(prefsText)) return setStatus("희망 조건에 개인정보(주민번호·전화·이메일)로 보이는 값이 있습니다. 지워 주세요.", false);
  const raw = $("openAt").value;
  if (!raw) return setStatus("접수 시작 시각을 입력해 주세요.", false);
  const openAt = (raw.length === 16 ? raw + ":00" : raw) + "+09:00";
  if (!Number.isFinite(Date.parse(openAt))) return setStatus("시각 형식이 올바르지 않습니다.", false);

  const override = {};
  if ($("slotRow").value.trim()) {
    try {
      document.querySelector($("slotRow").value.trim());
    } catch (_) {
      return setStatus("일정 행 선택자 형식이 올바르지 않습니다.", false);
    }
    override.slotRow = $("slotRow").value.trim();
  }
  if (csv($("applyTexts").value).length) override.applyTexts = csv($("applyTexts").value);
  if (csv($("fullKeywords").value).length) override.fullKeywords = csv($("fullKeywords").value);
  const overrides = {};
  if (Object.keys(override).length) SITE_KEYS.forEach((k) => (overrides[k] = override));

  const settings = {
    enabled: $("enabled").checked,
    openAt,
    prefsText,
    overrides,
    consent: { version: CONSENT_VERSION, at: new Date().toISOString() },
  };
  await chrome.storage.local.set({ settings });
  setStatus(`저장했습니다 (희망 ${prefs.length}개). 접수 화면을 새로고침하면 적용됩니다.`, true);
}

async function wipe() {
  await chrome.storage.local.clear();
  document.querySelectorAll("input[type=checkbox][data-c]").forEach((c) => (c.checked = false));
  ["openAt", "prefs", "slotRow", "applyTexts", "fullKeywords"].forEach((id) => ($(id).value = ""));
  setStatus("모든 설정과 동의 기록을 삭제했습니다.", true);
}

$("save").addEventListener("click", save);
$("wipe").addEventListener("click", wipe);
load();
