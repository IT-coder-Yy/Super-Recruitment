const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../app/static/js/admin.js"), "utf8");

async function submitWith(redirect) {
  let submit;
  let reloads = 0;
  const callbacks = [];
  const button = { textContent: "查询并核实变更" };
  const form = {
    dataset: {}, setAttribute() {}, removeAttribute() {}, appendChild() {},
    getAttribute: name => name === "action" ? "/api/interviews/1/meeting/reconcile" : "POST",
    querySelector: selector => selector === '[type="submit"]' ? button : null,
    addEventListener: (name, handler) => { if (name === "submit") submit = handler; }
  };
  const document = {
    body: {}, getElementById: () => null, querySelector: () => null, addEventListener() {},
    querySelectorAll: selector => selector.startsWith("form[action^") ? [form] : [],
    createElement: () => ({ dataset: {}, setAttribute() {} })
  };
  const location = new URL("http://local.test/interviews#interview-1");
  location.reload = () => { reloads += 1; };
  const window = {
    location, matchMedia: () => ({ matches: false, addEventListener() {} }),
    setTimeout: callback => callbacks.push(callback)
  };
  vm.runInNewContext(source, {
    document, window, URL, FormData: class {},
    fetch: async () => ({ ok: true, json: async () => ({ ok: true, redirect }) })
  });
  window.AdminUI.showToast = () => {};
  await submit({ preventDefault() {} });
  callbacks.forEach(callback => callback());
  return { href: location.href, reloads, buttonDisabled: button.disabled };
}

for (const redirect of ["/interviews#interview-1", "/interviews#interview-2"]) {
  test(`成功后跳转同页锚点仍刷新：${redirect}`, async () => {
    const result = await submitWith(redirect);
    assert.equal(result.href, "http://local.test" + redirect);
    assert.equal(result.reloads, 1);
    assert.equal(result.buttonDisabled, false);
  });
}
test("成功后跨页面跳转保留目标路径", async () => {
  const result = await submitWith("/candidates/1");
  assert.equal(result.href, "http://local.test/candidates/1");
  assert.equal(result.reloads, 0);
});
test("没有跳转地址时仍刷新当前页", async () => {
  assert.equal((await submitWith(undefined)).reloads, 1);
});
