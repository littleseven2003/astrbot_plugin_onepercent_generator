/**
 * 百分之一小作文生成器 - WebUI 黑名单管理
 */

const API_BASE = "/astrbot_plugin_onepercent_generator/blacklist";

// ─── 数据加载 ───

async function loadBlacklist() {
  try {
    const resp = await fetch(`${API_BASE}/get`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderList("group", data.groups || []);
    renderList("private", data.privates || []);
  } catch (e) {
    showToast("加载黑名单失败: " + e.message, "error");
  }
}

// ─── 渲染列表 ───

function renderList(type, items) {
  const listEl = document.getElementById(`${type}-list`);
  if (!items.length) {
    listEl.innerHTML = `<li class="empty-hint">暂无${type === "group" ? "群聊" : "私聊"}黑名单</li>`;
    return;
  }
  listEl.innerHTML = items
    .map(
      (id) => `
    <li>
      <span class="item-id">${type === "group" ? "群号" : "QQ号"}: ${escapeHtml(id)}</span>
      <button class="btn-remove" onclick="removeItem('${type}', '${escapeHtml(id)}')">移除</button>
    </li>`
    )
    .join("");
}

// ─── 添加 ───

async function addItem(type) {
  const input = document.getElementById(`${type}-input`);
  const id = input.value.trim();
  if (!id) {
    showToast("请输入ID", "error");
    return;
  }

  try {
    const resp = await fetch(`${API_BASE}/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, id }),
    });
    const data = await resp.json();
    if (data.error) {
      showToast(data.error, "error");
    } else {
      showToast(data.message || "操作成功", "success");
      input.value = "";
      await loadBlacklist();
    }
  } catch (e) {
    showToast("操作失败: " + e.message, "error");
  }
}

// ─── 移除 ───

async function removeItem(type, id) {
  try {
    const resp = await fetch(`${API_BASE}/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, id }),
    });
    const data = await resp.json();
    if (data.error) {
      showToast(data.error, "error");
    } else {
      showToast(data.message || "操作成功", "success");
      await loadBlacklist();
    }
  } catch (e) {
    showToast("操作失败: " + e.message, "error");
  }
}

// ─── Toast 提示 ───

function showToast(message, type = "info") {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className = `toast ${type}`;
  setTimeout(() => {
    toast.className = "toast hidden";
  }, 3000);
}

// ─── 工具函数 ───

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ─── 初始化 ───

document.addEventListener("DOMContentLoaded", () => {
  loadBlacklist();

  // 回车提交
  ["group", "private"].forEach((type) => {
    document.getElementById(`${type}-input`).addEventListener("keydown", (e) => {
      if (e.key === "Enter") addItem(type);
    });
  });
});
