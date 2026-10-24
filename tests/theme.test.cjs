const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../app/static/js/theme.js"), "utf8");

function boot({ stored = null, dark = false, blocked = false } = {}) {
  const events = {};
  const system = { matches: dark, addEventListener: (name, fn) => { events.system = fn; } };
  const document = { documentElement: { dataset: {}, style: {} }, querySelectorAll: () => [], addEventListener: () => {} };
  const storage = new Map(stored ? [["talentflow-theme", stored]] : []);
  const localStorage = {
    getItem: (key) => { if (blocked) throw Error("blocked"); return storage.get(key) || null; },
    setItem: (key, value) => { if (blocked) throw Error("blocked"); storage.set(key, value); }
  };
  const window = { matchMedia: () => system, addEventListener: (name, fn) => { events[name] = fn; } };
  vm.runInNewContext(source, { document, window, localStorage });
  return { document, window, system, events, storage };
}
test("首次跟随系统，并实时响应系统外观改变", () => {
  const app = boot({ dark: true });
  assert.equal(app.document.documentElement.dataset.theme, "dark");
  app.system.matches = false;
  app.events.system();
  assert.equal(app.document.documentElement.dataset.theme, "light");
});
test("手动主题持久保存，优先于系统；重新选择系统后恢复跟随", () => {
  const app = boot({ stored: "light", dark: true });
  assert.equal(app.document.documentElement.dataset.theme, "light");
  app.window.TalentFlowTheme.setPreference("dark");
  assert.equal(app.storage.get("talentflow-theme"), "dark");
  app.system.matches = false;
  app.events.system();
  assert.equal(app.document.documentElement.dataset.theme, "dark");
  app.window.TalentFlowTheme.setPreference("system");
  assert.equal(app.document.documentElement.dataset.theme, "light");
  assert.equal(app.storage.get("talentflow-theme"), "system");
});
test("异常偏好与禁用存储不影响页面和当前主题切换", () => {
  const app = boot({ stored: "__proto__", dark: true });
  assert.equal(app.document.documentElement.dataset.theme, "dark");
  const privateApp = boot({ blocked: true });
  privateApp.window.TalentFlowTheme.setPreference("dark");
  assert.equal(privateApp.document.documentElement.dataset.theme, "dark");
  privateApp.window.TalentFlowTheme.setPreference("invalid");
  assert.equal(privateApp.document.documentElement.dataset.theme, "dark");
});
test("同源另一页更改和清除偏好会同步", () => {
  const app = boot();
  app.events.storage({ key: "talentflow-theme", newValue: "dark" });
  assert.equal(app.document.documentElement.dataset.theme, "dark");
  app.events.storage({ key: null, newValue: null });
  assert.equal(app.document.documentElement.dataset.theme, "light");
});
