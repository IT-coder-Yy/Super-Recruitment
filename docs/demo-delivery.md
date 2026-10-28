# 稳定闭环 Demo 实施记录

更新日期：2026-10-27。**T11最小修复后，221项Python、4项主题、24组工程/缺陷回归通过，B05恢复通过；当前50/54通过，D01/D02/D04/H04四项待正式签收或真实验证，M5未完成。** T10曾因新反例将通过数撤回为49，历史失败与旧回归证据均保留。可选Docker沿用未执行状态；本轮未重跑UI。详见[复验报告](demo-benchmark-fix-20261027.md)、[验收清单](demo-acceptance-checklist.md)和[评分规则待确认项](demo-scoring-rules-pending.md)。原README、output、真实配置与业务库保留，未提交Git、未发送真实通知或调用真实模型。

## 已实现的业务

| 环节 | 本轮行为 |
|---|---|
| 导入与解析 | 每文件独立任务；支持文本、DOCX 段落及表格、文本 PDF；空白／损坏文件明确失败；姓名、有效中国大陆手机号、邮箱经程序校验后才能入库；预览补充可保存并保留版本 |
| 身份和来源 | 规范姓名＋手机识别人；同人同 `job_id` 复用投递、保留新来源；不同岗位独立；弱匹配提示人工核对；不重置后期进度 |
| 岗位匹配 | 只读本投递原文、确认事实和当前 JD；资料/JD 更新使旧评估过期；在线失败有独立尝试记录、可重试，不自动退回 Mock；在线输出必须引用输入中真实存在的片段 |
| 测评／笔试 | 共用一套资源和邮件服务，以类型区分；岗位＋外链必填，内部 PDF/DOCX 可选；预览收件人后发链接，不发附件；邮件成功不推进；HR 通过／不通过／跳过均留备注和真实操作人 |
| 资源生命周期 | 未引用可删除；有任一发放记录只能停用；停用禁止新的发送和失败重试；快照保留；并发删除与发送受事务约束 |
| 面试 | 未确认轮过期可原轮重签，旧链接失效；已确认保护；下一轮独立记录；并发重复预约或争抢重叠时段只能确认一次 |
| Offer | 条款保存、退回、版本审批；修改已批准但未发条款必须重审；已发冻结；成功／失败／送达未知区分；未知待人工核实，核实后不会再次发信 |
| 入职 | 有效接受 Offer 后初始化准备事项；HR 登记、补充、延期、放弃；材料、账号、工位准备完成仍保持办理中；独立确认实际到岗才进入已入职 |
| 跟进与统计 | 生命周期内 APScheduler 扫描、持久化去重和关闭旧待办；今日新增按北京时间创建日期，漏斗按已记录的曾到达阶段；考试发放、通过、不通过、跳过、历史未记录分别统计 |
| 运行与保护 | 统一运行配置、真实 HR 审计、CSRF 校验；SQLite 显式补列及升级前备份；新进程恢复后资料和进度保留；发送中断转待核实，评估中断转失败 |

实现复用现有 FastAPI、SQLAlchemy、模板和通知适配器，没有引入新运行依赖或多 Agent 编排平台。新增的 `evaluations.py`、`exams.py`、`onboarding.py`、`periodic.py` 各处理一个职责；无需让 Agent 执行文件管理、发信、状态推进、定时扫描或统计。开发和补验由主 Agent 完成，重复验证交给脚本；上轮由一个只读子 Agent 复核故障恢复、验收证据和小修复。本轮按用户要求新开一个独立子 Agent 逐组审查24组基准，只产出审查报告，不冒充真人签收。

## 历史实施证据 T1—T4

| 编号 | 结果 | 证据 |
|---|---|---|
| T1 | **98 项 Python、4 项主题测试通过**；语法检查通过 | [JUnit](../.test-runs/checks-55d628fd8d9b493ab83210fcf0bac48f/pytest.xml)；[隔离检查](../.test-runs/checks-55d628fd8d9b493ab83210fcf0bac48f/isolation.json) |
| T2 | **18 项浏览器检查通过**；浅色／深色／390px 移动端，无页面脚本错误或整页横向溢出 | [浏览器结果](../.test-runs/ui-demo-20261026-final/screenshots/results.json) |
| T3 | 全新隔离数据库、HR 与未登录候选人分别使用独立会话，通过真实表单走完整闭环；终点 candidate/application 均为 #7、状态 joined | [闭环记录](../.test-runs/ui-demo-20261026-final/flow/flow-results.json)；[条款编辑](../.test-runs/ui-demo-20261026-final/flow/03-offer-terms.png)；[独立到岗](../.test-runs/ui-demo-20261026-final/flow/05-independent-arrival.png)；[到岗结果](../.test-runs/ui-demo-20261026-final/flow/06-joined.png) |
| T4 | **12 份虚构简历 × 2 个真实不同岗位 ID = 24 组工程回放通过**；包含表格 DOCX、两页文本 PDF、缺身份字段人工补全 | [24 组报告](../.test-runs/samples-e95975c51c0a41e0a198c53dcb627d49/report.json)；[样本说明](../tests/fixtures/demo/README.md) |

