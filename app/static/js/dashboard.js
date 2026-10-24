(function () {
  "use strict";
  const root = document.querySelector("[data-dashboard]");
  if (!root) return;
  const indicator = document.querySelector("[data-live-status]");
  const updatedAt = document.querySelector("[data-updated-at]");
  const stages = [["applications", "投递"], ["screened", "初筛"], ["interviews", "面试"], ["offers", "Offer"], ["onboarded", "入职"]];
  let refreshing = false;
  function textElement(tag, value, className) {
    const el = document.createElement(tag);
    el.textContent = value;
    if (className) el.className = className;
    return el;
  }
  function render(data) {
    document.querySelectorAll("[data-metric]").forEach(el => {
      const value = data[el.dataset.metric];
      if (typeof value === "number") el.textContent = value.toLocaleString("zh-CN");
    });
    const funnel = data.funnel;
    const max = Math.max(1, ...stages.map(([key]) => funnel[key]));
    stages.forEach(([key]) => {
      const bar = root.querySelector('[data-funnel="' + key + '"]');
      bar.style.height = Math.max(38, Math.round(funnel[key] / max * 142)) + "px";
      bar.querySelector("[data-funnel-value]").textContent = funnel[key];
    });
    root.querySelector(".funnel").setAttribute("aria-label", "招聘漏斗：" + stages.map(([key, label]) => label + " " + funnel[key]).join("，"));
    const jobs = root.querySelector("[data-dashboard-jobs]");
    jobs.replaceChildren();
    data.jobs.forEach(job => {
      const row = document.createElement("tr");
      row.append(textElement("td", job.name), textElement("td", job.count));
      jobs.appendChild(row);
    });
    if (!data.jobs.length) {
      const row = document.createElement("tr");
      const cell = textElement("td", "暂无岗位数据");
      cell.colSpan = 2;
      row.appendChild(cell);
      jobs.appendChild(row);
    }
    const channels = root.querySelector("[data-dashboard-channels]");
    channels.replaceChildren();
    const channelMax = Math.max(1, ...data.channels.map(item => item.count));
    data.channels.forEach(item => {
      const row = document.createElement("div");
      row.className = "bar-row";
      const track = document.createElement("div");
      track.className = "bar-track";
      track.setAttribute("aria-hidden", "true");
      const fill = document.createElement("div");
      fill.className = "bar-fill";
      fill.style.width = item.count / channelMax * 100 + "%";
      track.appendChild(fill);
      row.append(textElement("span", item.name), track, textElement("span", item.count, "bar-value"));
      channels.appendChild(row);
    });
    if (!data.channels.length) channels.appendChild(textElement("p", "暂无渠道数据", "muted"));
    channels.appendChild(textElement("p", "统计范围：当前全部来源记录。", "muted"));
    indicator.classList.remove("is-offline", "status-warning");
    indicator.classList.add("status-success");
    indicator.textContent = "实时更新中";
    updatedAt.textContent = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date());
  }
  async function refresh() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    try {
      const response = await fetch(root.dataset.endpoint, { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      render(await response.json());
    } catch (_) {
      indicator.classList.remove("status-success");
      indicator.classList.add("is-offline", "status-warning");
      indicator.textContent = "更新失败 · 保留上次数据";
    } finally { refreshing = false; }
  }
  refresh();
  window.setInterval(refresh, Math.max(5000, Number(root.dataset.interval || 10000)));
  document.addEventListener("visibilitychange", refresh);
})();
