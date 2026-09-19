# 开发说明

[文档首页](README.md) · [项目架构](ARCHITECTURE.md) · [配置参考](CONFIGURATION.md) · [维护说明](MAINTENANCE.md) · [进展记录](PROGRESS.md)

源码核对日期：2026-09-19。以下命令供开发者自行执行；本轮文档整理没有运行构建、测试或应用。

## 1. 开发环境与启动

推荐 Python 3.12，与 Docker 镜像一致；SQLite 需支持 FTS5。Python 依赖见 [requirements.txt](../requirements.txt)。课程助手没有前端编译步骤，不依赖 Node 才能运行。

在项目根目录使用已安装的 Python，以下为 PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m study
```

仅在 `.env` 尚不存在时执行复制，避免覆盖已有配置。启动前按[配置参考](CONFIGURATION.md)填写模型和开发配置；本地 HTTP 使用 `STUDY_COOKIE_SECURE=0`。未指定数据目录时使用项目 `.study-data`，未提供会话密钥时本地生成持久密钥。

打开 `http://127.0.0.1:8080`。入口绑定回环地址且 `debug=False`，代码变更后需重新启动。首次启动还会播种内置教材，健康检查成功不代表教材已经 ready。未配模型时可查看书架等页面，但不能据此验收生成链路。

`npm start` 只是 `python -m study` 的包装，使用 PATH 中的 Python；`npm run test:study` 包装 Python unittest。

依赖使用版本范围，当前不是完整锁定的 Python 环境。更改依赖时应由开发者在隔离环境验证兼容性，并记录实际部署版本。

## 2. 开发入口与常见修改点

| 需求 | 首先阅读 | 必须一起核对 |
| --- | --- | --- |
| 增加学习模式 | `study/tutor.py`、`study/app.py` | `web/app.js` 模式映射、页面按钮、答案校验与模式测试 |
| 调整检索 | `study/rag.py` 的 `retrieve` | `app.py` 的 SQL/FTS 范围、诊断字段、无命中和跨书测试 |
| 调整问答工具循环 | `tutor.py` 的 `agent_stream`、`AGENT_*` 常量与提示词 | 提示词内次数与常量同步、SSE search 事件、`retrieval.steps` 持久化、前端轨迹渲染与日志文案 |
| 改分块/章节 | `study/documents.py` | 索引重建对旧引用的影响、内置书播种策略、阅读器 anchor 映射与批注版本 |
| 接入新模型 | `Tutor`、`EmbeddingClient`、`api_url` | 实际接口返回、主备 JSON 能力、tools 支持与兼容性、超时、配置文档 |
| 新增 API | `app.py` 的 `protect` 和相邻端点 | owner/book/conversation 校验、CSRF、错误响应、前端调用 |
| 修改数据结构 | `database.py` 的 SCHEMA 与初始化 | 旧库迁移、级联删除、备份恢复、显式列名插入 |
| 修改阅读器/批注 | `study/reader.py`、`web/reader.js` | 快照缓存与版本哈希、UTF-16 偏移、anchor 全等校验、409 语义、批注上限 |
| 修改反馈 | `rate_message`、`refresh_feedback_stats` | 三张反馈表、`feedbackRow`、事件口径、隐私留存 |

当前应用大量路由、调度与后台任务集中在 `create_app` 内部。增加逻辑前确认同步请求和后台线程之间的事务、锁及生命周期，不要把应用工厂当作纯读取工具。

## 3. HTTP API 契约

### 3.1 认证和公共约定

首先 `GET /api/auth/me`，同一 Cookie 会话保存返回的 `csrf_token`。登录或注册成功会重建会话并返回新 token，后续写请求必须更新为新值。

除公开身份/登录/注册端点外，`/api/` 需要认证。所有非 GET/HEAD/OPTIONS 请求需要 `X-CSRF-Token`。JSON 请求设置 `Content-Type: application/json`；上传使用 multipart/form-data，不要手写其 boundary。

错误通常为 `{"error":"说明"}`。400 表示输入错误，401 未认证，403 CSRF/注册策略/禁止操作，404 不存在或不可访问，409 冲突，413 过大，429 频率或槽位限制。SSE 开始之后的错误通过事件表达，不能只检查 HTTP 200。

