let cursor = 0;

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

function metric(label, value, tone = "") {
  return `<article class="metric ${tone}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`;
}

let authenticated = false;

function controls(task) {
  if (!authenticated) return '<a class="login-link" href="/login">登录后控制</a>';
  const action = ["running", "queued", "verifying"].includes(task.state) ? "pause" : ["paused", "inbox", "ready", "needs_fix"].includes(task.state) ? "resume" : null;
  if (!action) return "";
  return `<button class="small" data-task="${escapeHtml(task.task_id)}" data-action="${action}">${action === "pause" ? "暂停" : "继续"}</button>`;
}

function renderTask(task) {
  const nodes = task.nodes.map((node) => {
    const executor = node.effective_executor || node.executor;
    const model = node.result?.actual_model || node.effective_model || node.model;
    return `<span class="node ${escapeHtml(node.state)}">${escapeHtml(node.node_id)} · ${escapeHtml(executor)} / ${escapeHtml(model)} · ${escapeHtml(node.state)}</span>`;
  }).join("");
  return `<article class="task"><div class="task-head"><div><h3>${escapeHtml(task.task_id)}</h3><p>${escapeHtml(task.contract.objective)}</p></div><div class="task-actions"><span class="pill ${escapeHtml(task.state)}">${escapeHtml(task.state)}</span>${controls(task)}</div></div><div class="nodes">${nodes}</div></article>`;
}

function renderAcceptance(check) {
  return `<article class="acceptance-check"><span class="pill ${escapeHtml(check.status)}">${escapeHtml(check.id)} · ${escapeHtml(check.status)}</span><div><strong>${escapeHtml(check.requirement)}</strong><p>${escapeHtml(check.evidence)}</p></div></article>`;
}

async function refreshSnapshot() {
  const response = await fetch("/api/snapshot", {cache: "no-store"});
  const data = await response.json();
  document.querySelector("#health").className = "pill ok";
  const build = data.build?.commit ? ` · ${data.build.commit.slice(0, 10)}` : "";
  document.querySelector("#health").textContent = `v${data.version}${build} · online`;
  const counts = data.health.task_counts || {};
  const active = (counts.running || 0) + (counts.queued || 0) + (counts.verifying || 0);
  const quota = data.quota;
  const quotaPolicy = data.quota_policy;
  const activeModels = data.health.active_models || {};
  const activeSonnet = Object.entries(activeModels).filter(([model]) => model.toLowerCase().includes("sonnet")).reduce((total, [, count]) => total + count, 0);
  const activeHigh = Object.entries(activeModels).filter(([model]) => !model.toLowerCase().includes("sonnet") && (model.toLowerCase().includes("opus") || model.toLowerCase().includes("fable"))).reduce((total, [, count]) => total + count, 0);
  const sonnetCap = quotaPolicy?.models?.sonnet?.max_concurrency ?? 0;
  const highCap = Math.max(quotaPolicy?.models?.opus?.max_concurrency ?? 0, quotaPolicy?.models?.fable?.max_concurrency ?? 0);
  const quotaZones = quotaPolicy?.zones ? `O ${quotaPolicy.zones.opus} · S ${quotaPolicy.zones.sonnet} · F ${quotaPolicy.zones.fable}` : "unknown";
  const acceptance = data.acceptance;
  authenticated = data.authenticated;
  document.querySelector("#metrics").innerHTML = [
    metric("任务总数", data.tasks.length),
    metric("运行/排队", active, active ? "running" : ""),
    metric("已验收", counts.accepted || 0, "ok"),
    metric("状态陈旧", data.diagnostics.stale_tasks.length, data.diagnostics.stale_tasks.length ? "error" : "ok"),
    metric("Claude 五小时剩余", quota?.five_hour_remaining == null ? "未知/禁用" : `${quota.five_hour_remaining}%`, quota?.five_hour_remaining > 25 ? "ok" : "error"),
    metric("Claude 调度区", quotaZones, quotaPolicy?.zone === "green" ? "ok" : quotaPolicy?.zone === "yellow" || quotaPolicy?.zone === "mixed" ? "pending" : "error"),
    metric("Claude 当前并发", `高阶 ${activeHigh}/${highCap} · S ${activeSonnet}/${sonnetCap}`, activeHigh || activeSonnet ? "running" : ""),
    metric("验收通过", `${acceptance.counts.ok}/12`, acceptance.complete ? "ok" : "pending"),
    metric("事件游标", data.health.cursor),
  ].join("");
  document.querySelector("#acceptance-summary").textContent = `${acceptance.counts.ok} ok · ${acceptance.counts.pending} pending · ${acceptance.counts.error} error`;
  document.querySelector("#acceptance").innerHTML = acceptance.checks.map(renderAcceptance).join("");
  document.querySelector("#tasks").innerHTML = data.tasks.length ? data.tasks.map(renderTask).join("") : '<p class="muted">暂无任务</p>';
  document.querySelectorAll("button[data-task]").forEach((button) => button.addEventListener("click", controlTask));
  document.querySelector("#updated").textContent = `刷新 ${new Date().toLocaleTimeString()}`;
  cursor = Math.max(cursor, data.health.cursor || 0);
  await recordPhoneObservation(data);
}

async function recordPhoneObservation(data) {
  if (!data.authenticated || !/(iphone|android|mobile)/i.test(navigator.userAgent) || sessionStorage.getItem("workbench-phone-observed")) return;
  let clientId = localStorage.getItem("workbench-phone-client-id");
  if (!clientId) {
    clientId = `phone-${crypto.randomUUID()}`;
    localStorage.setItem("workbench-phone-client-id", clientId);
  }
  const response = await fetch("/api/clients/observe", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({client_id: clientId, snapshot_cursor: data.health.cursor || 0}),
  });
  if (response.ok) sessionStorage.setItem("workbench-phone-observed", "true");
}

async function controlTask(event) {
  const button = event.currentTarget;
  button.disabled = true;
  const response = await fetch(`/api/tasks/${encodeURIComponent(button.dataset.task)}/control`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: button.dataset.action}),
  });
  if (!response.ok) alert((await response.json()).error || "控制失败");
  await refreshSnapshot();
}

async function refreshEvents() {
  const response = await fetch(`/api/events?after=${Math.max(0, cursor - 30)}`, {cache: "no-store"});
  const data = await response.json();
  const events = data.events || [];
  if (events.length) cursor = Math.max(cursor, ...events.map((event) => event.cursor));
  document.querySelector("#cursor").textContent = `cursor ${cursor}`;
  document.querySelector("#events").innerHTML = events.slice(-30).reverse().map((event) => `<div class="event"><span>#${event.cursor}</span><strong>${escapeHtml(event.event_type)}</strong><span>${escapeHtml(event.task_id || "system")} ${escapeHtml(event.node_id || "")}</span></div>`).join("") || '<p class="muted">暂无事件</p>';
}

async function tick() {
  try { await Promise.all([refreshSnapshot(), refreshEvents()]); }
  catch (error) {
    document.querySelector("#health").className = "pill error";
    document.querySelector("#health").textContent = "stale / disconnected";
  }
}

tick();
setInterval(tick, 5000);
