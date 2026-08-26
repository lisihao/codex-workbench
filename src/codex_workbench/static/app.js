let cursor = 0;

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

function metric(label, value, tone = "") {
  return `<article class="metric ${tone}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`;
}

let authenticated = false;

function controls(task) {
  if (!authenticated) return '<a class="login-link" href="/login">登录后控制</a>';
  const action = ["running", "queued", "verifying"].includes(task.state) ? "pause" : ["paused", "inbox", "ready", "needs_fix"].includes(task.state) ? "resume" : null;
  const lifecycle = action ? `<button class="small" data-task="${escapeHtml(task.task_id)}" data-action="${action}" data-revision="${escapeHtml(task.state_revision)}">${action === "pause" ? "暂停" : "继续"}</button>` : "";
  const priority = !["accepted", "cancelled"].includes(task.state)
    ? `<button class="small quiet" data-task="${escapeHtml(task.task_id)}" data-action="set_priority" data-priority="${Math.max(-10, task.priority - 1)}" data-revision="${escapeHtml(task.state_revision)}">优先级−</button><button class="small quiet" data-task="${escapeHtml(task.task_id)}" data-action="set_priority" data-priority="${Math.min(10, task.priority + 1)}" data-revision="${escapeHtml(task.state_revision)}">优先级＋</button>`
    : "";
  return lifecycle + priority;
}

function renderTask(task) {
  const activeNode = task.nodes.find((node) => ["running", "blocked", "indeterminate"].includes(node.state));
  const nextNode = task.nodes.find((node) => node.state === "pending");
  const phase = activeNode ? `${activeNode.node_id} · ${activeNode.state}` : task.state;
  const nextAction = task.blocker || (nextNode ? `下一节点：${nextNode.node_id}` : task.verdict || "等待状态推进");
  const steering = task.steering?.length ? task.steering.at(-1).instruction : "无追加指令";
  const nodes = task.nodes.map((node) => {
    const executor = node.effective_executor || node.executor;
    const model = node.result?.actual_model || node.effective_model || node.model;
    return `<span class="node ${escapeHtml(node.state)}">${escapeHtml(node.node_id)} · ${escapeHtml(executor)} / ${escapeHtml(model)} · ${escapeHtml(node.state)}</span>`;
  }).join("");
  const artifacts = task.nodes.flatMap((node) => Object.entries(node.result?.artifacts || {}).map(([name, ref]) => {
    const label = `${node.node_id} · ${name}`;
    return authenticated
      ? `<a class="artifact" href="/api/artifacts/${encodeURIComponent(ref)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`
      : `<span class="artifact disabled">${escapeHtml(label)}</span>`;
  })).join("");
  const steer = authenticated && !["accepted", "cancelled"].includes(task.state)
    ? `<div class="steering"><input maxlength="500" data-steer-input="${escapeHtml(task.task_id)}" placeholder="给后续 attempt 补充一句指令（不扩大 scope）"><button class="small" data-steer-task="${escapeHtml(task.task_id)}" data-revision="${escapeHtml(task.state_revision)}">发送</button></div>`
    : "";
  return `<article class="task"><div class="task-head"><div><h3>${escapeHtml(task.task_id)}</h3><p>${escapeHtml(task.contract.objective)}</p></div><div class="task-actions"><span class="pill ${escapeHtml(task.state)}">${escapeHtml(task.state)}</span>${controls(task)}</div></div><div class="task-brief"><span>阶段 <strong>${escapeHtml(phase)}</strong></span><span>下一步 <strong>${escapeHtml(nextAction)}</strong></span><span>优先级 / 更新 <strong>${escapeHtml(task.priority)} · ${escapeHtml(new Date(task.updated_at).toLocaleString())}</strong></span><span>最新短指令 <strong>${escapeHtml(steering)}</strong></span></div><div class="nodes">${nodes}</div>${artifacts ? `<div class="artifacts">${artifacts}</div>` : ""}${steer}</article>`;
}

function renderAcceptance(check) {
  return `<article class="acceptance-check"><span class="pill ${escapeHtml(check.status)}">${escapeHtml(check.id)} · ${escapeHtml(check.status)}</span><div><strong>${escapeHtml(check.requirement)}</strong><p>${escapeHtml(check.evidence)}</p></div></article>`;
}

function renderApproval(approval) {
  const request = approval.request || {};
  const buttons = authenticated
    ? `<div class="approval-actions"><button class="small" data-approval="${escapeHtml(approval.approval_id)}" data-decision="retry" data-revision="${escapeHtml(approval.task_revision)}">重试</button><button class="small warning" data-approval="${escapeHtml(approval.approval_id)}" data-decision="fail" data-revision="${escapeHtml(approval.task_revision)}">标记失败</button><button class="small danger" data-approval="${escapeHtml(approval.approval_id)}" data-decision="cancel" data-revision="${escapeHtml(approval.task_revision)}">取消任务</button></div>`
    : '<a class="login-link" href="/login">登录后审批</a>';
  return `<article class="approval"><div><span class="pill needs_approval">${escapeHtml(approval.kind)}</span><h3>${escapeHtml(approval.task_id)} · ${escapeHtml(request.node_id)}</h3><p>${escapeHtml(request.reason)}</p><span class="muted">attempt ${escapeHtml(request.attempt)} · revision ${escapeHtml(approval.task_revision)}</span></div>${buttons}</article>`;
}

