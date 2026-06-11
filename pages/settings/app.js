/**
 * 百分之一小作文生成器 - 黑名单管理页面逻辑
 *
 * 通过 AstrBot 插件配置 API 实现黑名单数据的读写
 * 数据存储在插件 KV 存储中，key 为 "blacklist"
 */

const PLUGIN_NAME = "astrbot_plugin_onepercent_generator";
const API_BASE = `/api/plugin/${PLUGIN_NAME}`;

// DOM 元素
const groupList = document.getElementById("groupList");
const privateList = document.getElementById("privateList");
const statusMsg = document.getElementById("statusMsg");

/**
 * 显示状态消息
 */
function showStatus(text, type) {
  statusMsg.textContent = text;
  statusMsg.className = `status-msg ${type} show`;
  setTimeout(() => {
    statusMsg.className = "status-msg";
  }, 3000);
}

/**
 * 获取当前黑名单数据
 */
async function fetchBlacklist() {
  try {
    const resp = await fetch(`${API_BASE}/blacklist/get`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } catch (err) {
    console.error("获取黑名单失败:", err);
    // 返回默认空数据
    return { groups: [], privates: [] };
  }
}

/**
 * 添加黑名单
 */
async function addToBlacklist(targetId, targetType) {
  try {
    const resp = await fetch(`${API_BASE}/blacklist/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_id: targetId, target_type: targetType }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    showStatus(data.message || "操作成功", "success");
    renderBlacklist();
  } catch (err) {
    showStatus("操作失败: " + err.message, "error");
  }
}

/**
 * 移除黑名单
 */
async function removeFromBlacklist(targetId, targetType) {
  try {
    const resp = await fetch(`${API_BASE}/blacklist/remove`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_id: targetId, target_type: targetType }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    showStatus(data.message || "操作成功", "success");
    renderBlacklist();
  } catch (err) {
    showStatus("操作失败: " + err.message, "error");
  }
}

/**
 * 渲染黑名单列表
 */
async function renderBlacklist() {
  const data = await fetchBlacklist();

  // 渲染群聊黑名单
  renderList(groupList, data.groups || [], "group");

  // 渲染私聊黑名单
  renderList(privateList, data.privates || [], "private");
}

/**
 * 渲染单个列表
 */
function renderList(container, items, type) {
  if (!items || items.length === 0) {
    const label = type === "group" ? "群聊" : "私聊";
    container.innerHTML = `<li class="empty-tip">暂无${label}黑名单</li>`;
    return;
  }

  container.innerHTML = items
    .map(
      (id) => `
    <li>
      <span>${type === "group" ? "群号" : "QQ号"}: ${id}</span>
      <button onclick="handleRemove('${id}', '${type}')">移除</button>
    </li>
  `
    )
    .join("");
}

/**
 * 处理添加操作（从页面调用）
 */
function addBlacklist(type) {
  const inputId = type === "group" ? "addGroupInput" : "addPrivateInput";
  const input = document.getElementById(inputId);
  const value = input.value.trim();

  if (!value) {
    showStatus("请输入有效的ID", "error");
    return;
  }

  addToBlacklist(value, type);
  input.value = "";
}

/**
 * 处理移除操作（从页面调用）
 */
function handleRemove(targetId, type) {
  removeFromBlacklist(targetId, type);
}

/**
 * 支持 Enter 键提交
 */
document.getElementById("addGroupInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addBlacklist("group");
});

document.getElementById("addPrivateInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addBlacklist("private");
});

// 页面加载时渲染
renderBlacklist();