T1 检查了原 `.env`、`data/recruitment.db` 的前后 SHA-256 一致，且未创建真实目录的 `runtime.env`；测试明确只收集 `tests`，隔离 `output` 哨兵未被收集。测试外呼被替身拦截，浏览器闭环禁止访问其他站点。报告和截图在 `.test-runs/` 中，不进入 Git。

新增专项测试位于 [test_demo_contracts.py](../tests/test_demo_contracts.py)，覆盖资料修正、跨岗隔离、模型引用和失败、考试三分支、文件边界、版本冲突、SMTP SSL/STARTTLS、未知送达、并发预约／审批修改／资源删除、入职异常、定时触发、历史统计及恢复。旧库测试含缺失身份、第二轮、Offer、已入职记录；恢复到新目录并检查行数、完整性、附件和路径重定位。新进程重启测试实际运行应用启动逻辑，逐表比较业务记录，再继续完成原导入与考试结论。

只读独立审查发现的 5 项已处理：终止投递仍可发 Offer、资料变更后可能向旧邮箱重试、试卷未绑定岗位、已结束面试未生成反馈提醒、旧 Offer／入职待办不关闭。实际浏览器还发现入职表单的 `action` 字段覆盖 DOM 表单地址，已改为读取 HTML 属性，并由 T3 复验。

## 2026-10-26 补验 T5—T9

| 编号 | 结果 | 证据 |
|---|---|---|
| T5 | **169 项 Python + 4 项主题全部通过**；当时据此记录50项通过；B05在T10发现新反例后回退为待修，旧格式/补全测试仅作为局部证据；原配置/业务库摘要一致 | [JUnit](../.test-runs/checks-962db27234194e6b90ef03e5f0701921/pytest.xml)、[隔离记录](../.test-runs/checks-962db27234194e6b90ef03e5f0701921/isolation.json) |
| T6 | 两轮面试、Offer 退回重审、入职补充/延期/独立到岗及放弃两条页面路径；设置保存后服务实际重启，页面读回一致，整库和附件摘要不变 | [到岗闭环](../.test-runs/ui-remaining-20261026/flow/flow-results.json)、[放弃闭环](../.test-runs/ui-remaining-20261026/flow-withdraw/flow-results.json)、[重启页面核对](../.test-runs/ui-remaining-20261026/flow/restart-results.json)、[重启完整性](../.test-runs/ui-remaining-20261026/restart-integrity.json) |
| T7 | **18 项浏览器检查**，浅深色/移动端无脚本错误、无整页横向溢出；正常/损坏/空白文件混合批次页面 | [界面报告](../.test-runs/ui-remaining-20261026/screenshots/results.json)、[混合批次](../.test-runs/ui-remaining-20261026/batch/batch-results.json) |
| T8 | **24 组冻结样本工程回放通过**，没有修改人工基准、没有真实模型请求 | [样本报告](../.test-runs/samples-229ceaf119d34f31bbd1fc1618fe1c22/report.json) |
| T9 | Docker CLI 可执行，服务端 `docker_engine` 管道不存在，**容器子项未执行** | [只读环境探测](../.test-runs/docker-probe-20261026/result.json) |

该轮业务改动限于三个缺口，没有新增运行依赖、数据表或业务框架：

1. Mock 会议失败写入状态和审计，保持原预约可重试，返回明确 503。
2. 失败的内部通知增加原内容重试入口；关联新旧尝试、防止重复点击重发，提示使用正确渠道名称。超时后先核对内部群，可能重复提醒的边界在按钮确认中说明。
3. 候选人确认预约时也检查现有 15 分钟缓冲，防止两份提前生成的邀请确认成紧邻面试。