以下路径中的 `{book_id}`、`{conversation_id}`、`{message_id}`、`{chunk_id}` 均为服务返回的标识，不接受前端传入 owner 来决定权限。

### 3.2 端点索引

| 方法与路径 | 输入/输出概要 |
| --- | --- |
| `GET /health` | 公开；`{"status":"ok"}`，仅检查数据库连接可执行查询 |
| `GET /api/auth/me` | user、csrf_token、registration_open、test_code_registration、api_key_set（不回显密钥） |
| `POST /api/auth/register` | username、password，test_code 或 api_key 二选一（开放注册且无测试码时必须 api_key）；返回 user 和新 token |
| `POST /api/account/api-key` | `{"api_key":"..."}`；更新/清除个人密钥，返回 api_key_set |
| `POST /api/auth/login` | username、password；返回 user 和新 token |
| `POST /api/auth/logout` | 清除会话，返回 ok |
| `GET /api/config` | 模型配置状态、上传限制、格式；不是接口连通性测试 |
| `GET /api/logs` | 当前可见的最近日志，读取时触发过期清理 |
| `GET /api/books` | 可访问书籍列表，包含 builtin 标记 |
| `POST /api/books` | multipart `file`，可选 `title`；202 返回 book 并排队索引 |
| `GET /api/books/{book_id}` | book 及 sections |
| `DELETE /api/books/{book_id}` | 删除自有教材，内置书禁止 |
| `POST /api/books/{book_id}/reindex` | 重建自有教材索引并清理其会话；内置书禁止 |
| `GET /api/books/{book_id}/source` | 下载自有教材源文件；内置书禁止 |
| `GET /api/books/{book_id}/chunks/{chunk_id}` | chunk、prev、next（邻块无则 null）、window（引用段前 3 后 6 的初始阅读窗口） |
| `GET /api/books/{book_id}/chunks?anchor=&direction=before\|after&count=` | 按 anchor 邻近加载 1–20 块，按阅读顺序返回 |
| `POST /api/books/{book_id}/chunks/{chunk_id}/match` | `{"question":"问题"}`；返回句子 ranges，失败可返回空数组 |
| `GET /api/books/{book_id}/reader` | 正文阅读块窗口；`start`/`count`、`anchor`（chunk 定位）、`at`、`version` 定位参数互斥，返回 version、blocks、toc、total、length、anchor |
| `GET /api/books/{book_id}/annotations` | 当前用户在本书的批注列表，按正文位置排序 |
| `POST /api/books/{book_id}/annotations` | `{"version","start","end","quote","note","color"}`；摘录与正文全等校验，201 返回 annotation |
| `PATCH /api/books/{book_id}/annotations/{annotation_id}` | 仅允许修改 `note`、`color` |
| `DELETE /api/books/{book_id}/annotations/{annotation_id}` | 删除本人批注，返回 ok |
| `GET /api/books/{book_id}/conversations` | 当前用户在本书的会话列表 |
| `POST /api/books/{book_id}/conversations` | 创建会话，201 返回 conversation |
| `GET /api/books/{book_id}/conversations/{conversation_id}` | conversation 和 messages；助手历史包含当前 feedback |
| `DELETE /api/books/{book_id}/conversations/{conversation_id}` | 删除本人的会话及消息 |
| `POST /api/books/{book_id}/conversations/{conversation_id}/messages` | message、mode、可选 section；返回 SSE |
| `POST /api/books/{book_id}/conversations/{conversation_id}/messages/{message_id}/feedback` | rating: 1/-1/0、可选 reason；返回 feedback 数值或 null |

用户名为 3–32 位中文/字母/数字/下划线等受正则允许的字符，唯一性按 casefold 处理；密码长度 10–256。测试码注册与普通注册的优先级见配置及维护文档。

分块详情、邻块加载与 match 端点仍保留为公开 API；前端阅读器当前已改用 reader 接口，不调用这三个端点。

### 3.3 分块、批注和反馈数据

分块详情的 `chunk` 含 id、text、section、page、ordinal；`prev`、`next` 仅含 id、ordinal。邻块是书内最近序号，不是 ID 加减 1，也不是限定在本次检索命中集合。

match 的 ranges 使用相对于原分块文本的 `[start, end)` 字符区间；前端不得先改写文本再套用区间。关键词来自答案的 `retrieval.terms`，语义高亮则按原问题单独请求。

