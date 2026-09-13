const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const THEMES = {
  mist: {
    label: "雾蓝", bg: "#e0e5ec", dark: "#b8bcc2", light: "#ffffff",
    accent: "#6d5dfc", text: "#333333", muted: "#5b6470",
    dots: ["#6d5dfc", "#ff6b6b", "#4ecdc4", "#ffe66d"],
  },
  clay: {
    label: "陶土", bg: "#e8e3de", dark: "#c9c0b7", light: "#fffdfa",
    accent: "#e07a5f", text: "#3a3532", muted: "#655c53",
    dots: ["#e07a5f", "#3d405b", "#81b29a", "#f2cc8f"],
  },
  celadon: {
    label: "青瓷", bg: "#e2e8e6", dark: "#bcc9c5", light: "#ffffff",
    accent: "#2a9d8f", text: "#2f3b38", muted: "#52605c",
    dots: ["#2a9d8f", "#e76f51", "#264653", "#e9c46a"],
  },
  lavender: {
    label: "薰衣草", bg: "#e7e4f0", dark: "#c6c0d9", light: "#ffffff",
    accent: "#7c6ee6", text: "#3a3550", muted: "#5f5875",
    dots: ["#7c6ee6", "#e56b8c", "#5bc0be", "#f4d35e"],
  },
  graphite: {
    label: "石墨", bg: "#e4e6e9", dark: "#c2c6cc", light: "#ffffff",
    accent: "#4f6df5", text: "#2f3338", muted: "#5c636c",
    dots: ["#4f6df5", "#ef6461", "#37b5a8", "#f0c75e"],
  },
};

let currentTheme = "mist";
let unlimitedOn = false;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "-";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function toast(s) {
  const el = $("toast");
  el.hidden = false;
  el.textContent = s;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { el.hidden = true; }, 2200);
}

function show(data) {
  $("diag").textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
}

async function get(endpoint, params = {}) { return await bridge.apiGet(endpoint, params); }
async function post(endpoint, body = {}) { return await bridge.apiPost(endpoint, body); }

/* ---------------- theme ---------------- */