主要新增测试集中于 [test_acceptance_remaining.py](../tests/test_acceptance_remaining.py)，原 [test_demo_contracts.py](../tests/test_demo_contracts.py) 补充后期资料重评和固定旧版本回退。旧代码回退固定提交 `9108823d7dd9ae542e76f21620127c94b66ef640`，实际启动、登录和读取恢复库，并断言未重新迁移为新版。独立审查指出的 HEAD 漂移风险、通知文案和解析冲突断言均已修正并复核。

## 2026-10-27 基准与匹配复核 T10

新独立子 Agent 完整阅读12份简历及两份JD，再逐组对照24组草案与评分规则：8组需修改、9组依据不足、7组可保留方向；**24组数值区间均未校准，既有两条排序仅能保留方向。** 缺少业务领域证据、课程/个人实践的经验计分口径不明，以及全部样本共用泛化未知项，使现有区间不足以作为正式质量基准。此为独立 AI 审查，不是人类审核，也没有替换原审核人或批准字段。

主 Agent 在全新隔离库重新运行24组Mock，17个冻结文件摘要一致。实际反例包括“未使用 FastAPI”被提为技能、JavaScript误提Java、“无企业实习”和“四天意向”被提为实习经历，以及同一项目重复计数造成的评分失真。B05“不补造经历”因此失败待修；结构回放通过不能代替语义合理性通过。本轮只更新审查证据与验收状态，没有修改业务代码或原始基准，也没有重新执行全量169项回归。

证据：[逐组独立报告](demo-benchmark-ai-review-20261027.md)、[实际输出复核](demo-benchmark-replay-20261027.md)、[独立结构化意见](../.test-runs/benchmark-review-20261027/independent-review.json)、[机械核对](../.test-runs/benchmark-review-20261027/mechanical-checks.json)、[新回放](../.test-runs/samples-baf4e980afbd432d94069f6104722256/report.json)、[真实文件保护核对](../.test-runs/benchmark-review-20261027/protected-files.json)。真实配置/业务库与上轮最终隔离记录的摘要一致，真实目录未生成runtime.env。

## 2026-10-27 最小修复与复验 T11

**221项Python、4项主题及24组工程/缺陷回归全部通过，B05恢复通过。** 52项新增专项包含词边界、否定、他人贡献、明确经历分类、重复/无关条目不增分、人工字段优先和出勤问句/角色/跨行否定。独立子Agent只做代码审查，发现的出勤近邻已修复，最终无剩余阻断项。

业务改动仅三处：规则解析、复用规则的Mock结构化/覆盖评分、在线提示的证据边界。删除重复解析与按数量加分逻辑；没有新增依赖、数据表或迁移，没有重写已有人工资料。Mock改为未校准的词项/明确出勤覆盖演示，仍不能评价语义质量，D01/D02不通过。

证据：[全量JUnit](../.test-runs/checks-3a980860f72d424ca50d3695b7d4e7b3/pytest.xml)、[全量隔离记录](../.test-runs/checks-3a980860f72d424ca50d3695b7d4e7b3/isolation.json)、[24组报告](../.test-runs/samples-311cecc531e341ea8a40da6789b44be5/report.json)、[独立审查结论](../.test-runs/benchmark-fix-20261027/independent-review.json)、[保护核对](../.test-runs/benchmark-fix-20261027/protected-after.json)。详见[最小修复复验](demo-benchmark-fix-20261027.md)和[待确认评分规则](demo-scoring-rules-pending.md)。本轮未重跑UI或Docker，未调用真实模型或外部通知。

## 复演

在仓库根目录、已安装项目依赖的 Python 环境执行：

```powershell
# 自动选择解释器，也可先设置 TALENTFLOW_PYTHON。
powershell -ExecutionPolicy Bypass -File scripts/test.ps1

# 只读固定样本；自动使用新的隔离目录和显式 Mock。
python -B scripts/check_demo_samples.py

# 浏览器演示服务：目录必须不存在；账号 demo-admin / demo123456。
python -B scripts/serve_test_demo.py .test-runs/my-demo --port 8014
```

另一个终端运行浏览器检查，需要本机 Node、Playwright 和 Edge。若 `require('playwright')` 不可用，通过 `PLAYWRIGHT_MODULE` 指向本机已安装模块，不需要把测试依赖加进业务项目：

```powershell
$env:TALENTFLOW_URL='http://127.0.0.1:8014'
$env:UI_SCREENSHOT_DIR='.test-runs/my-demo/flow'
# $env:PLAYWRIGHT_MODULE='本机 playwright 模块的绝对目录'
node scripts/ui_flow.cjs
$env:UI_SCREENSHOT_DIR='.test-runs/my-demo/screenshots'
node scripts/ui_smoke.cjs
```

