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

  document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const opening = !body.classList.contains("sidebar-open");
      body.classList.toggle("sidebar-open", opening);
      document.querySelectorAll("[data-sidebar-toggle]").forEach((item) => {
        if (item.hasAttribute("aria-expanded")) item.setAttribute("aria-expanded", String(opening));
      });
      if (opening) {
        sidebarTrigger = button;
        window.setTimeout(() => sidebar?.focus(), 0);
      } else {
        sidebarTrigger?.focus();
      }
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

  document.querySelectorAll("[data-filter-table]").forEach((input) => {
    input.addEventListener("input", () => {
      const table = document.querySelector(input.dataset.filterTable);
      if (!table) return;
      const keyword = input.value.trim().toLowerCase();
      table.querySelectorAll("tbody tr").forEach((row) => {
        row.hidden = keyword && !row.textContent.toLowerCase().includes(keyword);
      });
    });
  });

  document.querySelectorAll("[data-table-select]").forEach((select) => {
    select.addEventListener("change", () => {
      const table = document.querySelector(select.dataset.tableSelect);
      if (!table) return;
      const column = Number(select.dataset.column || 0);
      const value = select.value.trim().toLowerCase();
      table.querySelectorAll("tbody tr").forEach((row) => {
        const cell = row.cells[column];
        row.hidden = Boolean(value && cell && !cell.textContent.trim().toLowerCase().includes(value));
      });
    });
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
        const input = document.querySelector(button.dataset.copyFallback || "");
        if (input) {
          input.focus();
          input.select();
        }
        showToast("请手动复制", "浏览器阻止自动复制，已选中链接。", "info");
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
      const originalText = submitButton?.textContent;
      if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = "处理中…";
      }

      try {
        const response = await fetch(form.action, {
          method: form.method,
          body: new FormData(form),
          headers: { Accept: "application/json", "X-CSRF-Token": csrfToken }
        });
        let data = {};

        try {
          data = await response.json();
        } catch (_) {
          // Use the fallback message when the response has no JSON body.
        }

        if (!response.ok) {
          const detail = data.detail;
          const message = typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? detail.map((item) => item.msg || String(item)).join("；")
              : data.message || `请求失败（${response.status}）`;
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
        window.AdminUI.showToast(
          "请求失败",
          error.message || "网络异常，请稍后重试。",
          "error"
        );
      } finally {
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
    stack.appendChild(toast);
    window.setTimeout(() => toast.remove(), 4200);
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && body.classList.contains("sidebar-open")) {
      body.classList.remove("sidebar-open");
      document.querySelectorAll("[data-sidebar-toggle][aria-expanded]").forEach((item) => item.setAttribute("aria-expanded", "false"));
      sidebarTrigger?.focus();
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
