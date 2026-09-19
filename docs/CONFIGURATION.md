# 配置参考

[文档首页](README.md) · [项目架构](ARCHITECTURE.md) · [开发说明](DEVELOPMENT.md) · [维护说明](MAINTENANCE.md) · [进展记录](PROGRESS.md)

源码核对日期：2026-09-19。以下默认值来自源码，不表示线上当前取值。

## 1. 配置加载与凭据

`study/app.py` 在模块导入时加载项目根目录 `.env`，`override=False`，已存在的进程环境变量优先，包括显式空字符串。大部分设置在创建应用、Tutor 或 EmbeddingClient 时读取；修改后应受控重启，不能依赖热更新。

生产凭据只写部署平台 Variables，不写 Git、教材、浏览器代码或文档。本地开发可使用被忽略的 `.env`；`.env.example` 仅为模板，当前尚未列出所有支持变量，以下表格覆盖测试码和备用模型预算等补充项。

所有布尔开关均仅字符串 `1` 表示启用，`true`、`yes` 不等效。使用与容器一致的 `/data` 作为挂载路径不属于私人机器路径；本地数据位置由配置决定，不应硬编码盘符。

## 2. 应用、存储和注册

读取位置：[study/app.py](../study/app.py) 的 `create_app`、`seed_test_codes`、认证端点，以及 [study/__main__.py](../study/__main__.py)。

| 变量 | 默认值 | 作用与注意事项 |
| --- | --- | --- |
| `PORT` | 8080 | 本地绑定 127.0.0.1；容器绑定 0.0.0.0。使用有效整数，勿给本地入口传空值 |
| `STUDY_DATA_DIR` | 项目 `.study-data` | 空值也回退本地目录；Docker 设置 `/data`，必须挂载持久卷 |
| `STUDY_SECRET_KEY` | 空 | 至少 32 字符；托管模式必须提供。非托管缺失时生成并复用数据目录 `.session-secret` |
| `STUDY_COOKIE_SECURE` | 托管 1，本地 0 | Docker 也设为 1；只在本地 HTTP 开发时用 0 |
| `STUDY_REGISTRATION_OPEN` | 1 | 普通注册入口开关；不会关闭测试码注册 |
| `STUDY_INVITE_CODE` | — | 已移除：邀请码通道已删除，注册只认测试码或个人 API Key |
| `STUDY_TEST_CODES` | 空 | 逗号/空白分隔、转大写，仅接受长度 6–64 的项；启动 INSERT OR IGNORE，不撤销已有码 |
| `STUDY_MAX_USERS` | 100 | 数据库总用户数上限，包括共享教材系统用户；直接转整数，配置应为正数 |
| `STUDY_MAX_BOOKS` | 20 | 每个用户私有教材上限；直接转整数，配置应为正数 |
| `RAILWAY_ENVIRONMENT_ID` / `RAILWAY_PROJECT_ID` | 空 | 任一存在即识别托管环境，要求持久密钥并启用单层 ProxyFix |

不要手工伪造 Railway 标识来解决登录问题。其他反向代理环境需评估转发头的信任边界，而不是直接增加受信任代理层数。

`STUDY_TEST_CODES` 不只是开关：有效配置存在时，每次启动都会按 `BETA_ACCOUNTS` 的固定用户名删除匹配账户，不限于历史预置账户，后来新注册的同名账户也受影响，详见[维护说明](MAINTENANCE.md)。测试码属于注册凭据，应按密钥管理。

## 3. 主模型与备用模型

读取位置：[study/tutor.py](../study/tutor.py) 的 `Tutor.__init__`、`_payload`。

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `STUDY_LLM_BASE_URL` | 空 | 主模型 OpenAI 兼容 API 前缀 |
| `STUDY_LLM_API_KEY` | 空 | 主模型 Bearer 凭据；无鉴权的本机服务可空 |
| `STUDY_LLM_MODEL` | 空 | 主模型名称，必须是服务端实际支持的名称 |
| `STUDY_LLM_MAX_TOKENS` | 16000 | 输出预算；有效整数钳制至 1000–200000，空或非法值回退默认 |
| `STUDY_LLM_FALLBACK_BASE_URL` | 空 | 备用 API 前缀 |
| `STUDY_LLM_FALLBACK_API_KEY` | 空 | 备用凭据，独立于主模型 |
| `STUDY_LLM_FALLBACK_MODEL` | 空 | 备用模型名称 |
| `STUDY_LLM_FALLBACK_MAX_TOKENS` | 主模型预算，或 16000 | 同样钳制至 1000–200000，空或非法值继承默认 |
| `STUDY_LLM_JSON_MODE` | 0 | 1 时对主备请求都加入 `response_format: {"type":"json_object"}`，需双方都支持 |

地址与模型非空就会被视作“已配置”，不强制密钥非空，也不会启动时探测连通性。仅备用配置完整时也可工作。`/api/config` 的 `llm_configured` 不能作为连通性证明。

当前客户端固定 temperature=0.15、连接/读取超时 `(5, 120)` 秒、不自动跟随 HTTP 重定向。读取超时不是整轮总耗时上限，串行主备及一次性回退可能产生多次请求。供应商自己的 token 上限可能小于客户端允许的 200000。

流式仅在首个可见正文增量前失败时切备用；已有文字后报错不会自动接着用备用模型续写。详见[项目架构](ARCHITECTURE.md)。

## 4. 向量服务

