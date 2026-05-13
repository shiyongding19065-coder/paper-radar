const configPath = "data/config.json";
let configSha = null;
let config = null;

const $ = (id) => document.getElementById(id);

function lines(value) {
  return value.split("\n").map((v) => v.trim()).filter(Boolean);
}

function setStatus(message) {
  $("status").textContent = message;
}

function githubSettings() {
  const settings = {
    owner: $("owner").value.trim(),
    repo: $("repo").value.trim(),
    branch: $("branch").value.trim() || "main",
    token: $("token").value.trim(),
  };
  localStorage.setItem("paperRadarSettings", JSON.stringify(settings));
  return settings;
}

function loadSavedSettings() {
  const saved = JSON.parse(localStorage.getItem("paperRadarSettings") || "{}");
  $("owner").value = saved.owner || "";
  $("repo").value = saved.repo || "";
  $("branch").value = saved.branch || "main";
  $("token").value = saved.token || "";
}

async function githubFetch(path, options = {}) {
  const settings = githubSettings();
  if (!settings.owner || !settings.repo || !settings.token) {
    throw new Error("请先填写 GitHub owner、repo 和 token。");
  }
  const response = await fetch(`https://api.github.com/repos/${settings.owner}/${settings.repo}/${path}`, {
    ...options,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${settings.token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status}: ${body}`);
  }
  return response;
}

async function loadConfig() {
  setStatus("正在加载配置...");
  const settings = githubSettings();
  const response = await githubFetch(`contents/${configPath}?ref=${encodeURIComponent(settings.branch)}`);
  const data = await response.json();
  configSha = data.sha;
  config = JSON.parse(decodeURIComponent(escape(atob(data.content.replace(/\n/g, "")))));
  renderConfig();
  setStatus("配置已加载。");
}

async function saveConfig() {
  setStatus("正在保存配置...");
  config = collectConfig();
  const settings = githubSettings();
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(config, null, 2) + "\n")));
  const response = await githubFetch(`contents/${configPath}`, {
    method: "PUT",
    body: JSON.stringify({
      message: "Update Paper Radar config",
      content,
      sha: configSha,
      branch: settings.branch,
    }),
  });
  const data = await response.json();
  configSha = data.content.sha;
  setStatus("配置已保存。");
}

async function runNow() {
  setStatus("正在触发云端运行...");
  const settings = githubSettings();
  await githubFetch("actions/workflows/paper-radar.yml/dispatches", {
    method: "POST",
    body: JSON.stringify({
      ref: settings.branch,
      inputs: { force: "true" },
    }),
  });
  setStatus("已经触发。可以到 GitHub Actions 查看运行进度。");
}

function renderConfig() {
  $("scheduleEnabled").value = String(config.schedule?.enabled ?? true);
  $("mode").value = config.schedule?.mode || "weekly";
  $("dayOfWeek").value = config.schedule?.day_of_week || "monday";
  $("dayOfMonth").value = config.schedule?.day_of_month || 1;
  $("time").value = config.schedule?.time || "09:00";
  $("provider").value = config.email?.provider || "resend";
  $("from").value = config.email?.from || "";
  $("maxAttachment").value = config.email?.max_total_attachment_mb || 20;
  $("attachPdfs").value = String(config.email?.attach_pdfs ?? true);
  $("defaultRecipients").value = (config.email?.default_recipients || []).join("\n");
  renderTopics();
}

function renderTopics() {
  const container = $("topics");
  container.innerHTML = "";
  (config.topics || []).forEach((topic, index) => {
    const node = document.createElement("article");
    node.className = "topic";
    node.innerHTML = `
      <div class="topic-head">
        <h3>${escapeHtml(topic.name || "未命名主题")}</h3>
        <button class="remove" data-remove="${index}">删除</button>
      </div>
      <div class="grid four">
        <label>主题名<input data-topic="${index}" data-field="name" value="${escapeAttr(topic.name || "")}" /></label>
        <label>启用<select data-topic="${index}" data-field="enabled"><option value="true">启用</option><option value="false">暂停</option></select></label>
        <label>每次最多下载<input data-topic="${index}" data-field="max_downloads_per_run" type="number" min="0" max="100" value="${topic.max_downloads_per_run ?? 10}" /></label>
        <label>排序<select data-topic="${index}" data-field="sort_by"><option value="newest">最新优先</option><option value="title">标题排序</option></select></label>
      </div>
      <div class="grid four">
        <label>检索来源<textarea data-topic="${index}" data-field="sources">${escapeHtml((topic.sources || []).join("\n"))}</textarea></label>
        <label>关键词<textarea data-topic="${index}" data-field="keywords">${escapeHtml((topic.keywords || []).join("\n"))}</textarea></label>
        <label>排除关键词<textarea data-topic="${index}" data-field="exclude_keywords">${escapeHtml((topic.exclude_keywords || []).join("\n"))}</textarea></label>
        <label>主题收件人<textarea data-topic="${index}" data-field="recipients">${escapeHtml((topic.recipients || []).join("\n"))}</textarea></label>
      </div>
    `;
    container.appendChild(node);
    node.querySelector(`[data-field="enabled"]`).value = String(topic.enabled ?? true);
    node.querySelector(`[data-field="sort_by"]`).value = topic.sort_by || "newest";
  });
}

function collectConfig() {
  const next = structuredClone(config || {});
  next.schedule = {
    enabled: $("scheduleEnabled").value === "true",
    mode: $("mode").value,
    day_of_week: $("dayOfWeek").value,
    day_of_month: Number($("dayOfMonth").value || 1),
    time: $("time").value || "09:00",
    timezone: next.schedule?.timezone || "Europe/London",
  };
  next.email = {
    ...(next.email || {}),
    provider: $("provider").value,
    from: $("from").value.trim(),
    default_recipients: lines($("defaultRecipients").value),
    max_total_attachment_mb: Number($("maxAttachment").value || 20),
    attach_pdfs: $("attachPdfs").value === "true",
  };
  next.topics = [...document.querySelectorAll(".topic")].map((node, index) => {
    const get = (field) => node.querySelector(`[data-topic="${index}"][data-field="${field}"]`);
    return {
      name: get("name").value.trim() || "Untitled",
      enabled: get("enabled").value === "true",
      keywords: lines(get("keywords").value),
      exclude_keywords: lines(get("exclude_keywords").value),
      sources: lines(get("sources").value),
      recipients: lines(get("recipients").value),
      max_downloads_per_run: Number(get("max_downloads_per_run").value || 0),
      attach_pdfs: true,
      sort_by: get("sort_by").value,
    };
  });
  return next;
}

function addTopic() {
  config = collectConfig();
  config.topics.push({
    name: "New Topic",
    enabled: true,
    keywords: [],
    exclude_keywords: [],
    sources: ["arxiv", "openalex", "semantic_scholar", "crossref", "pubmed", "europe_pmc", "datacite", "doaj", "biorxiv", "medrxiv"],
    recipients: [],
    max_downloads_per_run: 10,
    attach_pdfs: true,
    sort_by: "newest",
  });
  renderTopics();
}

function removeTopic(index) {
  config = collectConfig();
  config.topics.splice(index, 1);
  renderTopics();
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

document.addEventListener("click", (event) => {
  const remove = event.target.closest("[data-remove]");
  if (remove) removeTopic(Number(remove.dataset.remove));
});

$("loadConfig").addEventListener("click", () => loadConfig().catch((err) => setStatus(err.message)));
$("saveConfig").addEventListener("click", () => saveConfig().catch((err) => setStatus(err.message)));
$("runNow").addEventListener("click", () => runNow().catch((err) => setStatus(err.message)));
$("addTopic").addEventListener("click", addTopic);

loadSavedSettings();
config = {
  schedule: { enabled: true, mode: "weekly", day_of_week: "monday", day_of_month: 1, time: "09:00", timezone: "Europe/London" },
  email: { provider: "resend", from: "", default_recipients: [], attach_pdfs: true, max_total_attachment_mb: 20 },
  search: { default_sources: ["arxiv", "openalex", "semantic_scholar", "crossref", "pubmed", "europe_pmc", "datacite", "doaj", "biorxiv", "medrxiv"], lookback_days: 14, max_results_per_source: 25, open_access_only: true },
  topics: [],
};
renderConfig();
