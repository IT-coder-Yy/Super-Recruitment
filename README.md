# AI 招聘自动化 Demo

面向 HR 的本地低成本招聘自动化应用。系统以 SQLite 为事实数据源，通过确定性工作流管理候选人状态，并使用可切换的 AI 模型完成简历结构化和 JD 证据化评分。

## 当前能力

- 模拟招聘数据一键采集、自动记录和姓名+手机号查重
- 候选人、岗位、投递状态和原始来源统一管理
- Mock / OpenAI-compatible 双模式 AI 解析与评分
- 人工审批后进入面试，AI 不自动淘汰或录用
- 面试候选时段、预约 Token 和 Mock 腾讯会议
- 排期页固定展示候选人预约链接，支持复制、打开、SMTP/Mock 预约邮件
- 面试反馈后进入 Offer 审批、草稿摘要、邮件发放、候选人公开接受/拒绝
- 候选人接受 Offer 后自动生成入职跟进任务，并由 HR 人工确认到岗
- 超时跟进、企业微信 Webhook/Mock 通知
- 招聘漏斗与效率看板实时轮询更新
- XLSX/CSV 导出，可上传至腾讯文档
- 模型 API Key 前端配置与清空，密钥仅保存在本机 `.env`
- 候选人详情支持人工补充姓名、手机号、邮箱、技能、经历、聊天摘要和备注
- 候选人数据删除接口，用于 Demo 数据清理和删除能力演示
- 用户名密码注册与登录，未登录时无法访问管理页面和管理 API

## Windows 快速启动

首次运行：

```powershell
.\scripts\setup.ps1
```

启动：

```powershell
.\scripts\start.ps1
```

如果应用已经启动，脚本会直接提示现有访问地址，不会重复占用端口。

停止后台运行的本项目服务：

```powershell
.\scripts\stop.ps1
```

访问地址：

- 管理端：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>
- 模型设置：<http://127.0.0.1:8000/settings>

默认使用 Mock AI，不配置 API Key 也能演示完整流程。

首次访问管理端会进入登录页。任何访问者都可以注册账号：

- 用户名至少 3 个字符，英文字母不区分大小写
- 密码至少 6 个字符
- 注册成功后需要返回登录页手动登录
- 密码仅以加盐哈希形式保存在 SQLite 中

默认演示账号也会随种子数据创建：

- 用户名：`demo-admin`
- 密码：`demo123456`

如果本地数据库已存在且没有该账号，可运行：

```powershell
conda run -n testin python -m app.seed
```

## 一键测试

```powershell
.\scripts\test.ps1
```

脚本会先执行语法检查，再运行 pytest。若本机没有 conda，会自动回退到当前 Python：

```powershell
python -m compileall app tests scripts
python -m pytest -q
```

启动服务后可运行端到端冒烟测试：

```powershell
python .\scripts\smoke_test.py
```

## 完整演示路径

1. 打开 <http://127.0.0.1:8000>，用 `demo-admin / demo123456` 登录，或注册一个 Demo 账号。
2. 进入“模拟招聘平台”，查看虚构候选人、聊天和岗位 JD，点击采集。
3. 进入“数据采集”，也可以上传 `samples/resumes` 下的模拟简历。
4. 点击“开始解析”，查看文本提取、规则解析、人类可读摘要、缺失字段和技术 JSON。
5. 对缺少手机号的样例 `missing_phone_gu_qinghe.txt`，演示人工补充关键字段后再确认入库。
6. 点击“确认入库”或“入库并启动 AI 评估”。
7. 进入“候选人”详情页，查看简历摘要、经历、技能、聊天摘要和 AI 评估证据；用“补充资料”编辑候选人资料。
8. 进入“系统设置”，演示保存 API Key、清空 API Key；清空后回退 Mock。
9. 进入“AI 评估”，人工选择“进入面试”。
10. 进入“面试排期”，生成候选时段，复制预约链接或点击“发送预约邮件 / Mock”。
11. 打开候选人预约链接，选择一个时段；系统锁定时段并创建 Mock 腾讯会议。
12. 返回“面试排期”，提交结构化面试反馈，选择“发 Offer”。
13. 进入“Offer”，查看条款、审批摘要、邮件草稿、发放记录和审计日志。
14. 审批通过后发送 Offer；未配置 SMTP 时会生成 Mock 邮件记录。
15. 打开候选人 Offer 链接，接受或拒绝 Offer。
16. 接受后进入“入职跟进”，逐项确认入职任务，最终状态变为已入职。
17. 查看“跟进中心”“通知与同步”和“实时看板”，确认漏斗、Offer 接受率、入职率和导出记录。
18. 点击 XLSX/CSV 导出，作为腾讯文档协作出口。

## 模拟简历样例

可直接上传的虚构简历位于：

```text
samples/resumes/
```

包含：

- `backend_intern_chen_bozhou.txt`
- `qa_intern_xu_zhiyao.md`
- `data_analyst_shen_ruoning.txt`
- `ai_app_intern_luo_yunxi.md`
- `missing_phone_gu_qinghe.txt`

## 手动运行

```powershell
conda create -n testin python=3.11 -y
conda run -n testin python -m pip install -r requirements.txt
Copy-Item .env.example .env
conda run -n testin python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 可选 Docker 启动

首次启动前先创建本地环境变量文件：

```powershell
Copy-Item .env.example .env
docker compose up --build
```

访问 <http://127.0.0.1:8000>。Compose 会从 `.env` 读取环境变量，并将宿主机的 `./data` 挂载到容器 `/app/data`，数据库和导出文件会保留在本地。

## Mock / 真实能力边界

| 能力 | 当前 Demo 默认行为 | 真实接入条件 |
|---|---|---|
| 数据库 | SQLite 是唯一事实源 | 迁移服务器后可切 PostgreSQL |
| AI 模型 | 默认 Mock；可配置 OpenAI-compatible API Key | 提供企业允许使用的模型 Key |
| 腾讯会议 | 无凭证时创建 Mock 会议链接 | 腾讯会议开放平台应用和测试账号 |
| 企业日历 | Demo Calendar 忙闲时段 | 企业日历开放接口凭证 |
| 企业微信 | 未配置 Webhook 时写 Mock 内部通知 | 企业微信群机器人 Webhook |
| 候选人外部通知 | SMTP 未配置时写 Mock 邮件记录 | SMTP_HOST、SMTP_PORT、SMTP_USERNAME、SMTP_PASSWORD、SMTP_FROM |
| 腾讯文档 | XLSX/CSV 导出或 Mock 同步记录；不是事实源 | 腾讯文档开放能力或受控人工上传流程 |

不要把 Mock 会议、Mock 邮件、Mock 企业微信通知或 XLSX 导出描述为真实外部系统同步。

## 安全边界

- Demo 只使用虚构数据。
- `.env` 已加入 `.gitignore`，不要提交真实密钥。
- 设置页面不回显 API Key 明文。
- 设置页面支持清空 API Key，清空后回退 Mock 模式并写入审计日志。
- 登录、注册和后台 API 已加入 CSRF 校验；登录失败有基础限速。
- 管理页面和管理 API 必须登录后访问；候选人预约链接保持公开。
- 用户密码使用 `scrypt` 加盐哈希保存，不存储明文密码。
- 在线模型调用前会对手机号和邮箱脱敏。
- 招聘决定保留人工审批，不由模型自动完成。
- 薪资、职级和预计入职日期在首版 Offer 中保持“待 HR 填写”，AI 只生成草稿和审批摘要。
- 不绕过招聘平台验证码、登录保护或访问控制。
