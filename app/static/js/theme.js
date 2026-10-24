(function () {
  "use strict";
  const key = "talentflow-theme";
  const labels = { light: "浅色", dark: "深色", system: "跟随系统" };
  const icons = { light: "sun", dark: "moon", system: "monitor" };
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  try { preference = localStorage.getItem(key) || "system"; } catch (_) { /* 私密浏览仍可切换当前页面。 */ }
  if (!Object.hasOwn(labels, preference)) preference = "system";
  function apply() {
    const theme = preference === "system" ? (media.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelectorAll("[data-theme-label]").forEach((el) => { el.textContent = labels[preference]; });
    document.querySelectorAll("[data-theme-icon]").forEach((el) => { el.setAttribute("href", "/static/icons.svg#" + icons[preference]); });
    document.querySelectorAll("[data-theme-choice]").forEach((el) => {
      el.setAttribute("aria-pressed", String(el.dataset.themeChoice === preference));
    });
  }
  function setPreference(value) {
    if (!Object.hasOwn(labels, value)) return;
    preference = value;
    try { localStorage.setItem(key, value); } catch (_) { /* 内存中的偏好继续生效。 */ }
    apply();
  }
  window.TalentFlowTheme = { setPreference };
  apply();
  media.addEventListener("change", apply);
  window.addEventListener("storage", (event) => {
    if (event.key !== key && event.key !== null) return;
    preference = Object.hasOwn(labels, event.newValue) ? event.newValue : "system";
    apply();
  });
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.addEventListener("click", () => {
        setPreference(button.dataset.themeChoice);
        const menu = button.closest("details");
        if (menu) { menu.open = false; menu.querySelector("summary").focus(); }
      });
    });
    document.addEventListener("click", (event) => {
      document.querySelectorAll(".theme-menu[open]").forEach((menu) => {
        if (!menu.contains(event.target)) menu.open = false;
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      document.querySelectorAll(".theme-menu[open]").forEach((menu) => {
        menu.open = false;
        menu.querySelector("summary").focus();
      });
    });
  });
})();
