// 알림만 담당. 외부 서버로 아무것도 보내지 않는다.
"use strict";

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.type !== "notify") return;
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icon128.png",
    title: String(msg.title || "시험 접수 보조").slice(0, 60),
    message: String(msg.body || "").slice(0, 200),
    priority: 2,
    requireInteraction: true,
  });
});

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());

chrome.runtime.onInstalled.addListener((d) => {
  if (d.reason === "install") chrome.runtime.openOptionsPage(); // 설치 직후 동의·설정 화면
});