function alertText(alert) {
  if (alert.event_type === "approval.requested") return "需要你的审批";
  if (alert.event_type === "node.indeterminate") return "执行结果不确定";
  if (alert.event_type === "node.blocked") return "节点已阻塞";
  if (alert.event_type === "node.routed") return `执行器已切换：${alert.payload?.reason || "路由策略"}`;
  if (alert.event_type === "coordinator.started") return "Mac mini 协调器已启动";
  if (alert.event_type === "coordinator.stopped") return "Mac mini 协调器已停止";
  if (alert.event_type === "task.state_changed") return `任务进入 ${alert.payload?.to}`;
  return alert.event_type;
}

function renderAlert(alert) {
  const tone = ["approval.requested", "node.indeterminate", "node.blocked", "coordinator.stopped"].includes(alert.event_type) ? "error" : alert.payload?.to === "accepted" ? "ok" : "pending";
  return `<article class="alert"><span class="pill ${tone}">#${escapeHtml(alert.cursor)}</span><div><strong>${escapeHtml(alertText(alert))}</strong><p>${escapeHtml(alert.task_id || "system")} ${escapeHtml(alert.node_id || "")} · ${escapeHtml(new Date(alert.created_at).toLocaleString())}</p></div></article>`;
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
  const approvals = data.approvals || [];
  const alerts = data.alerts || [];
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
    metric("待处理审批", approvals.length, approvals.length ? "error" : "ok"),
    metric("事件游标", data.health.cursor),
  ].join("");
  document.querySelector("#approval-summary").textContent = approvals.length ? `${approvals.length} pending` : "0 pending";
  document.querySelector("#approvals").innerHTML = approvals.length ? approvals.map(renderApproval).join("") : '<p class="muted">当前没有待处理审批</p>';
  document.querySelector("#alerts").innerHTML = alerts.length ? alerts.slice(-8).reverse().map(renderAlert).join("") : '<p class="muted">当前没有重要提醒</p>';
  document.querySelector("#acceptance-summary").textContent = `${acceptance.counts.ok} ok · ${acceptance.counts.pending} pending · ${acceptance.counts.error} error`;
  document.querySelector("#acceptance").innerHTML = acceptance.checks.map(renderAcceptance).join("");
  document.querySelector("#tasks").innerHTML = data.tasks.length ? data.tasks.map(renderTask).join("") : '<p class="muted">暂无任务</p>';
  document.querySelectorAll("button[data-task]").forEach((button) => button.addEventListener("click", controlTask));
  document.querySelectorAll("button[data-steer-task]").forEach((button) => button.addEventListener("click", steerTask));
  document.querySelectorAll("button[data-approval]").forEach((button) => button.addEventListener("click", decideApproval));
  document.querySelector("#updated").textContent = `刷新 ${new Date().toLocaleTimeString()}`;
  cursor = Math.max(cursor, data.health.cursor || 0);
  await recordPhoneObservation(data);
  notifyNewAlerts(alerts);
}

function notifyNewAlerts(alerts) {
  if (!("Notification" in window) || Notification.permission !== "granted" || !alerts.length) return;
  const latest = Math.max(...alerts.map((alert) => Number(alert.cursor)));
  const stored = localStorage.getItem("workbench-alert-cursor");
  if (stored == null) {
    localStorage.setItem("workbench-alert-cursor", String(latest));
    return;
  }
  alerts.filter((alert) => Number(alert.cursor) > Number(stored)).forEach((alert) => {
    try { new Notification("Codex Workbench", {body: `${alertText(alert)} · ${alert.task_id || "system"}`}); }
    catch (error) { console.warn("notification unavailable", error); }
  });
  localStorage.setItem("workbench-alert-cursor", String(latest));
}

async function decideApproval(event) {
  const button = event.currentTarget;
  document.querySelectorAll(`button[data-approval="${CSS.escape(button.dataset.approval)}"]`).forEach((item) => { item.disabled = true; });
  const response = await fetch(`/api/approvals/${encodeURIComponent(button.dataset.approval)}/decide`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({decision: button.dataset.decision, expected_revision: Number(button.dataset.revision)}),
  });
  if (!response.ok) alert((await response.json()).error || "审批失败");
  await refreshSnapshot();
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
  const body = {action: button.dataset.action, expected_revision: Number(button.dataset.revision)};
  if (button.dataset.action === "set_priority") body.priority = Number(button.dataset.priority);
  const response = await fetch(`/api/tasks/${encodeURIComponent(button.dataset.task)}/control`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  if (!response.ok) alert((await response.json()).error || "控制失败");
  await refreshSnapshot();
}

async function steerTask(event) {
  const button = event.currentTarget;
  const input = document.querySelector(`input[data-steer-input="${CSS.escape(button.dataset.steerTask)}"]`);
  const instruction = input.value.trim();
  if (!instruction) return;
  button.disabled = true;
  const response = await fetch(`/api/tasks/${encodeURIComponent(button.dataset.steerTask)}/steer`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({instruction, expected_revision: Number(button.dataset.revision)}),
  });
  if (!response.ok) alert((await response.json()).error || "补充指令失败");
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

document.querySelector("#enable-notifications").addEventListener("click", async (event) => {
  if (!("Notification" in window)) {
    event.currentTarget.textContent = "当前浏览器不支持";
    return;
  }
  const permission = await Notification.requestPermission();
  event.currentTarget.textContent = permission === "granted" ? "前台通知已启用" : "通知未授权";
});