读取位置：[study/rag.py](../study/rag.py) 的 `EmbeddingClient`、`api_url`。

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `STUDY_EMBED_BASE_URL` | 空 | OpenAI 兼容 embeddings API 前缀 |
| `STUDY_EMBED_API_KEY` | 空 | Bearer 凭据 |
| `STUDY_EMBED_MODEL` | `bge-large-zh` | 代码默认模型名，不保证服务端支持 |
| `BAA_CLOUD_EMBED_URL` | 空 | 仅主向量地址为空时使用的兼容配置 |
| `BAA_CLOUD_EMBED_TOKEN` | 空 | 同组兼容凭据 |
| `BAA_CLOUD_EMBED_MODEL` | `bge-large-zh` | 同组兼容模型名 |

主向量地址为空时整组切换兼容配置，不混用两组密钥/模型。这只是配置兼容，不代表课程助手依赖另一个应用运行。

每批最多 16 个文本，连接/读取超时 `(15, 30)` 秒，使用 requests.Session 连接池。响应向量需为有限数值、非零，维度 16–8192，并在同次请求链路中一致；代码会归一化。`bge-m3` 等模型须显式填写准确名称，不能因为环境模板默认另一名称而省略。

换地址、模型名或维度会改变向量空间指纹；服务端在同一名称下替换权重也可能使旧向量失效，而指纹不一定能检测。应评估并重建索引；内置书不会仅因改模型变量自动重建。

## 5. API 地址拼接规则

聊天调用资源 `chat/completions`，向量调用资源 `embeddings`。规则为：裸主机补 `/v1`；存在任何路径时保留该路径，然后追加资源。不是固定给所有地址加 `/v1`。

| 输入前缀 | 聊天最终地址 |
| --- | --- |
| `https://provider.example` | `https://provider.example/v1/chat/completions` |
| `https://provider.example/v1/` | `https://provider.example/v1/chat/completions` |
| `https://open.bigmodel.cn/api/paas/v4` | `https://open.bigmodel.cn/api/paas/v4/chat/completions` |

不要把完整 `/chat/completions` 或 `/embeddings` 填入 BASE_URL，否则资源路径会重复。已有 `/api` 路径也会原样使用，不自动变为 `/api/v1`。

地址不允许用户名/密码、查询参数或 fragment；非本机必须 HTTPS，只有 localhost、127.0.0.1、::1 可使用 HTTP。配置新服务前核对供应商接口和真实返回结构；404 优先核查路径，而不是随意更换 token。

## 6. 固定容量与运行参数

以下为代码或部署文件中的常量，没有对应的通用环境变量。更改需要同步代码、测试和文档。

| 项目 | 当前值 |
| --- | --- |
| 部署副本 / Gunicorn worker / 请求线程 | 1 / 1 / 8 |
| Gunicorn timeout / graceful-timeout | 180 / 30 秒 |
| 健康检查路径 / 等待时间 | `/health` / 120 秒 |
| 失败重启策略 | ON_FAILURE，最多 5 次 |
| 索引执行线程 / 索引槽位 | 1 / 4（含执行与排队，不是 4 线程） |
| 同时问答槽位 | 2；满时返回 429 |
| qa 工具循环预算 | 工具调用 ≤10 次（AGENT_MAX_CALLS）、循环 ≤12 轮（AGENT_MAX_ROUNDS）；改值须同步系统提示词中写明的次数 |
| search_book 单次返回 | limit 1–8 段，默认 6 |
| 阅读正文块 | 约 2400 码点/块，按空行边界对齐；单次读取 ≤20 块（默认 12） |
| 阅读快照缓存 | 3 本书 / 64 MiB，进程内 |
| 私人批注 | 每书每人 500 条；摘录与笔记各 ≤4000 个 UTF-16 字符；频率 120 次/10 分钟 |
| 单教材上传 / 个人原文件总量 | 20 MiB / 50 MiB |
| 解析字符数 / 分块数上限 | 4,000,000 / 10,000 |
| 默认分块 / 重叠 | 1200 / 160 字符，不是 token |
| 用户每书会话 / 每会话消息 | 100 / 120；消息包含用户和助手两种角色 |
| 问题长度 | 1–3000 字 |
| 注册 / 新建对话 / 问答频率 | 5 次/IP/小时；40 次/用户/小时；60 次/用户/小时 |
| 反馈 / 语义高亮频率 | 200 次/用户/小时；120 次/用户/小时 |
| 登录频率 | 15 次/IP/10 分钟及 20 次/用户名/10 分钟 |
| 会话有效期 | 7 天；读取请求不滚动续期 |
| 应用日志保留 / 返回条数 | 3 天 / 最近最多 200 条可见记录 |
| 反馈统计周期 / 检查间隔 | 72 小时 / 6 小时，启动额外检查 |

请求体整体上限为 21 MiB，以容纳文件上传表单开销；非 `/api/books` 的 API 另有 32768 字节检查。书锁、限流桶和信号量不跨进程共享；语义高亮有独立频率限制，但不占用问答的两个槽位。

## 7. 配置审查清单

发布前核对数据目录与卷、持久密钥、HTTPS Cookie、注册策略、模型和备用模型的权限与费用。不要以 root `.env.example` 已填写为依据推断生产 Variables 已同步。

新增变量时同时修改读取逻辑、模板和本页；说明默认值、空值行为、是否需要重启，以及对旧索引和历史数据的影响。不要在文档中填入实际可用的密钥或测试码。