`ui_flow.cjs` 仅允许显式指定 8012–8019 的本机服务，先核对 Mock 设置，并阻止其他站点请求。请只配合 `serve_test_demo.py` 的虚构库使用。启动和测试脚本均不删除已有数据目录；停止该终端的服务即可结束演示。

S01—S09 的工程步骤按验收清单中的场景索引重放；指定的页面路径由 T6/T7 验证，其余故障/并发/重启由隔离 pytest 验证。AI 语义与人工基准仍单独待签收。

`serve_test_demo.py --resume` 仅允许恢复带本脚本标记且配置仍为 Mock 的虚构目录；不会重置用户密码或替候选人提交反馈。停止测试服务后，用相同目录和端口加 `--resume` 重启。浏览器脚本设 `UI_VERIFY_RESTART=1` 可核对原 `flow-results.json` 的状态与已保存配置；设 `UI_ONBOARDING_BRANCH=withdraw` 可演示放弃分支，设 `UI_BATCH_ONLY=1` 可运行混合批次检查。不同分支使用不同 `UI_SCREENSHOT_DIR`，切换分支前清除上述环境变量。

## 配置、升级与回退

- 正常服务仍沿用项目现有启动方式。首次启动从进程环境或旧根 `.env` 导入可编辑键，之后以数据目录 `runtime.env` 为准；页面保存写到同一文件。设置 `APP_DATA_DIR` 时不会读取真实根 `.env`。固定数据库地址仍由 `DATABASE_URL` 决定。
- `APP_PUBLIC_BASE_URL` 设置候选人可访问的完整地址；未设置默认本机 `http://127.0.0.1:8000`。`SMTP_SSL=true` 使用 SSL，否则 STARTTLS；旧 `SMTP_USE_SSL` 兼容。SMTP 未配置时明确记为 Mock。
- `MAX_UPLOAD_BYTES` 默认 10485760；`FOLLOWUP_ENABLED=false` 可关闭扫描，`FOLLOWUP_INTERVAL_SECONDS` 默认 900。仅承诺单进程 Demo；本轮未验证多 worker 或其他数据库类型。
- 首次升级旧 SQLite 前，在数据库同级 `backups/` 生成一致性 `.db`、`.json` 校验清单及 `.files/` 附件／运行配置副本；显式补列后回填来源关联。历史同人同岗重复不自动删除或合并，会告警并保留；有重复时不会强行创建唯一索引。
- 本轮只在虚构旧库执行迁移。处理真实历史库前应停服务，确认当前数据库及其同级 `uploads/`、`runtime.env` 已备份；若自行把配置或附件放到其他目录，需将其另行纳入备份。自动备份不包含任意外部路径。
- 回退先停止服务，使用 `python -B scripts/restore_backup.py <备份.db> <不存在的新目录>`。脚本校验哈希、恢复数据库和附件、重定位上传文件路径，不覆盖原库。再使用与备份对应的代码版本和新的数据库／数据目录启动；T5 已在隔离虚构库实际启动匹配的旧版本服务完成回退演练。不要把旧库直接交给新代码后称为回退，新代码会再次迁移。
- Docker 的数据卷配置已静态核对；T9 确认本机引擎不可用，未实际构建、重建或验证持久化；真实模型、SMTP、企业微信、会议和日历联通均未执行。会议仍为 Mock。

## 待审核与验收边界

1. [annotations.json](../tests/fixtures/demo/annotations.json) 是 24 组**待人工确认**草案，包含事实、原文片段、岗位要求、未知项、建议区间及排序。用户或指定 HR 审核后记录审核人、时间和版本，再冻结哈希；不能按当前 Mock 分数倒改标准。部分技能来自规则命中，语义、否定句和本人职责必须人工核对。
2. D01/D02 的语义基准以及 D04 的真实模型质量未通过验收。T4 仅证明解析、补全、入库、输入隔离和显式 Mock 能回放，不证明分数合理或排名正确。
3. T10发现的确定性反例已在T11修复并复验；复杂语义、正式区间和完整排序仍需[评分规则确认](demo-scoring-rules-pending.md)，不能由词项覆盖分自批。旧资料/旧评估保留版本，不静默重解析覆盖人工事实；如需修正应预览核对后重评。M5门槛未降低，也不按Mock得分倒改基准。
