/* 用户看板：加载统计与用户列表，支持搜索、密码显隐、自动刷新。 */
(function () {
  const TOKEN_KEY = "paper_agent_admin_token";
  const REFRESH_MS = 15000;

  const el = (id) => document.getElementById(id);
  const state = { token: localStorage.getItem(TOKEN_KEY) || "", timer: null, users: [], revealed: false, tab: "users" };

  // 供 keys.js（大模型与密钥页签）复用：请求 / 提示 / 复制 / 格式化
  window.PA = window.PA || {};
  window.PA.state = state;
  window.PA.el = el;

  function headers() {
    return state.token ? { "X-Admin-Token": state.token } : {};
  }

  function toast(text) {
    const box = el("toast");
    box.textContent = text;
    box.hidden = false;
    setTimeout(() => { box.hidden = true; }, 1800);
  }

  async function api(path) {
    const res = await fetch(path, { headers: headers() });
    if (res.status === 401) {
      state.token = "";
      localStorage.removeItem(TOKEN_KEY);
      openTokenModal("管理口令不正确");
      throw new Error("unauthorized");
    }
    if (!res.ok) throw new Error(`${path} -> ${res.status}`);
    return res.json();
  }

  /** 带请求体的调用（POST / PATCH / DELETE），失败时抛出服务端的 detail 文案 */
  async function apiSend(method, path, body) {
    const options = { method, headers: { ...headers(), "Content-Type": "application/json" } };
    if (body !== undefined) options.body = JSON.stringify(body);
    const res = await fetch(path, options);
    if (res.status === 401) {
      state.token = "";
      localStorage.removeItem(TOKEN_KEY);
      openTokenModal("管理口令不正确");
      throw new Error("unauthorized");
    }
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : `${method} ${path} -> ${res.status}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function openTokenModal(message) {
    el("tokenModal").hidden = false;
    if (message) {
      el("tokenError").textContent = message;
      el("tokenError").hidden = false;
    }
    el("tokenInput").focus();
    el("tokenInput").select();
  }

  function closeTokenModal() {
    el("tokenModal").hidden = true;
    el("tokenError").hidden = true;
  }

  function fmtTime(value) {
    if (!value) return "—";
    return value.replace("T", " ").slice(0, 19);
  }

  function initial(name) {
    return (name || "?").trim().slice(0, 1).toUpperCase();
  }

  // 与桌面端内置头像同色，看板里用首字母色块近似展示
  const AVATAR_COLORS = {
    user: "#7C8AA0",
    sparkles: "#8E7CC3",
    book: "#6E9C7A",
    terminal: "#5F7E8C",
    globe: "#7E93C4",
    moon: "#8A7FA8",
    pencil: "#B58A5F",
    image: "#C08A8A",
  };

  function avatarColor(spec) {
    if (!spec || !spec.startsWith("icon:")) return "#9A9A92";
    return AVATAR_COLORS[spec.slice(5)] || "#9A9A92";
  }

  function renderRows() {
    const tbody = el("tbody");
    tbody.innerHTML = "";
    el("empty").hidden = state.users.length > 0;

    state.users.forEach((user) => {
      const tr = document.createElement("tr");

      const id = document.createElement("td");
      id.className = "col-id";
      id.textContent = user.id;
      tr.appendChild(id);

      const nick = document.createElement("td");
      const wrap = document.createElement("div");
      wrap.className = "user";
      const avatar = document.createElement("span");
      avatar.className = "avatar";
      avatar.style.background = avatarColor(user.avatar);
      avatar.textContent = initial(user.nickname);
      const name = document.createElement("span");
      name.textContent = user.nickname;
      wrap.append(avatar, name);
      nick.appendChild(wrap);
      tr.appendChild(nick);

      const mail = document.createElement("td");
      const mailText = document.createElement("span");
      mailText.textContent = user.email;
      const copyMail = document.createElement("button");
      copyMail.className = "ghost";
      copyMail.type = "button";
      copyMail.textContent = "复制";
      copyMail.onclick = () => copy(user.email);
      mail.append(mailText, document.createTextNode(" "), copyMail);
      tr.appendChild(mail);

      const pwd = document.createElement("td");
      const box = document.createElement("div");
      box.className = "pwd";
      const code = document.createElement("code");
      const shown = state.revealed;
      code.textContent = shown ? user.password || "—" : mask(user.password);
      const toggle = document.createElement("button");
      toggle.className = "ghost";
      toggle.type = "button";
      toggle.textContent = shown ? "隐藏" : "显示";
      toggle.onclick = () => {
        const next = code.textContent.includes("•") || !state.revealed;
        code.textContent = next ? user.password || "—" : mask(user.password);
        toggle.textContent = next ? "隐藏" : "显示";
      };
      box.append(code, toggle);
      pwd.appendChild(box);
      tr.appendChild(pwd);

      const status = document.createElement("td");
      status.className = "col-status";
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = user.status === "active" ? "正常" : user.status;
      status.appendChild(badge);
      tr.appendChild(status);

      const created = document.createElement("td");
      created.textContent = fmtTime(user.created_at);
      tr.appendChild(created);

      const last = document.createElement("td");
      last.textContent = fmtTime(user.last_login_at);
      last.className = user.last_login_at ? "" : "muted";
      tr.appendChild(last);

      // 操作：直接给这个用户签发一把代理密钥（大模型请求要靠它）
      const action = document.createElement("td");
      action.className = "col-action";
      const issue = document.createElement("button");
      issue.className = "ghost";
      issue.type = "button";
      issue.textContent = "生成密钥";
      issue.onclick = () => issueKey(user);
      action.appendChild(issue);
      tr.appendChild(action);

      tbody.appendChild(tr);
    });
  }

  /** 给某个用户签一把代理密钥，并把结果展示出来。 */
  async function issueKey(user) {
    try {
      const created = await apiSend("POST", "/api/admin/user-keys", {
        user_id: user.id,
        label: `${user.nickname} 的密钥`,
      });
      toast(`已为 ${user.nickname} 生成密钥`);
      if (window.PA.onKeyIssued) window.PA.onKeyIssued(created);
    } catch (err) {
      if (err.message !== "unauthorized") toast(err.message);
    }
  }

  function mask(value) {
    if (!value) return "—";
    return "•".repeat(Math.min(value.length, 8));
  }

  function copy(text) {
    navigator.clipboard?.writeText(text).then(
      () => toast("已复制"),
      () => toast("复制失败")
    );
  }

  async function loadStats() {
    try {
      const data = await api("/api/admin/stats");
      el("statUsers").textContent = data.users;
      el("statToday").textContent = data.today_users;
      el("statCodes").textContent = data.codes_today;
      el("statTokens").textContent = data.active_tokens;
    } catch (err) {
      if (err.message !== "unauthorized") console.error(err);
    }
  }

  async function loadUsers() {
    const q = el("search").value.trim();
    try {
      const data = await api(`/api/admin/users?q=${encodeURIComponent(q)}&limit=200`);
      state.users = data;
      el("countText").textContent = `共 ${data.length} 位用户`;
      renderRows();
    } catch (err) {
      if (err.message !== "unauthorized") console.error(err);
    }
  }

  function refreshAll() {
    if (state.tab === "users") {
      loadStats();
      loadUsers();
    } else if (state.tab === "llm" && window.PA.refreshKeys) {
      window.PA.refreshKeys();
    } else if (state.tab === "logs" && window.PA.refreshLogs) {
      window.PA.refreshLogs();
    }
  }

  /** 切换页签：用户列表 / 大模型与密钥 / 请求日志。 */
  function switchTab(name) {
    state.tab = name;
    document.querySelectorAll(".tab").forEach((node) => {
      node.classList.toggle("active", node.dataset.tab === name);
    });
    el("viewUsers").hidden = name !== "users";
    el("viewLlm").hidden = name !== "llm";
    el("viewLogs").hidden = name !== "logs";
    refreshAll();
  }

  function bind() {
    el("refreshBtn").onclick = refreshAll;
    let timer = null;
    el("search").oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(loadUsers, 250);
    };
    el("showAllPwd").onchange = (e) => {
      state.revealed = e.target.checked;
      renderRows();
    };
    el("autoRefresh").onchange = (e) => {
      if (state.timer) clearInterval(state.timer);
      state.timer = e.target.checked ? setInterval(refreshAll, REFRESH_MS) : null;
    };
    el("tokenCancel").onclick = () => {
      localStorage.removeItem(TOKEN_KEY);
      state.token = "";
      closeTokenModal();
    };
    el("tokenSubmit").onclick = () => {
      const value = el("tokenInput").value.trim();
      if (!value) {
        el("tokenError").textContent = "请输入口令";
        el("tokenError").hidden = false;
        return;
      }
      state.token = value;
      localStorage.setItem(TOKEN_KEY, value);
      closeTokenModal();
      refreshAll();
    };
    el("tokenInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") el("tokenSubmit").click();
    });
    document.querySelectorAll(".tab").forEach((node) => {
      node.onclick = () => switchTab(node.dataset.tab);
    });
  }

  // 交给 keys.js 的公共能力
  window.PA.api = api;
  window.PA.apiSend = apiSend;
  window.PA.toast = toast;
  window.PA.copy = copy;
  window.PA.fmtTime = fmtTime;
  window.PA.switchTab = switchTab;

  async function boot() {
    bind();
    try {
      const cfg = await fetch("/api/admin/config").then((r) => r.json());
      if (cfg.require_token && !state.token) {
        openTokenModal("");
        return;
      }
      if (!cfg.show_password) {
        el("showAllPwd").disabled = true;
        el("showAllPwd").parentElement.title = "服务端未开启明文保存（STORE_PLAIN_PASSWORD=false）";
      }
    } catch (err) {
      console.error(err);
    }
    refreshAll();
  }

  boot();
})();