reader 的所有位置（`start`、`at`、批注起止）都以 UTF-16 代码单元计（`offset_unit: "utf-16"`），不能与码点下标混用。`version` 是规范全文的 SHA-256：定位参数带旧 version 而正文已变化时返回 409；引用 anchor 映射失败也返回 409。批注对象含 id、version、start、end、quote、note、color、created_at、updated_at；quote 必须与正文对应区间全等，创建后仅能改笔记与颜色。

feedback 的 0 是撤销当前评价，但仍写一条归档事件。reason 仅在 -1 时保存，strip 后最多 200 字。历史接口返回当前评价数值，不返回原因；UI 的原因面板只存在于当前 DOM，刷新后不会复原未提交输入。

## 4. SSE 与答案结构

客户端使用 POST fetch 流消费，服务端每帧为 `data: <JSON>\n\n`。事件类型在 JSON 的 type 字段，而不是 SSE 的 `event:` 行。

固定检索管线（explain / outline / quiz 及 qa 回退）的正常序列，delta 可以为零个：

```text
status(stage=retrieve)
status(stage=generate, hits=N)
delta(text=...) *
answer(message=..., user_message=...)
```

qa 模式由模型驱动工具循环，序列为：

```text
status(stage=generate)
status(stage=search, reset=true, search={query, count, auto?, error?}) *
delta(text=...) *
answer(message=..., user_message=...)
```

每帧 `search` 事件对应一次 `search_book` 调用（含自动补检与被拒调用）；`reset=true` 表示前端应清空此前的流式预览文本。

生成失败可发送 `error(error=...)`；验证参数等前置失败可能直接返回非 2xx JSON。自测仍使用此 SSE 端点，但模型生成是一次性，不发送正文增量。

`answer.message` 包含 id、role、content、mode、created_at，以及 paragraphs、quiz、citations、grounded、retrieval，部分回答还有 notice。段落包含 text 和 citations；自测条目包含 question、answer、explanation、citations。

引用对象包含 label、chunk_id、section、page、ordinal、excerpt。检索诊断包含 backend、degraded、scope、section、hits、retrieve_ms、terms，以及生成结束后的 generate_ms；qa 模式 scope 为 `agent-searches`，另含 steps（每次调用 `{query, count, auto?, error?}`）与 searches（即 steps 数量）。

读取历史时这些内容来自 `messages.payload`，不是数据库独立的 retrieval 列。无证据时是正常的 `grounded=false` 回答，不应误当网络错误。

流式预览不等于已验证或已保存；只有最终结果经过校验并提交后才收到 answer。不要在 delta 到达时写入正式消息、开放针对临时答案的反馈，或自动将部分文本拼接到备用模型输出。

## 5. 代码与前端修改约定

文件使用 UTF-8、LF；Python 文本读写显式指定 `encoding="utf-8"`。新代码注释和 docstring 保持 ASCII，中文面向 UI、错误提示和文档使用。不要在代码中写死私人盘符、密钥或具体用户账户。

所有权必须在 SQL/API 层验证。新增读写端点沿用 `book_row`、`conversation_row`，消息再限定 owner/book/conversation；共享书访问不意味着共享会话。

数据库修改需兼容既有文件。当前有若干 `INSERT ... VALUES(...)` 未列出列名的代码，增加列必须检查所有写入位置；不要仅改 SCHEMA 并假设旧库自动更新。

前端优先使用 `element`、textContent 等安全渲染文本。当前 CSP 只允许同源脚本/样式/连接，添加 CDN 或内联脚本会改变安全边界，不能为让新组件运行而随意放宽策略。

异步修改保留 `state.epoch`、AbortController 与面板递增序号检查。切书、切会话、退出后旧请求不得写回当前页面。阅读器的滚动加载只改变面板视图，不能静默改写历史引用。

## 6. 测试与人工验收

### 6.1 开发者自行执行的测试

本项目协作约定：助手默认仅做静态检查，不执行本地构建、类型检查、lint 或测试；以下命令由开发者自行决定运行，不表示本轮已经执行。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

等价入口为 `npm run test:study`（须确认 PATH Python）。也可在根目录使用：

```powershell
$env:PYTHONPATH = "."
.\.venv\Scripts\python.exe tests/test_study.py
```

