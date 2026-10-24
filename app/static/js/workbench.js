(function () {
  "use strict";

  const workspace = document.querySelector("[data-workbench]");
  if (!workspace) return;
  const search = workspace.querySelector("[data-workbench-search]");
  const job = workspace.querySelector("[data-workbench-job]");
  const cards = Array.from(workspace.querySelectorAll("[data-candidate-card]"));
  const rows = Array.from(workspace.querySelectorAll("[data-candidate-row]"));
  const empty = workspace.querySelector("[data-workbench-empty]");
  const reset = workspace.querySelector("[data-workbench-reset]");
  let view = "board";

  function showView(nextView) {
    view = nextView === "list" ? "list" : "board";
    workspace.querySelectorAll("[data-workbench-view]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.workbenchView === view));
    });
    workspace.querySelectorAll("[data-workbench-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.workbenchPanel !== view || !cards.some((card) => !card.hidden);
    });
    try { localStorage.setItem("talentflow.workbench.view", view); } catch (_) { /* View still works without storage. */ }
  }

  function filter() {
    const keyword = search.value.trim().toLocaleLowerCase();
    const jobId = job.value;
    const filtered = Boolean(keyword || jobId);
    const matches = (item) => (!jobId || item.dataset.job === jobId) && (!keyword || item.dataset.search.toLocaleLowerCase().includes(keyword));
    cards.concat(rows).forEach((item) => { item.hidden = !matches(item); });
    const count = cards.filter((card) => !card.hidden).length;
    workspace.querySelector("[data-active-count]").textContent = String(count);
    workspace.querySelector("[data-workbench-results]").textContent = filtered
      ? `找到 ${count} 条投递，共 ${cards.length} 条`
      : `${count} 条进行中的投递 · 按招聘阶段整理`;
    workspace.querySelectorAll("[data-workbench-column]").forEach((column) => {
      const visible = Array.from(column.querySelectorAll("[data-candidate-card]")).filter((card) => !card.hidden).length;
      column.querySelector("[data-column-count]").textContent = String(visible);
      const columnEmpty = column.querySelector("[data-column-empty]");
      columnEmpty.hidden = visible > 0;
      columnEmpty.textContent = filtered ? "此阶段没有匹配项" : `暂无${column.dataset.stageLabel}候选人`;
    });
    reset.hidden = !filtered;
    empty.hidden = count > 0;
    empty.querySelector("[data-empty-title]").textContent = filtered ? "没有找到匹配的候选人" : "暂无进行中的候选人";
    empty.querySelector("[data-empty-description]").textContent = filtered ? "换个姓名、岗位或负责人，或者清除筛选重试。" : "导入简历后，候选人会按招聘阶段显示在这里。";
    empty.querySelector("[data-empty-import]").hidden = filtered;
    showView(view);
  }

  search.addEventListener("input", filter);
  job.addEventListener("change", filter);
  reset.addEventListener("click", () => { search.value = ""; job.value = ""; filter(); search.focus(); });
  workspace.querySelectorAll("[data-workbench-view]").forEach((button) => {
    button.addEventListener("click", () => { showView(button.dataset.workbenchView); });
  });
  try { view = localStorage.getItem("talentflow.workbench.view") || "board"; } catch (_) { /* Use the default board. */ }
  filter();
})();
