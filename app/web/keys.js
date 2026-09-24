/* 大模型与密钥页签：内置模型、供应商密钥池、用户代理密钥。
   公共能力（api / apiSend / toast / copy / fmtTime）由 main.js 通过 window.PA 暴露。 */
(function () {
  const PA = window.PA || {};
  const el = (id) => document.getElementById(id);
  const esc = (text) => String(text == null ? "" : text);

  // 模型类型：chat 对话 / image 文生图 / video 文生视频
  const KIND_LABELS = { chat: "对话", image: "文生图", video: "文生视频" };
  const kindLabel = (kind) => KIND_LABELS[kind] || KIND_LABELS.chat;

  function badge(text, kind) {
    const node = document.createElement("span");
    node.className = "badge" + (kind ? " " + kind : "");
    node.textContent = text;
    return node;
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

  function code(text) {
    const node = document.createElement("code");
    node.className = "key";
    node.textContent = esc(text);
    return node;
  }

  function cell(row, node) {
    const td = document.createElement("td");
    if (typeof node === "string") td.textContent = node;
    else if (node) td.appendChild(node);
    row.appendChild(td);
    return td;
  }

  async function guard(fn) {
    try {
      await fn();
    } catch (err) {
      if (err.message !== "unauthorized") PA.toast(err.message);
    }
  }

  // ------------------------------------------------------------------ 统计
  async function loadStats() {
    await guard(async () => {
      const data = await PA.api("/api/admin/llm-stats");
      el("llmModels").textContent = data.models;
      el("llmKeys").textContent = data.user_keys;
      el("llmActiveKeys").textContent = data.active_user_keys;
      el("llmTtl").textContent = data.default_ttl_days ? `${data.default_ttl_days} 天` : "不过期";
    });
  }

  // ------------------------------------------------------------------ 内置模型
  function renderModels(models) {
    {
      const body = el("modelBody");
      body.innerHTML = "";
      el("modelEmpty").hidden = models.length > 0;

      models.forEach((model) => {
        const row = document.createElement("tr");
        cell(row, code(model.model_id));
        cell(row, badge(kindLabel(model.kind),
                        model.kind === "chat" ? "" : "warn"));
        cell(row, model.name || "—");
        cell(row, model.desc || "—");
        cell(row, code(model.base_url));
        cell(row, badge(model.enabled ? "启用" : "停用", model.enabled ? "on" : "off"));
        cell(row, badge(`${model.key_count} 个`, model.key_count ? "on" : "warn"));

        const toggle = button(model.enabled ? "停用" : "启用", () =>
          guard(async () => {
            await PA.apiSend("PATCH", `/api/admin/models/${encodeURIComponent(model.model_id)}`, {
              enabled: !model.enabled,
            });
            await refresh();
          })
        );
        // 名称 / 备注 / 地址 / 启用状态都在弹框里一次改完（名称与备注会下发给桌面端）
        const edit = button("编辑", () => openModelModal(model));
        const remove = button("删除", () =>
          guard(async () => {
            if (!window.confirm(`删除模型 ${model.model_id}？连同它的供应商密钥一起移除。`)) return;
            await PA.apiSend("DELETE", `/api/admin/models/${encodeURIComponent(model.model_id)}`);
            PA.toast("已删除");
            await refresh();
          })
        );
        cell(row, actions(toggle, edit, remove));
        body.appendChild(row);
      });
    }
  }

  // ------------------------------------------------------------------ 编辑弹框
  let editing = null;      // 当前正在编辑的模型

  function showModalError(id, text) {
    const box = el(id);
    box.textContent = text || "";
    box.hidden = !text;
  }

  function openModelModal(model) {
    editing = model;
    el("mmId").value = model.model_id;
    el("mmKind").value = model.kind === "image" ? "image" : "chat";
    el("mmName").value = model.name || "";
    el("mmDesc").value = model.desc || "";
    el("mmUrl").value = model.base_url || "";
    el("mmEnabled").checked = !!model.enabled;
    showModalError("mmError", "");
    el("modelModal").hidden = false;
    el("mmName").focus();
    el("mmName").select();
  }

  function closeModelModal() {
    el("modelModal").hidden = true;
    editing = null;
  }

  async function saveModelModal() {
    if (!editing) return;
    const name = el("mmName").value.trim();
    const desc = el("mmDesc").value.trim();
    const baseUrl = el("mmUrl").value.trim();
    if (!name) return showModalError("mmError", "请填显示名（桌面端下拉框里就显示它）");
    if (!/^https?:\/\//i.test(baseUrl)) {
      return showModalError("mmError", "Base URL 需要以 http:// 或 https:// 开头");
    }
    try {
      await PA.apiSend("PATCH", `/api/admin/models/${encodeURIComponent(editing.model_id)}`, {
        name,
        desc,
        kind: el("mmKind").value,
        base_url: baseUrl,
        enabled: el("mmEnabled").checked,
      });
    } catch (err) {
      if (err.message !== "unauthorized") showModalError("mmError", err.message);
      return;
    }
    closeModelModal();
    PA.toast("已保存，客户端下次启动 / 登录生效");
    await refresh();
  }

  async function addModel() {
    const modelId = el("mId").value.trim();
    const kind = el("mKind").value;
    const name = el("mName").value.trim();
    const desc = el("mDesc").value.trim();
    const baseUrl = el("mUrl").value.trim();
    if (!modelId || !baseUrl) return PA.toast("模型 ID 与 Base URL 都要填");
    await guard(async () => {
      await PA.apiSend("POST", "/api/admin/models", {
        model_id: modelId,
        kind,
        name,
        desc,
        base_url: baseUrl,
      });
      el("mId").value = el("mName").value = el("mDesc").value = el("mUrl").value = "";
      el("mKind").value = "chat";
      PA.toast(
        kind === "image"
          ? "文生图模型已添加，去密钥池配好 Key 桌面端就能出图"
          : "模型已添加"
      );
      await refresh();
    });
  }

  // ------------------------------------------------------------------ 供应商密钥
  async function loadProviderKeys() {
    await guard(async () => {
      const keys = await PA.api("/api/admin/provider-keys");
      const body = el("pkBody");
      body.innerHTML = "";
      el("pkEmpty").hidden = keys.length > 0;

      keys.forEach((item) => {
        const row = document.createElement("tr");
        cell(row, String(item.id));
        cell(row, code(item.model_id));
        cell(row, code(item.api_key));
        cell(row, item.label || "—");
        const state = item.status !== "active"
          ? badge("停用", "off")
          : item.cooling ? badge("冷却中", "warn") : badge("可用", "on");
        cell(row, state);
        cell(row, PA.fmtTime(item.last_used_at));

        // 列表里是打码值，复制时按 id 单独取明文（新模型复用旧 Key 时最省事）
        const copy = button("复制", () =>
          guard(async () => {
            const data = await PA.api(`/api/admin/provider-keys/${item.id}/secret`);
            if (!data || !data.api_key) return PA.toast("取不到密钥明文");
            PA.copy(data.api_key);
          })
        );
        const toggle = button(item.status === "active" ? "停用" : "启用", () =>
          guard(async () => {
            await PA.apiSend("PATCH", `/api/admin/provider-keys/${item.id}`, {
              status: item.status === "active" ? "disabled" : "active",
            });
            await refresh();
          })
        );
        const remove = button("删除", () =>
          guard(async () => {
            if (!window.confirm("删除这把供应商密钥？")) return;
            await PA.apiSend("DELETE", `/api/admin/provider-keys/${item.id}`);
            PA.toast("已删除");
            await refresh();
          })
        );
        cell(row, actions(copy, toggle, remove));
        body.appendChild(row);
      });
    });
  }

  async function addProviderKey() {
    const modelId = el("pkModel").value;
    const apiKey = el("pkKey").value.trim();
    const label = el("pkLabel").value.trim();
    if (!modelId || !apiKey) return PA.toast("请选择模型并填写 API Key");
    await guard(async () => {
      await PA.apiSend("POST", "/api/admin/provider-keys", {
        model_id: modelId,
        api_key: apiKey,
        label,
      });
      el("pkKey").value = el("pkLabel").value = "";
      PA.toast("密钥已加入池子");
      await refresh();
    });
  }

  async function fillModelSelect(models) {
    const select = el("pkModel");
    const current = select.value;
    select.innerHTML = "";
    models.forEach((model) => {
      const option = document.createElement("option");
      option.value = model.model_id;
      option.textContent =
        `${model.name || model.model_id}（${model.model_id}）· ${kindLabel(model.kind)}`;
      select.appendChild(option);
    });
    if (current && models.some((m) => m.model_id === current)) select.value = current;
  }

  // ------------------------------------------------------------------ 续期弹框
  let renewing = null;     // 当前正在续期的用户密钥

  function openRenewModal(item) {
    renewing = item;
    el("rnWho").textContent =
      `${item.nickname || "—"}（#${item.user_id}）· ${item.key}` +
      (item.expires_at ? ` · 当前到期 ${PA.fmtTime(item.expires_at)}` : " · 当前永不过期");
    el("rnDays").value = "30";
    showModalError("rnError", "");
    el("renewModal").hidden = false;
    el("rnDays").focus();
    el("rnDays").select();
  }

  function closeRenewModal() {
    el("renewModal").hidden = true;
    renewing = null;
  }

  async function saveRenewModal() {
    if (!renewing) return;
    const text = el("rnDays").value.trim();
    if (text === "") return showModalError("rnError", "请填天数");
    const days = Number(text);
    if (!Number.isFinite(days) || days < 0) {
      return showModalError("rnError", "天数请填 0 或正整数");
    }
    const expires = days === 0
      ? ""
      : new Date(Date.now() + days * 86400000).toISOString().slice(0, 19);
    try {
      await PA.apiSend("PATCH", `/api/admin/user-keys/${renewing.id}`, { expires_at: expires });
    } catch (err) {
      if (err.message !== "unauthorized") showModalError("rnError", err.message);
      return;
    }
    closeRenewModal();
    PA.toast(days === 0 ? "已改成永不过期" : `已续期 ${days} 天`);
    await refresh();
  }

  // ------------------------------------------------------------------ 用户密钥
  async function loadUserKeys() {
    await guard(async () => {
      const keys = await PA.api("/api/admin/user-keys");
      const body = el("ukBody");
      body.innerHTML = "";
      el("ukEmpty").hidden = keys.length > 0;

      keys.forEach((item) => {
        const row = document.createElement("tr");
        cell(row, String(item.id));
        cell(row, `${item.nickname || "—"}（#${item.user_id}）`);
        cell(row, code(item.key));
        cell(row, item.label || "—");

        const state = item.status !== "active"
          ? badge("停用", "off")
          : item.expired ? badge("已过期", "warn") : badge("可用", "on");
        cell(row, state);
        cell(row, item.expires_at ? PA.fmtTime(item.expires_at) : "永不过期");
        cell(row, PA.fmtTime(item.last_used_at));

        const copy = button("复制", () => PA.copy(item.key));
        const renew = button("续期", () => openRenewModal(item));
        const toggle = button(item.status === "active" ? "停用" : "启用", () =>
          guard(async () => {
            await PA.apiSend("PATCH", `/api/admin/user-keys/${item.id}`, {
              status: item.status === "active" ? "disabled" : "active",
            });
            await refresh();
          })
        );
        const remove = button("删除", () =>
          guard(async () => {
            if (!window.confirm("删除这把用户密钥？桌面端将无法再调用内置模型。")) return;
            await PA.apiSend("DELETE", `/api/admin/user-keys/${item.id}`);
            PA.toast("已删除");
            await refresh();
          })
        );
        cell(row, actions(copy, renew, toggle, remove));
        body.appendChild(row);
      });
    });
  }

  async function addUserKey() {
    const userId = Number(el("ukUser").value.trim());
    const label = el("ukLabel").value.trim();
    const daysText = el("ukDays").value.trim();
    if (!Number.isFinite(userId) || userId <= 0) return PA.toast("请填用户 ID");
    const payload = { user_id: userId, label };
    if (daysText !== "") payload.days = Number(daysText);
    await guard(async () => {
      const created = await PA.apiSend("POST", "/api/admin/user-keys", payload);
      el("ukUser").value = el("ukLabel").value = el("ukDays").value = "";
      PA.copy(created.key);
      PA.toast("已生成密钥并复制到剪贴板");
      await refresh();
    });
  }

  // ------------------------------------------------------------------ 模型健康 / 限流
  /** 一眼看出**是哪个模型在挨限流**：模型 + 它的密钥池状态 + 近期 429 次数。 */
  async function loadModelHealth() {
    const items = await PA.api("/api/admin/model-health").catch(() => []);
    const body = el("mhBody");
    body.innerHTML = "";
    el("mhEmpty").hidden = items.length > 0;

    items.forEach((item) => {
      const row = document.createElement("tr");
      const name = document.createElement("div");
      name.appendChild(code(item.model_id));
      const label = document.createElement("div");
      label.className = "muted";
      label.textContent = item.name || "";
      name.appendChild(label);
      cell(row, name);
      cell(row, item.enabled ? badge(kindLabel(item.kind), "on") : badge("已停用", "off"));

      // 密钥池：可用 / 总；有在冷却的说明刚被限流过，下一轮会自动换一把
      const pool = document.createElement("div");
      const usable = Number(item.usable_keys) || 0;
      const total = Number(item.keys) || 0;
      pool.appendChild(badge(`${usable}/${total}`, total === 0 ? "bad" : usable ? "on" : "warn"));
      if (item.cooling_keys) {
        const cooling = document.createElement("div");
        cooling.className = "muted";
        cooling.textContent = `冷却中 ${item.cooling_keys} 把${item.cooling_detail ? "：" + item.cooling_detail : ""}`;
        cooling.title = "命中限流 / 服务异常后，这把 Key 会被暂时跳过，下一轮自动换一把";
        pool.appendChild(cooling);
      }
      cell(row, pool);

      const limited = Number(item.limited) || 0;
      cell(row, limited ? badge(String(limited), "bad") : badge("0", "on"), "col-id");
      cell(row, item.last_limited_at ? PA.fmtTime(item.last_limited_at) : "—");

      const error = document.createElement("div");
      error.className = "muted";
      const text = item.last_error || "—";
      error.textContent = text.length > 70 ? text.slice(0, 70) + "…" : text;
      error.title = item.last_error || "";
      cell(row, error);
      body.appendChild(row);
    });
  }

  // ------------------------------------------------------------------ 刷新
  async function refresh() {
    await loadStats();
    // 模型列表只取一次：既用来填下拉框，也用来渲染表格
    const models = await PA.api("/api/admin/models").catch(() => []);
    fillModelSelect(models);
    renderModels(models);
    await loadProviderKeys();
    await loadUserKeys();
    await loadModelHealth();
  }

  PA.refreshKeys = () => { guard(refresh); };
  PA.onKeyIssued = (created) => {
    if (created && created.key) PA.copy(created.key);
    PA.switchTab("llm");
    PA.toast("已生成密钥并复制到剪贴板");
  };

  /** 弹框通用交互：点遮罩或按 Esc 关闭，输入框里回车即保存。 */
  function bindModal(maskId, onClose, onSave) {
    const mask = el(maskId);
    mask.addEventListener("mousedown", (event) => {
      if (event.target === mask) onClose();       // 只有点在遮罩本身上才关
    });
    mask.querySelectorAll("input").forEach((input) => {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") onSave();
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !mask.hidden) onClose();
    });
  }

  function bind() {
    el("mAdd").onclick = () => guard(addModel);
    el("pkAdd").onclick = () => guard(addProviderKey);
    el("ukAdd").onclick = () => guard(addUserKey);
    el("ukUser").addEventListener("keydown", (e) => {
      if (e.key === "Enter") guard(addUserKey);
    });

    el("mmCancel").onclick = closeModelModal;
    el("mmSave").onclick = saveModelModal;
    bindModal("modelModal", closeModelModal, saveModelModal);

    el("rnCancel").onclick = closeRenewModal;
    el("rnSave").onclick = saveRenewModal;
    el("renewModal").querySelectorAll(".chips button").forEach((chip) => {
      chip.onclick = () => {
        el("rnDays").value = chip.dataset.days;
        el("rnDays").focus();
      };
    });
    bindModal("renewModal", closeRenewModal, saveRenewModal);
  }

  bind();
})();
