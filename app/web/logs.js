/* 请求日志页签：谁（IP / 昵称 / 邮箱）在什么时候打了哪个接口、结果如何。
   被刷 / 被打的时候靠它回答「是谁」—— 来源排行一眼看出打得最多的 IP，
   明细里能看到具体信息（哪个模型、哪把密钥、哪次登录）。
   公共能力（api / apiSend / toast / copy / fmtTime）由 main.js 通过 window.PA 暴露。 */
(function () {
  const PA = window.PA || {};
  const el = (id) => document.getElementById(id);

  // page 从 0 开始；limit 是每页条数（分页由服务端 limit/offset 支持）
  const state = { ip: "", model: "", total: 0, page: 0, limit: 200 };

  function badge(text, kind) {
    const node = document.createElement("span");
    node.className = "badge" + (kind ? " " + kind : "");
    node.textContent = text;
    return node;
  }

  /** 状态码配色：2xx 正常、4xx 警告、5xx 出错。 */
  function statusBadge(code) {
    const value = Number(code) || 0;
    const kind = value >= 500 ? "bad" : value >= 400 ? "warn" : value >= 200 ? "on" : "";
    return badge(String(value || "—"), kind);
  }

  function button(text, handler, className) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = className || "ghost";
    node.textContent = text;
    node.onclick = handler;
    return node;
  }

  function actions(...nodes) {
    const wrap = document.createElement("div");
    wrap.className = "actions";
    nodes.forEach((node) => wrap.appendChild(node));
    return wrap;
  }

  function cell(row, node, className) {
    const td = document.createElement("td");
    if (className) td.className = className;
    if (node instanceof Node) td.appendChild(node);
    else td.textContent = node == null ? "" : String(node);
    row.appendChild(td);
    return td;
  }

  function code(text) {
    const node = document.createElement("code");
    node.className = "key";
    node.textContent = text == null ? "" : String(text);
    return node;
  }

  /** 长文本（路径 / UA）折行难看，截断并用 title 显示全文。 */
  function clipped(text, max) {
    const node = document.createElement("span");
    const full = text == null ? "" : String(text);
    node.textContent = full.length > max ? full.slice(0, max) + "…" : full;
    if (full.length > max) node.title = full;
    return node;
  }

  /** 正文里那句「模型 xxx · 」不重复显示 —— 模型已经单独一列了。
      库里存的原文不动，hover 时（title）仍能看到完整那句。 */
  function tidyDetail(text, model) {
    const full = text == null ? "" : String(text);
    if (!model) return full;
    return full.replace(`模型 ${model} · `, "").replace(`模型 ${model}`, "") || full;
  }

  function who(nickname, email) {
    const node = document.createElement("span");
    if (!nickname && !email) {
      node.className = "muted";
      node.textContent = "未识别（没带凭证 / 凭证无效）";
      return node;
    }
    node.textContent = nickname || "—";
    if (email) {
      const mail = document.createElement("span");
      mail.className = "muted";
      // 邮箱太长会把「账号」这一列撑得很宽，截断显示、hover 看全
      mail.textContent = ` ${email.length > 18 ? email.slice(0, 18) + "…" : email}`;
      mail.title = email;
      node.appendChild(mail);
    }
    return node;
  }

  // ------------------------------------------------------------------ 查询串
  function filters() {
    const params = new URLSearchParams({
      q: el("logSearch").value.trim(),
      status: el("logStatus").value,
      service: el("logService").value,
      hours: el("logHours").value,
      ip: state.ip,
      model: state.model,
    });
    return params;
  }

  /** 当前生效的筛选提示：来源 IP 与模型是「点一下就只看它」的快捷筛选。 */
  function activeScopes() {
    const scopes = [];
    if (state.ip) scopes.push(`来源 ${state.ip}`);
    if (state.model) scopes.push(`模型 ${state.model}`);
    return scopes;
  }

  function showFilter() {
    const box = el("logFilter");
    const scopes = activeScopes();
    if (!scopes.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    box.textContent = `当前只看 ${scopes.join(" + ")} · `;
    box.appendChild(button("取消筛选", () => { state.ip = ""; state.model = ""; refresh(); }));
  }

  function showDropped(data) {
    const box = el("logDropped");
    const lost = data.dropped || 0;
    const pending = data.pending || 0;
    if (!lost) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.textContent =
      `⚠ 有 ${lost} 条请求因为日志队列写满被丢弃（队列里还有 ${pending} 条待落库）—— ` +
      `说明短时间内的请求量已经超过日志写入速度，很可能正在被刷。` +
      `当前保留策略：${data.retention_days ? data.retention_days + " 天" : "不按时间清理"}，` +
      `最多 ${data.max_rows || "不限"} 条。`;
  }

  // ------------------------------------------------------------------ 概览
  async function loadStats() {
    const data = await PA.api(`/api/admin/logs/stats?${filters().toString()}`);
    const summary = data.summary || {};
    el("logTotal").textContent = summary.total;
    el("logErrors").textContent = summary.errors;
    el("logIps").textContent = summary.ips;
    el("logUsers").textContent = summary.users;
    el("logToday").textContent = summary.today;
    el("logLimited").textContent = data.limited || 0;
    showDropped(summary);
    renderModels(data.models || []);

    const ipBody = el("logIpBody");
    ipBody.innerHTML = "";
    const ips = data.ips || [];
    el("logIpEmpty").hidden = ips.length > 0;
    ips.forEach((item) => {
      const row = document.createElement("tr");
      cell(row, code(item.ip || "—"));
      cell(row, String(item.hits), "col-id");
      cell(row, item.errors ? badge(String(item.errors), "warn") : badge("0", "on"), "col-status");
      cell(row, who(item.nickname, item.email));
      cell(row, PA.fmtTime(item.last_at));
      cell(row, actions(
        button("只看它", () => { state.ip = item.ip; state.page = 0; refresh(); }),
        button("复制 IP", () => PA.copy(item.ip || ""))
      ), "col-action");
      ipBody.appendChild(row);
    });

    const pathBody = el("logPathBody");
    pathBody.innerHTML = "";
    (data.paths || []).forEach((item) => {
      const row = document.createElement("tr");
      cell(row, code(item.path));
      cell(row, item.method, "col-id");
      cell(row, String(item.hits), "col-id");
      cell(row, item.errors ? badge(String(item.errors), "warn") : badge("0", "on"), "col-status");
      pathBody.appendChild(row);
    });
  }

  /** 模型限流排行：429 多的排最上面，一眼看出「现在卡住的是哪个模型」。 */
  function renderModels(models) {
    const body = el("logModelBody");
    body.innerHTML = "";
    el("logModelEmpty").hidden = models.length > 0;
    models.forEach((item) => {
      const row = document.createElement("tr");
      cell(row, code(item.model || "—"));
      cell(row, String(item.hits), "col-id");
      const limited = Number(item.limited) || 0;
      cell(row, limited ? badge(String(limited), "bad") : badge("0", "on"), "col-id");
      cell(row, String(item.errors || 0), "col-id");
      cell(row, item.last_limited_at ? PA.fmtTime(item.last_limited_at) : "—");
      cell(row, actions(
        button("只看它", () => { scopeModel(item.model); refresh(); }),
        button("只看限流", () => {
          scopeModel(item.model);
          el("logStatus").value = "429";
          refresh();
        })
      ), "col-action");
      body.appendChild(row);
    });
  }

  // ------------------------------------------------------------------ 明细
  async function loadLogs() {
    const params = filters();
    params.set("limit", String(state.limit));
    params.set("offset", String(state.page * state.limit));
    const data = await PA.api(`/api/admin/logs?${params.toString()}`);
    state.total = data.total;

    // 页码可能越界（换筛选条件 / 日志被清掉后页数变少）—— 退回最后一页，别停在一片空白
    const pages = Math.max(1, Math.ceil(data.total / state.limit));
    if (state.page >= pages) {
      state.page = pages - 1;
      return loadLogs();
    }

    el("logCount").textContent = `共 ${data.total} 条`;
    el("logPage").textContent = `第 ${state.page + 1} / ${pages} 页`;
    el("logPrev").disabled = state.page <= 0;
    el("logNext").disabled = state.page + 1 >= pages;

    const body = el("logBody");
    body.innerHTML = "";
    el("logEmpty").hidden = data.items.length > 0;

    data.items.forEach((item) => {
      const row = document.createElement("tr");
      cell(row, PA.fmtTime(item.created_at), "col-time");

      const source = document.createElement("div");
      source.appendChild(code(item.ip || "—"));
      if (item.forwarded && item.forwarded !== item.ip) {
        const via = document.createElement("div");
        via.className = "muted";
        via.textContent = `反代 ${item.forwarded}`;
        via.title = `直连地址 ${item.peer_ip || "—"}；X-Forwarded-For：${item.forwarded}`;
        source.appendChild(via);
      }
      cell(row, source);

      cell(row, who(item.nickname, item.email));

      const request = document.createElement("div");
      request.appendChild(badge(item.method, ""));
      request.appendChild(document.createTextNode(" "));
      request.appendChild(clipped(item.path, 36));

      cell(row, request);
      // 模型单独一列：被限流时「是哪个模型」是第一件要知道的事
      const modelNode = item.model
        ? button(item.model, () => { scopeModel(item.model); refresh(); }, "link")
        : document.createTextNode("—");
      cell(row, modelNode);
      cell(row, statusBadge(item.status), "col-status");

      const detail = document.createElement("div");
      const fullDetail = item.detail || "—";
      detail.title = fullDetail;      // hover 看完整原文（正文里去掉的那句模型在这里还在）
      detail.appendChild(clipped(tidyDetail(fullDetail, item.model), 52));
      if (item.duration_ms >= 0) {
        const cost = document.createElement("span");
        cost.className = "muted";
        cost.textContent = ` ${item.duration_ms} ms`;
        detail.appendChild(cost);
      }
      cell(row, detail, "col-detail");

      const meta = document.createElement("div");
      meta.appendChild(clipped(item.credential || "—", 18));
      const extra = document.createElement("div");
      extra.className = "muted";
      extra.textContent = `${item.client || "未知"} · ${item.service === "admin" ? "看板" : "主服务"}`;
      if (item.user_agent) extra.title = item.user_agent;
      meta.appendChild(extra);
      cell(row, meta);

      cell(row, actions(
        button("只看它", () => { state.ip = item.ip; state.page = 0; refresh(); }),
        button("复制 IP", () => PA.copy(item.ip || ""))
      ), "col-action");
      body.appendChild(row);
    });
  }

  // ------------------------------------------------------------------ 导出 / 清空
  async function exportCsv() {
    // 走 fetch 而不是直接跳链接：管理口令在请求头里，<a download> 带不上
    const res = await fetch(`/api/admin/logs/export?${filters().toString()}`, {
      headers: PA.state.token ? { "X-Admin-Token": PA.state.token } : {},
    });
    if (!res.ok) return PA.toast(`导出失败（HTTP ${res.status}）`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `request-logs-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "")}.csv`;
    link.click();
    URL.revokeObjectURL(url);
    PA.toast("已导出 CSV");
  }

  async function clearLogs() {
    const scopes = activeScopes();
    const scope = scopes.length ? `按${scopes.join(" + ")}筛出来的日志` : "全部请求日志";
    if (!window.confirm(`清空${scope}？清掉就查不到历史了（建议先导出 CSV 留证）。`)) return;
    try {
      const params = filters();
      params.set("hours", el("logHours").value);
      const data = await PA.apiSend("DELETE", `/api/admin/logs?${params.toString()}`);
      PA.toast(`已清掉 ${data.removed} 条`);
      state.ip = "";
      state.model = "";
      state.page = 0;
      el("logModel").value = "";
      await refresh();
    } catch (err) {
      if (err.message !== "unauthorized") PA.toast(err.message);
    }
  }

  // ------------------------------------------------------------------ 刷新
  async function refresh() {
    try {
      showFilter();
      await loadStats();
      await loadLogs();
    } catch (err) {
      if (err.message !== "unauthorized") console.error(err);
    }
  }

  function bind() {
    el("logExport").onclick = () => { exportCsv().catch((err) => PA.toast(err.message)); };
    el("logClear").onclick = clearLogs;
    // 换筛选条件就从第一页看起（否则容易停在一个空页上）
    ["logStatus", "logHours", "logService"].forEach((id) => {
      el(id).onchange = () => { state.ip = ""; state.page = 0; refresh(); };
    });
    let timer = null;
    el("logSearch").oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(() => { state.page = 0; refresh(); }, 250);
    };
    // 模型筛选：输入框与「只看它」按钮共用 state.model，所以两处都要同步到输入框
    let modelTimer = null;
    el("logModel").oninput = () => {
      clearTimeout(modelTimer);
      modelTimer = setTimeout(() => {
        state.model = el("logModel").value.trim();
        state.page = 0;
        refresh();
      }, 250);
    };

    // 翻页：只重拉明细（概览 / 排行与页码无关）
    el("logPrev").onclick = () => { state.page = Math.max(0, state.page - 1); loadLogs(); };
    el("logNext").onclick = () => { state.page += 1; loadLogs(); };
    el("logLimit").onchange = () => {
      state.limit = Number(el("logLimit").value) || 200;
      state.page = 0;
      loadLogs();
    };
  }

  /** 记住「只看这个模型」，并把输入框同步成它（否则界面上看不出正在筛）。 */
  function scopeModel(model) {
    state.model = model;
    state.page = 0;
    el("logModel").value = model || "";
  }

  bind();
  PA.refreshLogs = () => { refresh(); };
})();