测试目录当前不是可直接按 `tests.test_study` 导入的包，不要假设 `python -m unittest tests.test_study` 可用。当前源码包含 85 个 `test_*` 用例（`test_study.py` 61 个、`test_reader.py` 24 个）；数量是静态清点，不是本轮通过结果。

| 测试类 | 主要覆盖 |
| --- | --- |
| `DocumentTests` | 编码、Markdown 清理、章节边界、重叠分块、DOCX 转换 |
| `RetrievalTests` | 词法/FTS/向量、空间隔离、引用验证、JSON 解析、语义句子匹配 |
| `LlmFallbackTests` | 一次性回退、首增量前回退、增量后不回退、失败传播 |
| `AgentToolTests` | 工具循环：调用与轮数预算、超限拒绝、非法工具与参数、证据池编号、供应商重试与交接、流式预览 |
| `IsolationTests` | 认证/CSRF、跨账户和跨书、上传、重索引、对话删除、qa SSE 契约和反馈汇总 |
| `MigrationTests` | 共享教材旧外键迁移及历史保留 |
| `TestCodeTests` | 测试码绑定、重复注册、入口状态、启动播种和旧账户退役 |
| `ApiKeyAccountTests` | 自带 API Key 注册、更新与使用范围 |
| `CanonicalTests` / `ReaderApiTests` / `ReaderMigrationTests` | 规范全文与阅读块、阅读/批注 API 的权限与版本语义、旧库批注表迁移 |

运行前使用隔离环境。`create_app` 会加载 `.env` 并启动内置书播种、索引和汇总；不能仅因 `TESTING=True` 就认定完全离线。应在导入模块前，用显式空环境值覆盖 `.env` 中的主备模型、两组 embedding、测试码等实际配置，或在测试中完整 mock；不要让回归测试消耗生产额度。qa 模式的 SSE 测试尤其要 patch `agent_stream`（或 `generate` 与 `generate_stream` 两者），否则本地 `.env` 配置了真实模型时会发生真实网络调用。

SSE 测试需在 mock 作用域内调用 `response.get_data(as_text=True)` 消费 body；只检查 status_code=200 不会证明生成器已执行。同时覆盖 `generate` 与 `generate_stream`，防止真实模型调用绕过 mock。临时数据目录清理前要停止汇总线程、等待播种与索引执行器结束，避免 Windows SQLite 文件锁。

### 6.2 人工验收重点

| 场景 | 预期结果 |
| --- | --- |
| 两账户互访私有书/会话/消息 | 404 或认证拒绝，不能读取正文或写反馈 |
| 读取同一本内置书 | 两用户均可读，自己的对话彼此不可见 |
| 同账户两本互相矛盾的教材 | 切书后只使用当前书；章节筛选不越界 |
| 普通问答与自测 | 正文按增量呈现，自测最终返回；最终引用可打开真实分块 |
| 无证据 / 模型非法引用 | 前者明确依据不足；后者错误且不保存为合法回答 |
| 主模型首增量前/后分别失败 | 前者允许备用，后者错误退出，不拼接回答 |
| 问答工具检索 | 等待时每次调用有编号状态行；回答下折叠轨迹与检索次数一致；引用可打开阅读器定位 |
| 阅读器与批注 | 引用定位底纹正确；目录跳转与滚动加载不跨书；批注保存、回显、改色、删除正常；正文更新后旧版本定位返回 409 |
| 切书/停止/断线 | 旧响应不进入新页面；重新打开历史确认是否已保存 |
| 不满意原因、改评、撤销 | 当前状态正确；归档保留事件；满意/撤销清空当前原因 |
| 三日统计 | 在隔离数据中模拟闭合周期；复查不重复新增同周期，不用线上改时间戳测试 |
| 内置教材重建 | 先在备份副本验证清会话影响，绝不为验收随意改生产教材 |

端到端验证使用专用测试账户和非敏感问题；注册会永久消耗测试码、问答会产生模型费用，执行前需明确授权。

## 7. 提交前检查

核对 `.env`、数据目录、SQLite/WAL/SHM、测试码清单和私人素材未进入变更；内置教材会随镜像交付，需确认使用与分发权利。更改权限、删除策略、模型上下文或统计口径时同步对应专题文档。

文档改动不需要重新编译课程助手，不构成部署或数据库维护授权。静态检查、单元测试、浏览器验收和线上验证应分别记录，不把其中一种表述为全部通过。
