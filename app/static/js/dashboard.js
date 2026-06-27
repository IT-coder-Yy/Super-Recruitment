(function () {
  "use strict";

  const root = document.querySelector("[data-dashboard]");
  if (!root) return;

  const endpoint = root.dataset.endpoint || "/api/dashboard/summary";
  const interval = Math.max(5000, Number(root.dataset.interval || 15000));
  const indicator = document.querySelector("[data-live-status]");
  const updatedAt = document.querySelector("[data-updated-at]");

  const aliases = {
    new_today: ["new_today", "today_new", "new_candidates", "today_candidates"],
    pending_review: ["pending_review", "pending_evaluations", "review_pending"],
    pending_schedule: ["pending_schedule", "schedule_pending"],
    pending_feedback: ["pending_feedback", "feedback_pending"],
    overdue: ["overdue", "overdue_tasks", "overdue_count"],
    applications: ["applications", "applied", "total_applications"],
    screened: ["screened", "screening", "screened_count"],
    interviews: ["interviews", "interview", "interview_count"],
    offers: ["offers", "offer", "offer_count"],
    onboarded: ["onboarded", "hires", "joined"]
  };

  function pick(source, names) {
    for (const name of names) {
      if (source && source[name] !== undefined && source[name] !== null) return source[name];
    }
    return undefined;
  }

  function normalize(payload) {
    const source = payload && payload.data ? payload.data : (payload || {});
    const metrics = source.metrics || source.summary || source;
    const funnel = source.funnel || source.recruitment_funnel || {};
    const result = {};
    Object.entries(aliases).forEach(([key, names]) => {
      result[key] = pick(key in funnel ? funnel : metrics, names);
      if (result[key] === undefined) result[key] = pick(funnel, names);
    });
    result.jobs = source.jobs || source.job_progress || [];
    result.channels = source.channels || source.channel_stats || [];
    return result;
  }

  function render(data) {
    document.querySelectorAll("[data-metric]").forEach((element) => {
      const value = data[element.dataset.metric];
      if (value !== undefined) element.textContent = Number(value).toLocaleString("zh-CN");
    });

    const funnelValues = ["applications", "screened", "interviews", "offers", "onboarded"];
    const max = Math.max(1, ...funnelValues.map((key) => Number(data[key] || 0)));
    funnelValues.forEach((key) => {
      const bar = document.querySelector(`[data-funnel="${key}"]`);
      if (!bar || data[key] === undefined) return;
      const value = Number(data[key] || 0);
      bar.style.height = `${Math.max(38, Math.round((value / max) * 142))}px`;
      const label = bar.querySelector("[data-funnel-value]");
      if (label) label.textContent = value.toLocaleString("zh-CN");
    });

    if (indicator) {
      indicator.classList.remove("is-offline");
      indicator.textContent = "实时更新中";
    }
    if (updatedAt) {
      updatedAt.textContent = new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit", minute: "2-digit", second: "2-digit"
      }).format(new Date());
    }
  }

  async function refresh() {
    try {
      const response = await fetch(endpoint, {
        headers: { Accept: "application/json" },
        cache: "no-store"
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(normalize(await response.json()));
    } catch (error) {
      if (indicator) {
        indicator.classList.add("is-offline");
        indicator.textContent = "静态数据 · 等待接口";
      }
      root.dataset.lastError = error.message;
    }
  }

  refresh();
  window.setInterval(refresh, interval);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
})();