function hexLuminance(hex) {
  const value = String(hex || "").replace("#", "");
  if (value.length !== 6) return 0.8;
  const channel = (i) => {
    const c = parseInt(value.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
}

function applyTheme(theme) {
  const root = document.documentElement.style;
  const dark = hexLuminance(theme.bg) < 0.35;
  root.setProperty("--bg", theme.bg);
  root.setProperty("--surface", theme.bg);
  root.setProperty("--dark", theme.dark);
  root.setProperty("--light", theme.light);
  root.setProperty("--accent", theme.accent);
  root.setProperty("--text", dark ? "#e8ecf1" : (theme.text || "#333333"));
  root.setProperty("--muted", dark ? "#aeb6c2" : (theme.muted || "#5b6470"));
  root.setProperty("--ok", dark ? "#7fd8a8" : "#176a45");
  root.setProperty("--bad", dark ? "#ff9aa8" : "#b23345");
  const dots = theme.dots || [];
  ["--accent-2", "--accent-3", "--accent-4"].forEach((name, i) => {
    if (dots[i + 1]) root.setProperty(name, dots[i + 1]);
  });
}

function renderThemes() {
  const box = $("themes");
  box.innerHTML = Object.entries(THEMES).map(([key, theme]) => `
    <button type="button" class="swatch ${currentTheme === key ? "active" : ""}"
      data-palette="${key}" title="${esc(theme.label)}" aria-label="${esc(theme.label)}">
      <i style="background:${esc(theme.accent)}"></i>
    </button>`).join("") + `
    <button type="button" class="swatch ${currentTheme === "custom" ? "active" : ""}"
      data-palette="custom" title="自定义" aria-label="自定义">
      <i style="background:${esc($("cAccent").value || "#6d5dfc")}"></i>
    </button>`;
}

function selectTheme(name) {
  if (name === "custom") {
    currentTheme = "custom";
    $("customTheme").classList.add("open");
    applyCustomFromInputs();
  } else {
    const theme = THEMES[name] || THEMES.mist;
    currentTheme = THEMES[name] ? name : "mist";
    applyTheme(theme);
    $("customTheme").classList.remove("open");
  }
  renderThemes();
}

function customThemeValue() {
  return {
    bg: $("cBg").value, dark: $("cDark").value, light: $("cLight").value,
    accent: $("cAccent").value, text: "#333333", muted: "#5b6470",
    dots: [$("cAccent").value, "#ff6b6b", "#4ecdc4", "#ffe66d"],
  };
}

function applyCustomFromInputs() {
  applyTheme(customThemeValue());
}

async function saveTheme(name, custom) {
  try {
    await post("theme/save", custom ? { name, custom } : { name });
    return true;
  } catch (err) {
    console.warn("theme save failed", err);
    return false;
  }
}

/* ---------------- interest editor ---------------- */

let interestItems = [];
let interestConfigured = true;

function renderInterest() {
  const hint = interestConfigured
    ? ""
    : `<p class="sub">当前显示的是回退关键词（来自「兴趣关键词」配置），保存后会写入新的兴趣词表。</p>`;
  $("interest").innerHTML = hint + (interestItems.map((item, index) => `
    <div class="interest-row">
      <input type="text" class="interest-keyword" data-index="${index}"
        aria-label="兴趣词" placeholder="兴趣词，例如 AI" value="${esc(item.keyword)}" />
      <input type="number" class="interest-quota" data-index="${index}"
        aria-label="每轮入库数量" min="0" max="999" step="1" value="${esc(item.quota)}" />
      <button type="button" class="tiny ghost" data-act="interest-del" data-index="${index}"
        aria-label="删除该兴趣词">删</button>
    </div>`).join("") || `<p class="sub">暂无兴趣词，点击下方按钮添加。</p>`);
}

function collectInterest() {
  const keywords = [...document.querySelectorAll(".interest-keyword")];
  const quotas = [...document.querySelectorAll(".interest-quota")];
  const items = [];
  keywords.forEach((input, i) => {
    const keyword = input.value.trim();
    if (!keyword) return;
    const quota = Math.max(0, Math.min(999, parseInt(quotas[i]?.value || "0", 10) || 0));
    items.push({ keyword, quota });
  });
  return items;
}

async function saveInterest() {
  const items = collectInterest();
  if (!items.length) { toast("至少保留一个兴趣词"); return; }
  try {
    const result = await post("interest/save", { items });
    interestItems = items;
    renderInterest();
    toast(`已保存 ${result?.count ?? items.length} 个兴趣词`);
    await load();
  } catch (err) {
    show(err.stack || String(err));
    toast("保存失败");
  }
}

/* ---------------- data loading ---------------- */

async function load(includeInterest = true) {
  const [status, runs, videos] = await Promise.all([
    get("status"), get("runs"), get("recent"),
  ]);

  const c = status.counts || {};
  const a = status.audit || {};
  $("stats").innerHTML = [
    ["视频记录", c.videos], ["已入库", c.ingested], ["已并入", c.merged],
    ["汇总文档", c.digests], ["无字幕", c.no_subtitle], ["失败", c.failed],
    ["排除", c.excluded], ["审核可疑", a.suspect],
    ["知识库", status.kb_id ? String(status.kb_id).slice(0, 8) : "未创建"],
  ].map(([k, v]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join("");

  const lr = status.last_run;
  const daily = status.daily || {};
  const dailyDesc = Object.entries(daily).map(([k, v]) => `${esc(k)}:${esc(v)}`).join("、") || "无";
  const digests = (status.digests || []).map((d) =>
    `${esc(d.keyword)}(${esc(d.rounds)}轮${d.audit_status ? `/${esc(d.audit_status)}` : ""})`
  ).join("、") || "无";
  const startHour = Number(status.daily_start_hour);
  const schedule = Number.isFinite(startHour) && startHour >= 0 && startHour <= 23
    ? `每天 ${String(startHour).padStart(2, "0")}:00 · 下次 ${esc(status.next_run || "-")}`
    : "未开启（只手动）";
  $("status").innerHTML = `
    <span><b>插件</b>${status.enabled ? "运行中" : "已关闭"}</span>
    <span><b>Cookie</b>${status.has_cookie ? "已配置" : "未配置"}</span>
    <span><b>按需读</b>${status.on_demand ? "已启用" : "未启用"}</span>
    <span><b>模式</b>${status.unlimited_mode ? "无限（高消耗）" : "每日配额"}</span>
    <span><b>定时</b>${schedule}</span>
    <span><b>今日入库</b>${dailyDesc}</span>
    <span><b>汇总</b>${digests}</span>
    <span><b>审核</b>通过 ${esc(a.ok || 0)} / 可疑 ${esc(a.suspect || 0)} / 未审 ${esc(a.pending || 0)}</span>
    <span><b>上次任务</b>${lr ? `${fmtTime(lr.started_at)} ${esc(lr.status)}` : "暂无"}</span>`;

  $("runs").innerHTML = (runs.items || []).map((r) => `
    <button class="run-row" data-run="${esc(r.run_id)}">
      <time>${fmtTime(r.started_at)}</time>
      <span>${esc(r.trigger)}</span>
      <b>${esc(r.status)}</b>
      <span>处理 ${esc(r.processed)} · 入库 ${esc(r.ingested)} · 跳过 ${esc(r.skipped)} · 失败 ${esc(r.failed)}</span>
    </button>`).join("") || `<p class="sub">暂无运行记录。</p>`;

  $("videos").innerHTML = (videos.items || []).map((v) => `
    <div class="video">
      <b>${esc(v.title || v.bvid)}</b>
      <small>${fmtTime(v.updated_at)} · ${esc(v.bvid)} · ${esc(v.status)}
        ${v.category ? ` · ${esc(v.category)}` : ""}${v.attempts ? ` · 尝试 ${esc(v.attempts)} 次` : ""}</small>
      <small>${v.doc_name ? `${esc(v.doc_name)} · ` : ""}${esc(v.reason)}
        ${v.audit_status ? `<span class="badge ${v.audit_status === "ok" ? "ok" : "bad"}">审核:${esc(v.audit_status)}</span>` : ""}</small>
    </div>`).join("") || `<p class="sub">暂无视频记录。</p>`;

  $("digests").innerHTML = (status.digests || []).map((d) => `
    <div class="digest">
      <b>${esc(d.doc_name || d.keyword)}</b>
      <small>${esc(d.keyword)} · 第 ${esc(d.rounds)} 轮 · 来源 ${esc(d.sources)} 条
        ${d.audit_status ? `<span class="badge ${d.audit_status === "ok" ? "ok" : "bad"}">审核:${esc(d.audit_status)}</span>` : ""}</small>
      ${d.audit_note ? `<small>${esc(d.audit_note)}</small>` : ""}
    </div>`).join("") || `<p class="sub">暂无汇总文档。</p>`;

  unlimitedOn = Boolean(status.unlimited_mode);
  const unlimitedBtn = document.querySelector('[data-act="unlimited"]');
  if (unlimitedBtn) {
    unlimitedBtn.textContent = unlimitedOn ? "无限模式：开" : "无限模式：关";
    unlimitedBtn.classList.toggle("primary", unlimitedOn);
  }

  if (includeInterest) {
    try {
      const interest = await get("interest");
      interestItems = (interest.items || []).map((item) => ({
        keyword: item.keyword, quota: item.quota,
      }));
      interestConfigured = Boolean(interest.configured);
      renderInterest();
    } catch (err) {
      console.warn("interest load failed", err);
    }
  }

  show({ status, runs, videos });
}

/* ---------------- actions ---------------- */

async function health() {
  const r = await get("health");
  show(r);
  const issues = [];
  if (r.embedding === "missing") issues.push("缺少 Embedding");
  if (r.knowledge_base !== "ready") issues.push("知识库未就绪");
  if (r.on_demand_tool !== "ready") issues.push("按需读工具未启用");
  if (r.cookie !== "configured") issues.push("Cookie 未配置（AI 字幕可能读不到）");
  const cooling = Number(r.subtitle_cooldown_seconds || 0);
  if (cooling > 0) issues.push(`B站风控冷却中 ${cooling}s`);
  toast(issues.length ? `健康检查：${issues.join("；")}` : "健康检查：全部正常");
}

async function runOnce() {
  const btn = document.querySelector('[data-act="run"]');
  if (btn) btn.disabled = true;
  try {
    const r = await post("run", {});
    show(r);
    toast(r.queued ? "已排队，当前任务结束后开始" : "已开始刷取");
    await load(false);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------------- events ---------------- */

document.addEventListener("click", async (event) => {
  const runBtn = event.target.closest("[data-run]");
  if (runBtn) {
    const r = await get("run-events", { run_id: runBtn.dataset.run });
    $("events").innerHTML = (r.items || []).map((x) => `
      <div class="event">
        <time>${fmtTime(x.ts)}</time>
        <b>${esc(x.stage)}</b> <span>${esc(x.status)}</span>
        <div>${esc(x.message)}</div>
        ${x.bvid ? `<small>${esc(x.bvid)}</small>` : ""}
      </div>`).join("") || `<p class="sub">暂无阶段记录。</p>`;
    show(r);
    return;
  }

  const themeBtn = event.target.closest("[data-palette]");
  if (themeBtn) {
    const name = themeBtn.dataset.palette;
    if (name !== "custom" && !THEMES[name]) return;
    selectTheme(name);
    const saved = await saveTheme(name, name === "custom" ? customThemeValue() : undefined);
    toast(saved
      ? `配色：${name === "custom" ? "自定义" : THEMES[name]?.label || name}`
      : "配色已应用，但保存失败（刷新后会恢复）");
    return;
  }

  const act = event.target.closest("[data-act]")?.dataset.act;
  if (!act) return;

  try {
    if (act === "refresh") { await load(); toast("已刷新"); }
    else if (act === "health") await health();
    else if (act === "run") await runOnce();
    else if (act === "interest-add") {
      interestItems = collectInterest();
      interestItems.push({ keyword: "", quota: 3 });
      renderInterest();
    }
    else if (act === "interest-del") {
      event.target.closest(".interest-row")?.remove();
      interestItems = collectInterest();
      renderInterest();
    }
    else if (act === "interest-save") await saveInterest();
    else if (act === "unlimited") {
      const target = !unlimitedOn;
      const r = await post("unlimited", { enabled: target });
      unlimitedOn = Boolean(r.enabled);
      if (target) {
        toast(r.queued ? "无限模式已开启，当前任务结束后立即开始" : "无限模式已开启，正在刷取（token 消耗大）");
      } else {
        toast("无限模式已关闭");
      }
      await load(false);
    }
    else if (act === "custom-apply") {
      selectTheme("custom");
      const saved = await saveTheme("custom", customThemeValue());
      toast(saved ? "已应用自定义配色" : "已应用，但保存失败（刷新后会恢复）");
    }
  } catch (err) {
    show(err.stack || String(err));
    toast("操作失败");
  }
});

/* ---------------- boot ---------------- */

async function boot() {
  try {
    await bridge.ready();
    try {
      const theme = await get("theme");
      if (theme?.name === "custom" && theme.custom) {
        $("cBg").value = theme.custom.bg || "#e0e5ec";
        $("cDark").value = theme.custom.dark || "#b8bcc2";
        $("cLight").value = theme.custom.light || "#ffffff";
        $("cAccent").value = theme.custom.accent || "#6d5dfc";
      }
      selectTheme(theme?.name && (THEMES[theme.name] || theme.name === "custom") ? theme.name : "mist");
    } catch (err) {
      selectTheme("mist");
    }
    await load();
    setInterval(() => {
      if (!document.hidden) load(false).catch(() => {});
    }, 30000);
  } catch (err) {
    show(err.stack || String(err));
  }
}

boot();
