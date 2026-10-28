(function () {
  "use strict";

  const body = document.body;
  const sidebar = document.getElementById("sidebar");
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  let sidebarTrigger = null;

  if (csrfToken) {
    document.querySelectorAll("form").forEach((form) => {
      if (form.querySelector('input[name="csrf_token"]')) return;
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "csrf_token";
      input.value = csrfToken;
      form.prepend(input);
    });
  }

  const mobileNavigation = window.matchMedia("(max-width: 979px)");
  function setSidebar(opening) {
    body.classList.toggle("sidebar-open", opening);
    document.querySelectorAll("[data-sidebar-toggle][aria-expanded]").forEach((item) => {
      item.setAttribute("aria-expanded", String(opening));
      item.setAttribute("aria-label", opening ? "关闭导航" : "打开导航");
    });
    const pageShell = document.querySelector(".page-shell");
    if (pageShell) pageShell.inert = opening && mobileNavigation.matches;
    if (sidebar) sidebar.inert = !opening && mobileNavigation.matches;
    if (opening) sidebar?.focus();
    else sidebarTrigger?.focus();
  }
  if (sidebar) sidebar.inert = mobileNavigation.matches;
  mobileNavigation.addEventListener("change", () => setSidebar(false));
  document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const opening = !body.classList.contains("sidebar-open");
      if (opening) {
        sidebarTrigger = button;
      }
      setSidebar(opening);
    });
  });

  document.querySelectorAll("[data-tabs]").forEach((tabs) => {
    const buttons = tabs.querySelectorAll("[data-tab]");
    const scope = tabs.parentElement;
    buttons.forEach((button, index) => {
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(button.classList.contains("is-active")));
      button.addEventListener("click", () => {
        buttons.forEach((item) => item.classList.toggle("is-active", item === button));
        scope.querySelectorAll("[data-tab-panel]").forEach((panel) => {
          panel.classList.toggle("is-active", panel.dataset.tabPanel === button.dataset.tab);
        });
        buttons.forEach((item) => item.setAttribute("aria-selected", String(item === button)));
      });
      button.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let targetIndex = index;
        if (event.key === "ArrowRight") targetIndex = (index + 1) % buttons.length;
        if (event.key === "ArrowLeft") targetIndex = (index - 1 + buttons.length) % buttons.length;
        if (event.key === "Home") targetIndex = 0;
        if (event.key === "End") targetIndex = buttons.length - 1;
        buttons[targetIndex].focus();
        buttons[targetIndex].click();
      });
    });
  });

  document.querySelectorAll("[data-password-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.passwordToggle);
      if (!input) return;
      input.type = input.type === "password" ? "text" : "password";
      button.textContent = input.type === "password" ? "显示" : "隐藏";
    });
  });

  document.querySelectorAll("[data-file-zone]").forEach((zone) => {
    const input = zone.querySelector('input[type="file"]');
    const target = document.querySelector(zone.dataset.fileZone);
    if (!input || !target) return;

    ["dragenter", "dragover"].forEach((eventName) => {
      zone.addEventListener(eventName, () => zone.classList.add("is-dragging"));
    });
    ["dragleave", "drop"].forEach((eventName) => {
      zone.addEventListener(eventName, () => zone.classList.remove("is-dragging"));
    });
    input.addEventListener("change", () => {
      target.innerHTML = "";
      Array.from(input.files).forEach((file) => {
        const item = document.createElement("div");
        item.className = "file-item";
        const size = file.size > 1048576
          ? `${(file.size / 1048576).toFixed(1)} MB`
          : `${Math.max(1, Math.round(file.size / 1024))} KB`;
        item.innerHTML = `<span>${escapeHtml(file.name)}</span><span class="muted">${size}</span>`;
        target.appendChild(item);
      });
    });
  });

  const tableFilters = new Map();
  document.querySelectorAll("[data-filter-table], [data-table-select]").forEach((control) => {
    const table = document.querySelector(control.dataset.filterTable || control.dataset.tableSelect);
    if (!table) return;
    if (!tableFilters.has(table)) tableFilters.set(table, []);
    tableFilters.get(table).push(control);
    if (control.dataset.filterTable && window.location.pathname === "/candidates") {
      control.value = new URLSearchParams(window.location.search).get("q") || control.value;
    }
  });
  tableFilters.forEach((controls, table) => {
    const rows = Array.from(table.querySelectorAll("tbody tr")).filter((row) => row.cells.length > 1);
    if (!rows.length) return;
    const empty = document.createElement("tr");
    empty.className = "table-filter-empty";
    const cell = document.createElement("td");
    cell.colSpan = table.querySelectorAll("thead th").length || rows[0].cells.length;
    cell.textContent = "没有匹配结果，请调整搜索或筛选条件。";
    empty.appendChild(cell);
    table.tBodies[0].appendChild(empty);
    function filterRows() {
      let visible = 0;
      rows.forEach((row) => {
        row.hidden = !controls.every((control) => {
          const value = control.value.trim().toLowerCase();
          if (!value) return true;
          const target = control.dataset.filterTable ? row : row.cells[Number(control.dataset.column || 0)];
          const text = (target?.textContent || "").trim().toLowerCase();
          return control.dataset.filterTable ? text.includes(value) : text === value;
        });
        if (!row.hidden) visible += 1;
      });
      empty.hidden = visible !== 0;
      document.querySelectorAll("[data-filter-count]").forEach((counter) => {
        if (counter.dataset.filterCount === "#" + table.id) counter.textContent = visible;
      });
    }
    controls.forEach((control) => control.addEventListener(control.matches("select") ? "change" : "input", filterRows));
    filterRows();
  });

  document.querySelectorAll("[data-confirm]").forEach((button) => {
    button.addEventListener("click", (event) => {
      if (!window.confirm(button.dataset.confirm || "确认执行此操作？")) {
        event.preventDefault();
      }
    });
  });

  document.querySelectorAll("[data-copy-value]").forEach((button) => {
    button.addEventListener("click", async () => {
      const value = button.dataset.copyValue || "";
      try {
        await navigator.clipboard.writeText(value);
        showToast("已复制", "链接已复制到剪贴板。", "success");
      } catch (_) {
        const input = button.dataset.copyFallback ? document.querySelector(button.dataset.copyFallback) : null;
        if (input) {
          input.focus();
          input.select();
        }
        showToast("请手动复制", input ? "浏览器阻止自动复制，已选中链接。" : "浏览器阻止自动复制，请选中页面中的链接复制。", "info");
      }
    });
  });

  document.querySelectorAll("form[data-demo-submit]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.dataset.nativeSubmit === "true") return;
      event.preventDefault();
      showToast("操作已提交", form.dataset.demoSubmit || "请求已进入处理队列。", "success");
    });
  });

  document.querySelectorAll("form[action^='/api/']:not([data-demo-submit])").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitButton = form.querySelector('[type="submit"]');
      if (form.dataset.submitting === "true") return;
      form.dataset.submitting = "true";
      form.setAttribute("aria-busy", "true");
      const originalText = submitButton?.textContent;
      const errorBox = form.querySelector("[data-form-error]") || document.createElement("p");
      errorBox.dataset.formError = "";
      errorBox.className = "notice notice-danger";
      errorBox.setAttribute("role", "alert");
      errorBox.hidden = true;
      if (!errorBox.isConnected) form.appendChild(errorBox);
      if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = "处理中…";
      }

      try {
        const response = await fetch(form.getAttribute("action"), {
          method: form.getAttribute("method") || "POST",
          body: new FormData(form),
          headers: { Accept: "application/json", "X-CSRF-Token": csrfToken }
        });
        let data = {};

        try {
          data = await response.json();
        } catch (_) {
          // Use the fallback message when the response has no JSON body.
        }

        if (!response.ok || data.ok === false || response.redirected && response.url.includes("/login")) {
          const detail = data.detail;
          const message = typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? detail.map((item) => item.msg || String(item)).join("；")
              : data.message || (response.status === 401 || response.redirected ? "登录已过期，请重新登录后重试。" : `请求失败（${response.status}）`);
          errorBox.textContent = message;
          errorBox.hidden = false;
          window.AdminUI.showToast("操作失败", message, "error");
          return;
        }

        window.AdminUI.showToast(
          "操作成功",
          data.message || data.detail || "操作已完成。",
          "success"
        );
        window.setTimeout(() => {
          if (data.redirect) {
            window.location.href = data.redirect;
          } else {
            window.location.reload();
          }
        }, 600);
      } catch (error) {
        errorBox.textContent = "网络异常，操作未确认完成。请检查连接后重试。";
        errorBox.hidden = false;
        window.AdminUI.showToast(
          "请求失败",
          error.message || "网络异常，请稍后重试。",
          "error"
        );
      } finally {
        delete form.dataset.submitting;
        form.removeAttribute("aria-busy");
        if (submitButton) {
          submitButton.disabled = false;
          submitButton.textContent = originalText;
        }
      }
    });
  });

  document.querySelectorAll("form[data-unsaved-warning]").forEach((form) => {
    let dirty = false;
    form.addEventListener("input", () => { dirty = true; });
    form.addEventListener("submit", () => { dirty = false; });
    window.addEventListener("beforeunload", (event) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
  });

  document.querySelectorAll("[data-toast]").forEach((button) => {
    button.addEventListener("click", () => {
      showToast(
        button.dataset.toastTitle || "操作完成",
        button.dataset.toast || "已保存当前更改。",
        button.dataset.toastType || "success"
      );
    });
  });

  function showToast(title, message, type) {
    const stack = document.getElementById("toastStack");
    if (!stack) return;
    const toast = document.createElement("div");
    toast.className = `toast is-${type || "info"}`;
    toast.innerHTML = `<div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(message)}</p></div>`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    const dismiss = document.createElement("button");
    dismiss.className = "icon-button";
    dismiss.type = "button";
    dismiss.setAttribute("aria-label", "关闭提示");
    dismiss.innerHTML = '<svg class="icon" aria-hidden="true"><use href="/static/icons.svg#close"/></svg>';
    dismiss.addEventListener("click", () => toast.remove());
    toast.appendChild(dismiss);
    stack.appendChild(toast);
    if (type !== "error") window.setTimeout(() => toast.remove(), 6000);
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && body.classList.contains("sidebar-open")) {
      setSidebar(false);
    }
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.closest("input, textarea, select, [contenteditable]")) {
      const search = document.querySelector(".sidebar-search input");
      if (search && !mobileNavigation.matches) { event.preventDefault(); search.focus(); }
    }
    if (event.key === "Tab" && body.classList.contains("sidebar-open") && sidebar) {
      const items = Array.from(sidebar.querySelectorAll("a, button, input, select, [tabindex='0']")).filter((el) => !el.disabled && el.getClientRects().length);
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === sidebar)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;"
    })[character]);
  }

  window.AdminUI = { showToast, escapeHtml };
})();
