/* 使用独立测试库运行服务后执行；仅登录、读取和切换视图，不提交招聘决定。 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const base = process.env.TALENTFLOW_URL || "http://127.0.0.1:8001";
const output = path.resolve(process.env.UI_SCREENSHOT_DIR || "output/ui-implementation");

(async () => {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.UI_BROWSER || "msedge" });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, colorScheme: "light" });
  const page = await context.newPage();
  const errors = [];
  const overflows = [];
  const results = [];
  page.on("pageerror", error => errors.push(error.message));
  async function theme(value) {
    await page.locator(".theme-menu summary").click();
    await page.locator('[data-theme-choice="' + value + '"]').click();
  }
  async function capture(name) {
    await page.screenshot({ path: path.join(output, name + ".png"), fullPage: true, animations: "disabled" });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    if (overflow) overflows.push(name);
  }
  try {
    await page.goto(base + "/login");
    await capture("login-light");
    await page.getByLabel("用户名", { exact: true }).fill(process.env.UI_USERNAME || "demo-admin");
    await page.getByLabel("密码", { exact: true }).fill(process.env.UI_PASSWORD || "demo123456");
    await page.getByRole("button", { name: "显示密码", exact: true }).click();
    assert.equal(await page.locator("#password").getAttribute("type"), "text");
    await page.getByRole("button", { name: "隐藏密码", exact: true }).click();
    await Promise.all([page.waitForURL(base + "/"), page.getByRole("button", { name: "登录", exact: true }).click()]);
    await page.locator("[data-workbench]").waitFor();
    await theme("dark");
    await page.reload();
    assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
    await theme("system");
    await page.emulateMedia({ colorScheme: "dark" });
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
    await page.emulateMedia({ colorScheme: "light" });
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    await theme("light");
    results.push("主题选择/刷新持久/跟随系统即时切换通过");
    await page.locator("[data-workbench-search]").fill("无此候选人-验收");
    assert.equal(await page.locator("[data-candidate-card]:visible").count(), 0);
    await page.locator("[data-workbench-reset]").click();
    const cardCount = await page.locator("[data-candidate-card]:visible").count();
    assert.ok(cardCount > 0);
    await page.locator('[data-workbench-view="list"]').click();
    assert.equal(await page.locator("[data-candidate-row]:visible").count(), cardCount);
    await page.locator('[data-workbench-view="board"]').click();
    const schedule = await page.locator(".wb-schedule").boundingBox();
    assert.ok(schedule.height <= 320, "日程应紧凑");
    await capture("home-light");
    await theme("dark");
    await capture("home-dark");
    await theme("light");
    results.push("首页无匹配/清除筛选/看板列表切换/紧凑日程通过");

    const routes = ["/candidates", "/candidates/1", "/evaluations", "/exams", "/interviews", "/offers", "/onboarding", "/follow-ups", "/import", "/dashboard", "/notifications", "/settings", "/audit", "/mock-platform"];
    for (const route of routes) {
      const response = await page.goto(base + route);
      assert.equal(response.status(), 200, route);
      await page.locator("main").waitFor();
      const name = route.slice(1).replaceAll("/", "-");
      await capture(name + "-light");
      await theme("dark");
      await capture(name + "-dark");
      await theme("light");
      results.push(route + " 浅深色渲染通过");
    }
    await page.goto(base + "/candidates");
    const search = page.locator('[data-filter-table="#candidateTable"]');
    const job = page.locator('[data-table-select="#candidateTable"][data-column="1"]');
    const options = await job.locator("option").allTextContents();
    if (options.length > 1) {
      await job.selectOption({ label: options[1] });
      await search.fill("不会匹配的姓名");
      assert.equal(await page.locator("#candidateTable tbody tr:not(.table-filter-empty):visible").count(), 0);
      await search.fill("");
      for (const value of await page.locator("#candidateTable tbody tr:not(.table-filter-empty):visible td:nth-child(2)").allTextContents()) assert.equal(value.trim(), options[1]);
    }
    await page.goto(base + "/candidates?q=" + encodeURIComponent("无此候选人-验收"));
    assert.equal(await search.inputValue(), "无此候选人-验收");
    assert.equal(await page.locator("#candidateTable tbody tr:not(.table-filter-empty):visible").count(), 0);
    results.push("表格组合筛选及侧栏搜索参数通过");

    await page.setViewportSize({ width: 390, height: 844 });
    for (const route of ["/", ...routes]) {
      await page.goto(base + route);
      await capture((route === "/" ? "home" : route.slice(1).replaceAll("/", "-")) + "-mobile");
    }
    await page.goto(base + "/");
    await page.getByRole("button", { name: "打开导航", exact: true }).click();
    assert.equal(await page.locator(".page-shell").evaluate(el => el.inert), true);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#sidebar").evaluate(el => el.inert), true);
    await page.locator(".theme-menu summary").click();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator(".theme-menu").getAttribute("open"), null);
    await theme("dark");
    await capture("home-mobile-dark");
    results.push("移动视口/抽屉导航/焦点隔离/Escape关闭通过");
    const publicPage = await context.newPage();
    await context.clearCookies();
    for (const route of ["/login", "/register", "/schedule/invalid-token"]) {
      await publicPage.setViewportSize({ width: 390, height: 844 });
      await publicPage.goto(base + route);
      await publicPage.screenshot({ path: path.join(output, route.slice(1).replaceAll("/", "-") + "-mobile.png"), fullPage: true });
      assert.equal(await publicPage.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
    await fs.writeFile(path.join(output, "results.json"), JSON.stringify({ results, errors, overflows }, null, 2));
    console.log(JSON.stringify({ passed: results.length, results, errors, overflows }, null, 2));
    assert.deepEqual(errors, [], "浏览器脚本错误");
    assert.deepEqual(overflows, [], "页面横向溢出");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
