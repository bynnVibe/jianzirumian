# 见字如面 — 手写笔记知识库问答系统

> **「见字如面」** —— 将手写笔记转化为可检索的智慧，用知识滋养身心。
>
> 面向个人知识管理的智能问答系统：手写笔记图片经 OCR 转为文字，自动构建向量知识库，结合大语言模型（LLM）实现基于知识库的精准问答；支持联网搜索补充知识库未覆盖的信息。
>
> 🚀 **第一次接触本项目？** 直接跳到 [快速启动（开发环境）](#快速启动开发环境)，5 步 10 分钟跑起来；部署上云见 [Docker 部署](#docker-部署本地)。
>
> 🐳 **生产环境推荐：阿里云 ECS 容器部署** —— 本地构建镜像 → 导出 → 上传 → ECS 加载运行（服务器只跑容器不做构建，适合低配 ECS），见 [阿里云 ECS 容器部署](#阿里云-ecs-容器部署生产推荐)；确定性质量门禁与镜像构建已沉淀为 GitHub Actions 工作流，见 [CI/CD 流水线与工作流](#cicd-流水线与-github-工作流)。
>
> 👀 **只想先看一眼界面？** 登录页点「系统样例」即可打开静态视觉样例（新标签页），或直接浏览器打开根目录 [`系统样例.html`](./系统样例.html)（无需任何依赖）；系统内置使用指南（登录页「系统使用指导」，地址 `/guide`），涵盖问答、知识库工作台、回归评测、可观测性四模块与远程 Embedding 配置。详细操作与四维技术说明见[用户使用手册](./docs/用户使用手册.md)，其中附录 D 为**系统使用样例**（端到端场景演练）、附录 E 为**系统使用指导**（按角色的操作指引）。

---

## 目录

- [系统架构](#系统架构)
- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [快速启动（开发环境）](#快速启动开发环境)
- [Docker 部署（本地）](#docker-部署本地)
- [阿里云 ECS 容器部署（生产推荐）](#阿里云-ecs-容器部署生产推荐)
- [CI/CD 流水线与 GitHub 工作流](#cicd-流水线与-github-工作流)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [开发说明](#开发说明)

---

## 系统架构

后端采用单向依赖的分层架构，所有外部依赖（LLM / OCR / Embedding / 搜索 / 向量库）通过工厂模式抽象、运行时可切换 provider：

```
API 层 (app/api/*，路由与鉴权) → 服务层 (app/services/*，业务编排) → 核心层 (app/core/*，LLM/OCR/向量检索等共享能力)
```

### 五层微服务部署拓扑

运行拓扑按五层拆解（类微服务），功能架构不变：

| 层 | 组件 | 职责 | 对应容器/目录 |
|------|------|------|------|
| 层1 API Gateway | Nginx | 静态页面、限流、请求路由；HTTPS 由外部代理终止 | `frontend` |
| 层2 Agent Service | FastAPI 业务单体 | 单机、单进程、单业务写者，独占锁保护恢复与索引 | `agent` |
| 层3 LLM Proxy | 自建网关 | 多模型路由、原子请求配额、审计、故障切换、Token 用量 | `llm-proxy` |
| 层4 数据层 | Redis + SQLite + FAISS | 可降级文件缓存 + 业务持久化 + 向量检索 | `redis` + `./data` |
| 层5 监控层 | Prometheus + Grafana | 指标抓取 + 大屏（Trace 见应用内可观测性） | `prometheus` + `grafana` |

- Nginx 将业务请求交给 Agent、模型请求交给 Proxy；公共 `/metrics` 禁止访问，Prometheus 在容器网络内抓取。
- Agent 全部业务模块统一经 Proxy 出网：按「业务路由（角色 → 路由规则 → 模型）」解析模型与上游，日志显性记录「路由规则 → 模型 → 上游」全链路；网关未初始化（无启用上游/规则）时自动降级直连已配置提供商。
- 不支持多 Agent/worker 或横向扩容。Proxy 重启不会恢复或重置 Agent 业务任务。
- 所有宿主端口默认回环；Grafana 无默认密码。存活使用即时 `up`，readiness 只检查本地资源，不探测付费模型。

### Agent 能力编排与受控执行（能力缺口补救）

针对「系统现有工具解决不了的问题」（如实时天气、时效信息），系统把能力缺口补救与动态命令执行收敛为一条可信控制面主干（对齐《Agent 能力缺口补救与动态命令执行整改方案》）：

```
聊天入口/SSE → 请求上下文与需求分析（需求契约）→ Agent 编排器
  ├─ 能力目录与候选筛选（注册表工具 + 能力配方归一）
  ├─ 缺口识别与执行规划（缺参数不猜测 → missing_argument）
  ├─ 统一工具调用入口 ─ 策略引擎 / 审批记录 / 预算账本
  │     ├─ 固定工具适配器（invoke_tool：调用前钩子 + 可观测性）
  │     └─ 可信 Sandbox Controller → 一次性执行环境 → 网络出口 Broker（host 白名单）
  └─ 证据验证（四级：执行级→结构级→证据级→需求级）→ 回答生成
```

核心不变量：**缺参数不猜测**（不默认城市/URL，缺失即 `missing_argument`）、**隔离运行时不可用则拒绝动态执行并返回 `sandbox_unavailable`，绝不回退主机子进程**、**判定异常保留 `unknown/unresolved`，绝不假成功**。结果拆为三层状态（`execution_status` / `validation_status` / `resolution_status`），严格区分「程序执行成功」与「用户需求被满足」。声明式命令仅允许预审核白名单二进制（`date`/`curl`）+ 绑定 `spec_id`，联网命令只放行审核过的公网 host（拒绝回环/内网/云元数据）。对外接口：`POST /api/capability-gap/orchestrate`（编排一轮）、`GET /api/capability-gap/sandbox/profiles`（运行配置可用性）。

---

## 功能特性

### 用户与权限

| 功能 | 说明 |
|------|------|
| 注册/登录 | 首个注册用户自动成为管理员；Token 认证 + 24h 滑动续期；管理员可开关开放注册、管理用户 |
| 游客模式 | 未注册可直接体验对话，受每日免费次数限制（计数持久化） |
| 知识库权限 | 公共库全员可见；个人库仅属主可见，向量检索阶段**物理隔离**，任何用户（含管理员）不会检索到他人私人库 |
| 个人中心 | 修改用户名/联系方式/密码；使用看板（提问/检索/上传/Token 等总量统计 + 每日趋势，近 7/30/90 天） |

### 知识上传与解析（两段式）

| 功能 | 说明 |
|------|------|
| 两段式流程 | 「文件上传」先落盘并后台多线程并发解析（图片 OCR / Word・PDF 按页提取，表格转 Markdown）；确认效果后切「知识入库」写入向量库，解析与入库解耦 |
| 图片编辑器 | 单张上传支持旋转/复位/框选裁剪；批量多选逐张编辑或一键全部上传；前端即时模糊检测（拉普拉斯方差），模糊件弹窗提醒 |
| 多引擎 OCR | PaddleOCR（本地免费）/ 阿里云百炼（手写体精度高）/ 自定义 OCR API，上传界面自由切换 |
| 查看/编辑/润色 | 入库前可查看解析效果，直接编辑或一键 AI 润色，**入库的是最终确认文本**；Word/PDF 支持原始排版分页预览与页码跳转；润色自动留档片段改动前后版本，「版本」按钮可随时预览并回溯恢复 |
| 链接导入 | 支持提交公开文章链接（公众号/CSDN 等）；安全直抓（certifi 显式 SSL 上下文根治 HTTPS 证书校验失败，TLS/HTTP/超时错误精确分类）+ 站点 Skill 提取，并按用户 playbook 技能选择移动端 UA / 渲染优先策略；隔离渲染服务未启用时自动降级宽松提取/整页净化兜底（带噪声警告），不因渲染服务未启动而整单失败；审查面板提供「内容润色」（按 Skill 清洗策略去角标/噪声行/多余空格，保序保段不新增内容）；链接正文逐条查看确认后入库 |
| 目标知识库 | 两级筛选（公共/个人 → 具体库），支持新建个人知识库；入库进度实时显示，失败项可重试 |
| 入库历史 | 按时间倒序回溯本人成功入库记录，可预览原始文件、按页查看解析文档 |
| 重复入库校验 | 文档/图片/链接在**入口处**按内容指纹（`ingest_dedup`）校验重复；同一来源再次入库时前端预警、后端返回 409 提示「已入库」并跳过，不再重复解析/向量化，减少重复工作与资源消耗 |

### 知识库工作台（总览 + 详情）

侧边栏单一「知识库」入口（`/kb`），整合原知识库管理、知识收藏、知识百科三个页面：

| 层级 | 说明 |
|------|------|
| 总览页（`/kb`） | 分「公共知识库」「个人知识库」两区呈现库卡片（文档数/分片数等统计）；支持新建/编辑/删除知识库（公共库仅管理员可建），点击库名称进入详情 |
| 详情页（`/kb/:kbId`） | 7 个选项卡：**文档**（条目列表/双模检索/删除）、**检索测试**（库内混合检索调试）、**思维导图**（LLM 后台生成层级导图，节点可溯源跳文档）、**知识图谱**（llm-wiki 百科聚合生成，力导向画布可视化）、**知识收藏**、**知识百科**（均按当前库过滤）、**RAG评估**（按库评测用例 + Ragas 四指标运行）；游客仅可见文档/检索测试 |
| 上传文档 | 详情页「上传文档」按钮弹窗复用知识库上传界面，目标库锁定为当前知识库；独立上传页 `/kb/upload` 仍保留 |
| 图谱 ↔ 百科绑定 | 知识图谱由知识百科（llm-wiki）编译产物聚合生成（「重建索引」），支持节点/关系增删、关键词与语义搜索、节点溯源跳转文档/百科页；百科词条删除联动清理图谱 |

旧路径 `/knowledge`、`/bookmarks`、`/wiki` 等统一重定向到 `/kb`。

### 检索问答（RAG）

- **混合检索链路**：FAISS 稠密召回 + 项目内轻量 BM25 稀疏召回 + RRF 融合 + Rerank 重排（本地 CrossEncoder 或阿里云 DashScope）；管理员可切换 `dense`/`hybrid` 模式并调整 top_k、RRF 参数
- **问答流水线**：意图改写 → 检索 → 证据充分性判断（轻量 Self-RAG，不足时补缺失要点重检一次）→ 与联网搜索并行 → 注入上下文 → LLM 流式生成 → 引用编号核对（越界引用追加提醒），全程步骤进度条实时展示
- **复杂度分流 Agent**：检索前先做指令复杂度分类（启发式预筛 + LLM 细判，维度=推理步数/工具依赖/上下文依赖），据此分流——简单问题走上述固定流水线（视作 ReAct 单步快路，行为不变）；中等问题走 **ReAct** 多轮工具循环（`search_knowledge`/`web_search`，`Thought→Action→Observation`）；复杂问题走 **Plan&Execute**（先产出步骤清单并发 `plan` 事件，再顺序执行工具、汇总观测后作答，失败步标记后继续）。分类器/Agent 任何异常或超时都回退现有流水线，最坏情况=改造前行为；前端实时展示思考链/解题计划/工具调用过程
- **Human-in-the-Loop 确认**：① 仅开知识检索、未开联网且知识库无结果时，暂停并弹卡询问「是否联网搜索」（`need_search` 事件，可「联网搜索」或「直接回答」）；② 证据链经自动重检仍不足且判定缺口源于意图模糊时，用 LLM 生成澄清追问并弹卡（`clarify` 事件，附 2~4 个可点选补充方向）。两者均以 `done` 结束本轮，用户响应触发新的 `/chat/send`，服务端不挂起状态
- **文档级来源与反馈**：同一知识库、同一文档的命中片段合并为一个来源，按文档最高检索相关度降序排列，排序后统一编号；来源列表默认折叠，展开文档可看各片段全文/图片/页码。百分比是检索相关度，不是答案正确率；旧会话保留原引用编号映射。支持点赞点踩（点踩可一键导入回归评测集）
- **正文流式体验**：生成正文与过程事件分离，`reasoning_content` 不进入答案，过滤正文开头明确的推理标签块，并用最终答案提示约束过程化自述。主聊天收到大块正文时分帧平滑展示；这不能消除检索、模型首块或代理缓冲带来的等待。主聊天与知识助手最终回答流失败不再静默切成非流式整段补答
- **Markdown 富文本渲染**：回答中的表格/列表/代码等 Markdown 在页面以真实样式渲染（表格带表头底色、斑马纹与横向滚动，不再散排成纯文本）；最终答案约束引导 LLM 对对比、清单、打分、参数等结构化数据优先输出 Markdown 表格
- **联网搜索**：独立开关，DuckDuckGo（免费）/ AnySearch / Bing / SerpAPI 多源可选
- **会话管理**：多会话历史按日期分组、用户隔离；三层上下文压缩（借鉴 Claude Code）——微压缩三档分级截断 + token headroom 触发的结构化五节摘要 + 会话列表手动「压缩历史」（可填侧重点），压缩后注入「摘要 + continuation 指令」再水化
- **用户记忆**：类型化跨会话记忆（偏好/事实/目标/纠正，借鉴 Claude Code auto memory），支持纠正覆盖与自动合并；两层加载（常驻画像卡 + 相关检索）注入回答；侧边栏「我的记忆」可浏览/编辑/删除/主动写入；敏感信息（密码/token/证件号等）正则拦截不落库
- **Skill 配置（用户技能管理）**：侧边栏「Skill 配置」（`/skills`）让用户管理可复用技能并决定是否应用，借鉴成熟 Agent 的 Skill 理念。两类技能三个作用域——`prompt`（chat，注入对话系统提示）与 `playbook`（url_import 链接抓取策略 / polish 正文清洗策略）；内置「网页链接抓取回退策略」「链接正文清洗策略」（默认启用）与「通俗化健康结论」（默认关闭）仅可开关不可删改，内置/我的技能分组可折叠展开；自定义技能支持 CRUD + 导入本地技能文件（.json/.md/.txt）+ 校验（名称/内容/playbook JSON/敏感信息/上限 50），**新建/编辑/导入时经安全预审**（5 类高安全风险 + 6 类本项目无法实现的能力，命中拒绝落库并告知原因），预审行为全程留痕于 `skill_review_logs` 并在可观测性页「Skill 安全预审」面板重点暴露，按用户隔离、开关即时生效。技能分**个人/公共两种可见性**（`user_skills.visibility`）：个人技能仅属主可见可用，公共技能全员可见可用（列表标注「来自 X」且共享只读），新建时可选归属；可观测性「技能沉淀建议」识别到高频能力缺口时，侧边提供「同意沉淀」按钮，点击弹出编辑弹窗（选择公共/个人 + 名称/描述/指令内容/其他需求输入）一键沉淀为可用技能；沉淀编辑弹窗内置「市场相似 Skill 搜寻」，聚合 Skills.sh / ClawHub 等主流市场（不可达源自动降级不阻断），命中以卡片呈现（名称/描述/热度/来源）并可一键应用填充编辑表单
- **聊天附件确认入库**：默认只识别或总结；明确保存意图只生成确认卡，点击确认后才写入可写知识库。默认私人库，24小时有效，刷新保留状态；游客不可写，失败/中断不自动重试。
- **截图检索（粘贴即问）**：聊天输入框直接 `Cmd/Ctrl + V` 粘贴截图（自动命名 `截图_时分秒.png` 并标「截图检索」），后端 OCR 识别图中文字后**并入知识库检索查询**再走上述问答流水线：只贴图不打字=以图中文字提问，贴图 + 一句话=检索查询含图中文字而回答仍针对你的问题；识别内容按 `OCR_RETRIEVAL_CHARS`（默认 300 字）裁剪避免稀释检索信号，完整原文仍注入对话上下文；未开「知识检索」时只识别作答，入库意图仍走确认卡
- **聊天页模型下拉**：输入区下拉列出匹配路由规则（chat_answer 绑定规则 → 默认规则 → 首个可用启用规则）的上游模型，选不了其他规则的模型；选中即按该模型出网，留空按业务路由/规则内随机均衡解析
- **停止生成**：取消、超时或异常 EOF 保留部分文本并标为中断；不撤销已确认的入库。聊天默认总预算300秒，Proxy240秒，重试不重置。
- **语义缓存（相似问题命中直回）**：入口收拢于左下角用户菜单（个人中心）的「语义缓存」弹窗（不再占用对话主页面），针对**当前用户**历史问答按语义相似度匹配——每次正常作答后把「问题向量 + 回答」沉淀入 `semantic_cache`（经网关 embed 向量化，per-user 隔离），下次提问若相似度 ≥ 阈值（默认 0.90）则**直接回复缓存内容、不走知识检索/Agent/生成的业务路由**，命中回答带「语义缓存命中」徽标；弹窗可开关（运行时覆盖）、查看阈值/条目数/累计命中、逐条删除或一键清空；过短提问/回答不缓存，embedding/存储异常静默降级不阻断问答

### llm-wiki 知识编译层

借鉴 llm-wiki「LLM 作为知识编译器」思想，增量共存于问答库 RAG 之上：入库成功后后台自动将资料编译为结构化百科词条（概述/关键要点/相关概念/原文精选），写入 `wiki_pages` 表与向量库；检索结果按文档最高相关度排序；启用百科优先时仅将同一文档内部的百科片段前置，原文片段仍保留。支持跨素材综合词条（digest）、存量一键补编译、在线开关（编译/检索优先）、Markdown 落盘镜像（`data/wiki/`）、删除来源联动清理；编译失败静默降级，不阻断上传与问答。

**对话/检索历史知识沉淀（路径A：高频主题 → digest 私人词条）**：周期性从用户对话历史（`chat_messages ⋈ chat_sessions.user_id`）用 LLM 归纳「反复被问及」的高频主题（质量闸门：命中 ≥N 次、过滤寒暄），对每个未沉淀过的主题调用 `synthesize(topic, user, visibility=private)` 综合成**仅本人可见的私人 digest 词条**（回写向量库参与检索），落 `wiki_topic_digest_log`（`UNIQUE(user_id, topic_hash)`）防重复编译，词条删除联动清理允许重沉淀；独立开关 `WIKI_CONVERSATION_COMPILE_ENABLED`（受 `WIKI_COMPILE_ENABLED` 总开关约束），周期任务仅由持写者锁的 Agent 进程执行，失败静默降级不阻断问答；WikiView「对话沉淀」卡可预览高频候选主题、手动「立即沉淀」并跳转已沉淀私人词条。由此形成「问得越多 → 私人百科越丰富 → 检索越准」的正向飞轮。

### 知识收藏

收藏 AI 回答或知识片段，支持自定义标签、标签筛选、全文搜索、分页浏览、用户隔离；Word/PDF 来源保留文档类型与页码，收藏页可「在线预览（第 N 页）」跳回原文；卡片详情 Markdown 全文渲染。

### 管理员 / 运维能力

| 模块 | 说明 |
|------|------|
| 系统管理控制台（`/admin`） | 独立布局：用户管理、系统设置（OCR/搜索/Embedding/Rerank、注册/游客次数与检索参数）；原 LLM/知识助手/OpenRouter 免费模型配置已整体合并至模型网关，设置页保留迁移指引。设置区块默认折叠，点击标题展开参数/开关。网络 OCR/Embedding/Rerank 的连接参数在模型网关上游管理保存与测试，设置页只读展示现状；配置未保存离开设置页时弹窗确认（保存配置后离开/不保留直接离开/留在本页） |
| 回归评测（`/admin/eval`） | 评测平台三段式页签：**① 评测集**——用例管理（手工/JSON 批量/点踩反馈导入），卡片格呈现，点击进入详情跑 Ragas 四指标（忠实度/回答相关性/上下文精确率/上下文召回率）+ 检索命中率回归，跨 run 环比防优化回退；**② 评估器**——基于已有评测集构建评测工作流（新建/编辑/删除），一键运行并在页内实时监控进度与逐用例结果，下方整合 **Prompt / Skill 专项评测工作台**（静态断言 + Rubric 多维 LLM-as-judge + A/B pairwise 对比，LLM 产出优化文本可二次编辑后一键上线：提示词发布新版本、技能重跑安全预审）；**③ 实验**——跨评测集的运行历史总表（状态/进度/四指标/发起人），点击行进入逐用例结果与环比分析。评测离线独立：不写线上会话、不计 usage；单点指标超时降级不中断 run |
| 模型网关（`/admin/gateway`） | 见下节 |
| Agent 可观测性（`/admin/observability`） | 四大模块页签：**DashBoard**——可编辑呈现的指标卡（性能/质量/安全/业务四组目录，按账号持久化）+ 对话趋势（7/14/30 天）+ 业务/质量安全/模型资源三栏概况（「模型与调用资源」的工具调用/LLM 调用计数按 span 级明细钻取，数量与钻取列表一致），卡片与概况数字可点击钻取明细（钻取弹窗表格**分页展示**并在表格下方附**口径计算说明**，明确该数值如何算出；指标监控界面同此设计）；**日志记录**——按日期（按日落盘文件）/用户/级别/关键词筛选的系统日志，同分钟合并、详情弹窗、一键跳转关联链路；**指标监控**——质量/性能/安全/业务四个方面聚合（黄金信号、反馈与回归、网关安全事件与工具拦截、资产存量与活跃用户），上下文构成、记忆与 Token 用量分析融合于业务指标；**链路追踪**——「对话型链路」子页签为请求 Trace 列表，点击行打开 Trace 详情查看已接入追踪并保存的步骤与 LLM prompt/response（输入、输出各最多保留 20,000 字符，超长截断），并可「查看详细日志」按 trace ID 精确关联已落盘日志与异常堆栈（常见凭据脱敏）；「工具调用链路」子页签呈现 Tool Routing 调用链、标准化工具注册表与工具链路设计。普通用户另有个人视图（`/observability`）与「我的上下文 & 记忆」概览（仅统计本人：上下文窗口/构成表、压缩摘要、记忆分布与预算水位，`GET /observability/my-context`）；概览中记忆数值卡（条数/上限、画像卡候选、被检索命中、从未使用、平均重要度）与偏好/事实/目标/纠正类型条均**可点击钻取**记忆明细弹窗（类型/内容/重要度/画像卡/关键词/最近使用/更新时间，底部附口径说明，`GET /observability/my-context/memory-drill`） |
| CI/CD 流水线（`/admin/pipeline`） | 评估驱动部署（EDD），仅运维角色；确定性操作（离线回归/前端测试构建/部署快照一致性/镜像构建导出）已沉淀为 GitHub Actions 工作流（`.github/workflows/ci.yml`、`docker-images.yml`），见 [CI/CD 流水线与工作流](#cicd-流水线与-github-工作流) |

### 模型网关（LLM Local Gateway，层3 LLM Proxy）

对下游工具/应用提供 OpenAI 兼容模型入口（`/v1/chat/completions`、`/v1/models`，及多模态透传 `/v1/embeddings`、`/v1/reranks`），凭本地密钥鉴权，托管真实上游 Key：

| 能力 | 说明 |
|------|------|
| 业务路由（系统唯一模型入口） | 系统各模块（问答/知识助手/意图改写/证据校验/百科编译，记忆抽取/会话摘要/知识点总结/OCR 润色等后台角色，及多模态向量化 embed/重排序 rerank/OCR 识别 ocr）按角色绑定路由规则与模型；模型解析优先级：调用方显式模型（聊天页下拉选模型）> 业务路由绑定 > 网关默认模型（须在命中规则上游清单内）> 规则内随机均衡；未绑定角色自动回退默认规则；**类别感知选路**：角色按 `ROLE_CATEGORY` 只在同类上游（embed→embedding/rerank→rerank/ocr→ocr/其余→llm）内选候选，前端模型候选也按类别过滤互不串类；原系统设置中的 LLM/知识助手/OpenRouter 免费模型配置已合并至此 |
| 多模态上游分类（网关唯一来源） | 「上游管理」按 `category` 分 **LLM / OCR / Embedding / ReRank** 四类分组折叠（组头 chevron + 计数徽标），添加/编辑弹窗含「类别」下拉；网络 OCR/Embedding/ReRank 连接配置只存于网关上游，首次启动幂等种子化把系统设置生效的网络 provider 迁入并绑定 embed/rerank/ocr 角色；系统设置对应区块改为只读摘要 + 「前往网关」跳转（写入接口返 410），仅保留 provider 类型切换与 local 引擎字段；系统的 Embedding/ReRank/OCR 调用默认经 `/v1/embeddings`、`/v1/reranks`、`/v1/chat`（ocr 角色）穿过网关出网，网关未就绪时自动降级直连；**上游「连通测试」按 `category` 选择探测端点与请求体**（embedding→`/embeddings` 发 `{input}`、rerank→`/reranks` 发 `{query,documents}`、其余→`/chat/completions` 发最小 chat），逐模型探测、任一成功即更新健康度与延迟，401/403/404/429 精确归因——修复了改造前一律发 chat 请求到 `/chat/completions` 导致 Embedding/ReRank 上游测试误报 400/404 的问题 |
| 密钥与配额 | 请求次数原子认领；Token 为事后计量软限额，不是硬费用上限。模型授权、过期校验；密钥仅存哈希，明文只展示一次；系统内部出网使用自动创建的「系统内部调用」密钥，密钥页顶部卡片显性化其配置过程（自动创建/存储位置/携带方式/自愈）并支持一键轮换 |
| 负载均衡与故障切换 | 渠道优先级分层 + 同层权重加权随机（A-Res）；失败自动切换，连续失败 ≥3 次熔断、成功一次恢复；openai/anthropic/ollama 三协议归一化为 OpenAI 形态（含流式 SSE） |
| 日志与审计 | 每次调用全量入库（状态/Token/路由/参数/风险/**业务场景 role**/七阶段可视化调用链路），可搜索筛选（含按场景）分页展开明细；审计页每行带场景标签，行头徽标与状态联动（失败不再显示「无风险」而标「调用异常」），链路节点内联展示各环节具体动作；后端日志同步显性记录三段链路：`[业务路由] role → 规则 → 模型`、`[网关选路] 规则 → 模型 → 上游候选`、`[网关出网] 规则 → 模型 → 命中上游/状态码/耗时` |
| 安全审计中心 | 约 25 条内置规则、6 大类别（凭证泄露/敏感路径/危险命令/外联追踪/Unicode 隐写/公网 IP），审计/警告/脱敏/阻断四模式，规则可启停、支持黑白名单与扫描试跑 |

鉴权/模型授权 → 安全审计 → 候选渠道 → 原子请求认领 → 上游。审计拒绝、无渠道不扣请求，进入上游后失败不退，重试不重复扣；首块输出后不切换或重放。管理页「Proxy 连通测试」由独立 Proxy 执行，可能产生费用；未配置内部诊断凭据时明确拒绝。

### 缓存与稳定性

- **Redis 文件缓存**：知识库原始文件（图片/Word/PDF/预览 PDF）经 Redis 加速来源跳转与预览；公共库全局共享、个人库按属主隔离；Redis 不可用自动降级直读磁盘；响应头 `X-File-Cache` 标注命中状态
- **主问答语义缓存**：按用户落库 `semantic_cache`（问题向量 JSON + 回答），相似问题命中直回、绕过检索与生成，减少重复上游调用；开关/阈值/容量均可配（`SEMANTIC_CACHE_*`），超出条目上限按最近最少使用淘汰
- **持久化**：认证会话（`auth_sessions`）、游客计数（`guest_usage`）、工具调用、用户记忆、用户技能配置（`user_skills`，含公共/个人 `visibility`）与技能预审留痕（`skill_review_logs`）、入库指纹（`ingest_dedup`）、对话沉淀日志（`wiki_topic_digest_log`）、语义缓存（`semantic_cache`）、系统提示词版本（`prompt_versions`）与 Prompt/Skill 专项评测记录（`prompt_skill_evals`）等均落库 SQLite，服务重启不丢状态；聊天会话懒加载 + 短期缓存，避免内存压力

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **前端框架** | Vue 3 + Vite 8 | Composition API + SFC，构建产物按 vendor 拆包 |
| **状态管理** | Pinia 2 | 响应式状态管理 |
| **路由** | Vue Router 4 | 前端路由（history 模式） |
| **后端框架** | Python FastAPI | 高性能异步 Web 框架 |
| **AI/LLM 框架** | LangChain 0.3 | 检索增强生成 (RAG) |
| **向量数据库** | FAISS | 高效相似度搜索（稠密召回） |
| **稀疏检索** | 项目内轻量 BM25 实现 | 与 FAISS 稠密召回通过 RRF 融合，不引入额外中文分词依赖 |
| **业务存储** | SQLite | 用户/会话/用量/知识库元数据/认证会话/游客计数/用户记忆/用户技能/会话摘要/工具调用记录 |
| **Embedding** | Ollama / OpenAI 兼容 API（含阿里云 `text-embedding-v4`） | 远程或本地模型生成向量，FAISS 在本地存储与检索，二者独立 |
| **LLM** | 模型网关（层3 LLM Proxy，统一出网）/ Ollama 本地 / OpenAI 兼容 API | 问答生成：系统各模块按网关「业务路由」解析模型与上游；网关未初始化时降级直连 .env 配置的提供商 |
| **OCR** | PaddleOCR / 阿里云百炼 / 自定义 | 手写体文字识别 |
| **流式通信** | Server-Sent Events (SSE) | 实时流式输出 |
| **模型网关（层3 LLM Proxy）** | httpx 异步代理 + OpenAI 兼容协议（独立容器） | 统一接入上游 LLM，负载均衡/故障切换/密钥配额/安全审计 |
| **联网搜索** | DuckDuckGo / AnySearch / Bing / SerpAPI | 实时信息获取 |
| **容器化** | Docker + Docker Compose | 五层拓扑编排（网关/agent/llm-proxy/数据/监控） |
| **监控层（层5）** | Prometheus + Grafana | 抓取 agent 与 llm-proxy 的 `/metrics` 并出大盘；Trace 见应用内 Agent 可观测性 |
| **API 网关（层1）** | Nginx | 静态文件 + 限流 + 可信代理头 + 路由拆分 + SSE；外部 HTTPS |

---

## 快速启动（开发环境）

> 从零到能提问约 10 分钟（不含模型下载）。按 5 步顺序执行即可，无需修改任何代码。

### 第 1 步 · 环境准备

| 依赖 | 版本要求 | 验证命令 |
|------|---------|---------|
| Python | **3.11 ~ 3.12**（推荐 3.11） | `python3 --version` |
| Node.js | >= 20.19（推荐 22，Vite 8 要求） | `node --version` |
| Git | 任意 | `git --version` |
| （方案 A 需要）Ollama | latest | `ollama --version` |

### 第 2 步 · 拉取代码并安装依赖

```bash
git clone <仓库地址> && cd jianziruyang

# 后端依赖（含 FAISS / LangChain / sentence-transformers，体积较大，建议国内配 pip 镜像）
cd backend
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cd ..

# 前端依赖
cd frontend && npm install && cd ..
```

### 第 3 步 · 配置 `backend/.env`（二选一）

```bash
cp backend/.env.example backend/.env
```

**方案 A · 全本地零成本（不需要任何 API Key，首次体验推荐）**

```bash
ollama serve &                 # 若本地 Ollama 未运行
ollama pull qwen2.5:7b         # LLM（生成回答）
ollama pull bge-m3             # Embedding（向量化入库）
```

`.env` 保持默认即可（LLM 与 Embedding 默认都指向本地 Ollama）。

**方案 B · 云端 API（效果更好，需自备 Key）**

任选 OpenAI 兼容提供商（DeepSeek / 通义千问 / OpenRouter 免费模型等）：

```dotenv
LLM_PROVIDER=openai
API_BASE_URL=https://api.deepseek.com/v1
API_KEY=sk-你的密钥
API_MODEL=deepseek-chat

# Embedding 独立于 LLM 配置，以下为阿里云 OpenAI 兼容接口示例
EMBEDDING_PROVIDER=custom
EMBEDDING_API_BASE=https://<你的地域或工作空间接口域名>/compatible-mode/v1
EMBEDDING_API_KEY=<你的阿里云API密钥>
EMBEDDING_API_MODEL=text-embedding-v4
# 可选：启动时真实探测；示例模板默认 false，开启会产生少量模型调用
EMBEDDING_STARTUP_PROBE=true
# OpenAI 兼容客户端请求超时（秒），不是整条业务链路的总预算
EMBEDDING_REQUEST_TIMEOUT=20
```

> `.env` 中的 LLM 配置现在仅作为「网关未初始化（无启用上游/规则）时的降级直连」与网关默认模型兜底（`API_MODEL`）；配置网关后，各模块统一按「业务路由」出网（含方案 A 的本地 Ollama，可在网关「上游管理」中添加 ollama 协议上游承接）。
>
> OCR 默认 `OCR_PROVIDER=local`（PaddleOCR，首次识别时自动下载模型）；也可切 `aliyun`（需 AccessKey）或 `custom_api`。
> 阿里云地址请使用控制台中与 Key、模型权限匹配的地域/工作空间兼容接口根地址，替换上面的占位符，不要追加 `/embeddings`；已有可用工作空间地址无需改成公共地址。`custom`（自定义 API）与 `openai` 均使用 OpenAI 兼容协议，不代表必须调用 OpenAI 官方服务。云端模型使用 `EMBEDDING_API_MODEL`，`EMBEDDING_MODEL` 只用于 Ollama。
>
> 网络模型连接参数以「模型网关 → 上游管理 / 业务路由」为准；上面的环境项用于初始化及网关未就绪时的回退，不会覆盖已保存的网关配置。非网关设置仍按运行时覆盖（`data/runtime_config.json`）优先于环境默认；环境默认中，进程环境变量优先于 `backend/.env`。
>
> 页面支持的模型参数/开关可在线保存应用；`EMBEDDING_STARTUP_PROBE`、`EMBEDDING_REQUEST_TIMEOUT` 仅在环境配置中设置，修改后需重启后端；容器环境变更需重新应用 Compose，单纯 restart 不更新容器环境。

**远程 Embedding 验收流程**：管理员/运维进入「系统管理 → 模型网关 → 上游管理」，添加 `embedding` 类别的上游，填写兼容协议地址、Key 与模型列表（如 `text-embedding-v4`），保存后点「测试」；再在「业务路由」为 `embed` 角色绑定对应规则与模型，点击「保存全部」。系统设置的网络 Embedding 区块已改为只读现状与类型切换，不再提供连接字段编辑或测试按钮。

- 上游测试会真实请求模型，可能消耗少量额度，但不写入文档或重建 FAISS；HTTP 连通成功不代表返回向量与现有索引兼容。
- 用已有资料做检索测试，核对命中来源；需要验收入库时使用真实资料。管理员可在系统设置核对当前生效模型、地址、业务客户端最近请求状态及 FAISS 维度。
- 业务客户端「待验证 / 已验证 / 异常」反映当前进程最近请求及向量校验结果，与网关连通测试状态不同；readiness 也不验证远程模型。
- **远程 Embedding + 本地 FAISS 是正常组合**，不会自动改用本地模型。改模型不会自动转换历史向量；即使维度一致，不同模型的向量空间也可能不兼容，需先核对原入库模型，再安排备份与重建。

### 第 4 步 · 启动

**推荐：后台驻留启动**（进程脱离终端，关闭终端后服务不退出）：

```bash
bash scripts/dev-daemon.sh start    # 启动后端 + 前端（自动先清理旧进程）
bash scripts/dev-daemon.sh status   # 查看运行状态与端口健康
bash scripts/dev-daemon.sh stop     # 停止服务
```

日志位于 `.dev-logs/backend.log` 与 `.dev-logs/frontend.log`。

**备选：前台启动**（适合调试）：`bash start.sh` 一键启动（Ctrl+C 停止），或分别在两个终端运行：

```bash
cd backend && source .venv/bin/activate && uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-proxy-headers
cd frontend && npm run dev
```

> 业务出网已默认走同进程内的网关 `/v1`（`GATEWAY_INTERNAL_URL`，无需另行启动）；仅测试独立渠道诊断时另行启动 Proxy：`cd backend && .venv/bin/uvicorn app.llm_proxy:app --port 8001 --no-proxy-headers`。不要启动第二个 Agent。

### 第 5 步 · 首次访问与验证

打开 <http://localhost:5173>，点击「立即注册」——**第一个注册账号自动成为管理员**。

- [ ] `curl http://localhost:8000/api/ready` 返回 ready（不代表外部模型已可用）
- [ ] 在模型网关保存并测试 embedding 上游、绑定 embed 业务路由，再做检索测试；管理员在系统设置核对生效模型与本地 FAISS 状态（上游连通与 readiness 均不能代替索引兼容性验收）
- [ ] 上传一张手写笔记照片 → OCR 识别 → 切「知识入库」确认入库 → 对话页提问；确认正文增量显示，展开来源文档核对命中片段
- [ ] 管理员进入「系统管理」控制台，可见用户管理/系统设置/回归评测/模型网关/可观测性等模块

> `.env`（密钥）、`data/`（用户数据/向量库/上传图片）、日志与依赖目录均被 `.gitignore` 排除，不随仓库分发；新克隆后运行时目录自动创建，唯一需要准备的是 `backend/.env`。

---

## Docker 部署（本地）

要求 Docker 24+、Compose v2.24+；部署辅助程序使用 Python3.11+。先人工创建 `backend/.env`，设置至少16字符的非默认 `GRAFANA_ADMIN_PASSWORD`；渠道诊断需至少32字符的 `PROXY_DIAGNOSTIC_TOKEN`，两服务使用同一值。

```bash
# 仅首次且文件不存在时复制；不要覆盖已有配置
cp backend/.env.example backend/.env
# 编辑配置后构建，Agent/Proxy复用后端镜像
docker compose --env-file backend/.env build agent frontend
bash scripts/deploy-aliyun.sh server-preflight
bash scripts/deploy-aliyun.sh server-start
```

访问 `http://127.0.0.1:5173`，开放流量前完成首个管理员注册。监控为回环9090/3000；Grafana用户名默认admin，密码必须自行设置。更改环境变量不会轮换已有数据卷内的密码。

### 后端镜像瘦身构建（可选）

后端镜像默认全量构建（含本地重排序 torch 与 LibreOffice），约 2.3GB。若重排序使用远端提供商（如 `RERANKER_PROVIDER=dashscope`），可瘦身至 ~574MB：

```bash
docker compose --env-file backend/.env build --build-arg SLIM=1 agent
bash scripts/deploy-aliyun.sh server-start
```

瘦身影响（均有自动降级）：本地 CrossEncoder 重排序不可用；Word 原始排版 PDF 预览降级为解析内容逻辑分页 + 下载原文。

### 数据持久化

| 宿主机目录 | 容器内路径 | 内容 |
|-----------|------------|------|
| `./data` | `/app/data` | FAISS 向量库、上传图片、收藏、wiki 等 |
| `./data/db` | `/app/backend/data` | SQLite 数据库（agent 与 llm-proxy 共享） |
| `./logs/backend` | `/app/backend/log` | 后端日志（按日期 `YYYY-MM-DD.log`） |
| `./logs/frontend` | `/var/log/nginx` | Nginx 访问/错误日志 |
| `./backend/.env` | `/app/backend/.env` | 环境变量（只读挂载） |
| `./monitoring` | — | Prometheus/Grafana 配置（只读，缺失会导致 compose 启动失败） |

> Nginx 日志写入挂载目录 `logs/frontend/`，`docker logs <frontend 容器>` 为空属设计行为。
> Redis 为纯文件缓存（512MB + allkeys-lru、不落盘），重启丢失仅影响预热速度。

### 自定义配置

环境配置变更后执行 `server-start` 重新应用，单纯restart不更新容器环境。Agent/Proxy均有host-gateway映射；Linux宿主模型须监听容器可达地址并用防火墙限制来源，不能公开模型端口。默认网段 `172.30.0.0/24`、frontend固定地址 `172.30.0.10`；如冲突，协调修改 `COMPOSE_SUBNET` 与 `FRONTEND_PROXY_IP`。

Agent/Gateway分别导出持久counter，清理日志不回退；共享Gateway域含Agent兼容入口，仅Proxy导出一次。成功率/平均延迟/P95为窗口gauge，不使用rate。抓取或readiness失败持续1分钟触发规则，外部告警通知需另行配置。

---

## 阿里云 ECS 容器部署（生产推荐）

> ⭐ **本项目生产环境运行于阿里云 ECS 容器环境**：五层拓扑（Nginx / Agent / LLM Proxy / Redis+SQLite+FAISS / Prometheus+Grafana）全部容器化；开发机（Mac M 芯片/Intel）**本地构建 → 导出镜像 → 上传 → ECS 加载运行**，服务器只跑容器不做构建，适合低配 ECS；瘦身构建（SLIM=1）镜像从 ~2.3GB 降至 ~574MB。全流程由 `scripts/deploy-aliyun.sh` 自动化，镜像构建也可由 GitHub 工作流 `docker-images.yml` 完成（见下节）。

安全组只开放批准的80/443及受限SSH，不公开8000/8001/9090/3000。安装Docker/Compose和Python3.11+，预留镜像与静止快照空间，核对脚本的目标服务器参数。

```bash
# 开发机：分别执行，不能用管道连接子命令
bash scripts/deploy-aliyun.sh build
bash scripts/deploy-aliyun.sh save
bash scripts/deploy-aliyun.sh upload

# 新服务器：在部署目录解包；已有部署先按下节备份旧配置
tar xzf jianzirumian-deploy.tar.gz
mkdir -p backend
cp .env.example backend/.env  # 仅首次，没有现有文件时
# 编辑配置后
bash deploy-aliyun.sh server-load
bash deploy-aliyun.sh server-preflight
bash deploy-aliyun.sh server-start
```

服务器使用包根目录脚本；包必须包含 `deploy_runtime.py`、`nginx-https.conf` 和 `monitoring/`。`server-status`/`server-logs`查看状态，`server-stop`停止，`server-update`预检后更新。Compose统一显式使用 `--env-file backend/.env`。脚本默认瘦身构建，需要本地重排序/LibreOffice时使用 `SLIM=0`。

**与 CI/CD 工作流衔接**：`docker-images.yml` 工作流可在 GitHub Actions 上构建并导出同名 `jianzirumian-backend.tar.gz` / `jianzirumian-frontend.tar.gz` 镜像包（SLIM 可选）；下载工件放入 ECS 部署目录后即可 `server-load` → `server-preflight` → `server-start`。工件仅含两个镜像包，部署脚本 / `deploy_runtime.py` / `nginx-https.conf` / `monitoring/` 等辅助文件仍用本地 `save` 流程生成的部署包（或部署目录已有文件）。

### 外部 HTTPS

参考 [宿主Nginx示例](scripts/nginx-https.conf)，替换域名与已有证书路径：80重定向HTTPS，443的所有路径代理回环5173，SSE超时420秒；不要单独将业务路径绕到8000。

`EDGE_PROXY_CIDR`只配置容器实际看到的外部代理来源（Linux宿主通常是桥网关地址，必须核实）；后端仅信任固定frontend地址。云LB场景只把 `FRONTEND_BIND_IP` 改为批准内网地址，其余端口仍回环。监控使用SSH隧道，例如 `ssh -L 3000:127.0.0.1:3000 用户@服务器`。

### 旧 backend 拓扑迁移与回滚

**不能先覆盖旧配置再尝试迁移。** 升级前独立备份旧Compose、旧env及所有配置bind挂载，保留项目名与数据位置；其他配置备份按其部署目录相对路径放在旧Compose备份旁。普通更新检测到旧backend会拒绝。

```bash
# 解包新配置后，只读预检
bash deploy-aliyun.sh server-migrate-backend \
  --old-compose /备份/旧版/docker-compose.yml --old-env /备份/旧版/backend.env \
  --bundle /备份/本次回滚 --dry-run
# 确认预检结果与维护窗口后，执行相同命令并去掉 --dry-run

# 回滚会替换升级后业务数据，必须显式确认
bash deploy-aliyun.sh server-rollback \
  --bundle /备份/本次回滚 --confirm-overwrite-new-data
```

迁移核对真实服务/挂载/端口/环境/旧镜像ID，保留受限回滚产物，停写并静止快照后才替换本项目容器/网络、加载新镜像、检查readiness；不清理无关容器或数据卷。快照/就绪失败即停止，不自动覆盖数据。

回滚校验产物后恢复固定旧镜像及业务快照，升级后数据保留在 `displaced-*`。旧版只验证原健康接口，不等于新readiness；Prometheus/Grafana命名卷不随业务数据回退，需独立备份。正式切换前必须演练真实容器、Linux宿主网络、停服告警与恢复。

---

## CI/CD 流水线与 GitHub 工作流

两层分工：**GitHub Actions 工作流**把确定性、不依赖密钥、可复现的操作沉淀为质量门禁与镜像产物；**应用内 CI/CD 流水线**（`/admin/pipeline`，仅 ops）负责评估回归（EDD）与发布编排。

### GitHub Actions 工作流（`.github/workflows/`）

| 工作流 | 触发 | 内容 |
|------|------|------|
| `ci.yml` | push 到 main/master、PR | ① 后端离线隔离回归（`backend/tests/run_isolated.py`，不读真实 .env、不连业务 DB/外网）；② 前端单元测试 + 生产构建（`npm test` / `npm run build`）；③ 部署快照一致性（`scripts/sync_deploy.py --check`） |
| `docker-images.yml` | 手动（workflow_dispatch，SLIM 开关可选）或 v* tag | Docker 构建后端/前端镜像（默认 SLIM=1 瘦身）并导出 tar.gz 工件（保留 14 天），供阿里云 ECS 离线 `server-load`；不推送 registry、不读取密钥 |

安全约定：工作流事件字段不直接插值到 shell，经环境变量引号传递（如 SLIM 开关）。

### 应用内 EDD 流水线

六阶段：代码提交 → 单元测试 → 评估回归 → 构建镜像 → 灰度发布 → 全量上线；评估回归取 Ragas 四指标均值为质量分，低于阈值（默认 0.85）阻断后续发布。发布命令可配置为 `deploy-aliyun.sh` 的真实子命令：

| 阶段 | 命令示例（真实子命令） |
|------|------|
| 构建镜像 | `bash scripts/deploy-aliyun.sh build` |
| 灰度发布 | `bash scripts/deploy-aliyun.sh save && bash scripts/deploy-aliyun.sh upload`（推送镜像包到 ECS，不触碰运行中服务） |
| 全量上线 | ECS 侧 `bash deploy-aliyun.sh server-update`（预检后更新）或 `server-start` |

确定性验证不再依赖人工执行：代码推送后 CI 自动跑回归/构建/快照一致性；打 tag 后自动生成镜像工件，运维只需在 ECS 加载与发布。

---

## 项目结构

```text
backend/app/api/         路由与权限（含 kb/kb_insights/eval/observability/capability 等）
backend/app/services/    聊天、确认动作、知识库、图谱/思维导图、评测、网关等业务；
                         agent_orchestrator/capability_catalog/sandbox/ 为能力编排与受控执行层
backend/app/core/        数据库、运行所有权、模型、检索与观测；requirement_contract 为需求契约
backend/tests/           正式隔离回归
frontend/src/views/      页面（知识库总览/详情、评测平台、可观测性、网关等）
frontend/src/components/ 确认卡、kb/* 知识库面板、observability/* 观测组件、gateway/* 等
frontend/src/stores/     Pinia 与 SSE 客户端
monitoring/             抓取、告警与Grafana装配
scripts/                部署保护、HTTPS示例、开发脚本
docker-images/          与主配置同步的部署快照
.github/workflows/      GitHub Actions：CI 质量门禁与镜像构建导出（确定性操作沉淀）
docs/                   用户使用手册（含系统使用样例/使用指导附录）等文档
```

运行数据、密钥、依赖与日志不随仓库分发。

---

## 常见问题

### Q: faiss-cpu 安装失败

```bash
pip install faiss-cpu --only-binary :all:
```

或使用 Python 3.11（有完整预编译 wheel 支持）。

### Q: 前端页面空白或请求失败

1. 在服务器本地检查 `http://127.0.0.1:8000/api/ready` 与后端日志
2. 检查前端代理配置（开发环境）或 Nginx 配置（生产环境）
3. 阿里云需检查安全组是否开放了相应端口

### Q: 知识库搜索搜不到预期的文档？

搜索分两种模式：**输入即过滤**（实时按标题/文件名模糊匹配，字符重叠度 ≥ 60% 即命中）；**Enter 触发语义检索**（向量搜索返回片段卡片，可展开全文/页码/原图）。输入关键词未按 Enter 时显示「未找到」属正常——说明无标题匹配，按 Enter 再试内容检索。

### Q: 上传的笔记为什么没有出现百科词条？

知识编译在入库成功后后台异步执行（几十秒到几分钟），存量补编译先批量入队再由后台 worker 逐条处理，编译落库段（向量索引/FAISS/派生视图重建）在工作线程执行、不阻塞主事件循环。常见原因：① 内容过短（默认少于 80 字不编译，属正常跳过）；② 编译开关被关闭（系统设置「llm-wiki 知识编译」区块确认，可「存量补编译」一键补齐）；③ LLM 限流/失败静默降级（不影响原始资料与检索，稍后补编译）。

### Q: 上传 Word/PDF 后表格变成乱文本？

系统已内置表格 Markdown 化：PDF 经 PyMuPDF 检测表格区域转 `| 列1 | 列2 |` 格式并过滤重复行内文本；Word 按文档流提取段落与表格同样输出 Markdown。上传页「预览/编辑」区可确认解析效果并手动修正后再入库。

### Q: 如何切换 LLM 模型？

在「系统管理 → 模型网关 → 上游管理」维护模型连接参数，再在「业务路由」调整相应角色的规则与模型；LLM 配置已不在系统设置中编辑。修改环境文件不能覆盖已保存的网关配置。

### Q: 为什么配置远程 Embedding 后仍显示 FAISS 本地存储？

FAISS 是向量存储与检索引擎，不是 Embedding 模型。管理员可在系统设置核对「当前生效模型」与 FAISS 状态，再到模型网关测试 embedding 上游并通过业务检索核验；「待验证」只表示当前配置实例尚未验证。启动探测默认关闭，开启 `EMBEDDING_STARTUP_PROBE=true` 后独立于索引加载执行；启动探测与网关「连通测试」同路径**直连上游 `/embeddings`**（带真实密钥、不经同进程网关 `GATEWAY_INTERNAL_URL` 自调用——lifespan 未结束前事件循环不受理入站请求，自调用会挂到超时并误报「请求超时」）；远程探测失败不会使整个应用无法启动，`/api/ready` 返回 ready 也不证明模型可用。

### Q: Embedding 测试失败或修改后不生效？

先在模型网关上游管理保存参数并点「测试」，再核对 embed 业务路由、生效地址与检索请求结果；上游测试只检查连通，不替代业务向量校验。401 检查 Key/地域/工作空间，403 检查权限与额度，额度不足检查计费，429 降低频率或检查配额，400/404 核对接口与模型名，超时检查网络和远程服务。模型返回维度与索引不符时恢复原模型，或备份后另行重建；不要清空索引来“修复连接”。只改 `.env` 时还需核对进程环境与页面运行时覆盖项。

### Q: 为什么同一文档只显示一个来源，百分比代表什么？

同一知识库内按文档身份聚合，不按同名标题合并。点击默认收起的「知识来源 · N 份文档」，再展开文档查看 M 个命中片段；百分比取该文档最高检索相关度，不是答案正确率。历史回答保留原编号映射，不会改写正文。

### Q: 为什么回答仍有首块等待或看起来成段出现？

流式需等检索/工具处理和模型首块返回；上游服务或外部反向代理也可能缓冲。主聊天的分帧展示只能平滑已收到的正文，不能让未返回的内容提前出现。管理员/运维可在请求详细日志查看 OpenAI 兼容流的首正文耗时、正文 chunk 数与最大 chunk 长度，结合检索耗时和代理配置定位。推理字段与开头明确标签块已隔离，但模型写入普通正文的过程自述仍可能出现，历史回答不会自动清洗。

### Q: 如何查看一次请求的详细日志？

「系统管理 → Agent 可观测性 → 链路追踪 → 对话型链路」打开目标请求的 Trace 详情 →「查看详细日志」→「刷新日志」或「继续加载日志」。日志按 trace ID 精确关联，支持多行异常堆栈及跨日分页；只脱敏返回副本，不修改原始文件。旧日志没有 trace ID、已轮转清理、未落盘或独立进程未关联的记录不能补齐，可返回步骤追踪查看已保存的输入/输出与错误（有长度上限）。日志为空不代表请求没有执行。也可在「日志记录」页签按日期/用户筛选当天日志，点击行内「查看链路」跳转对应 Trace。

### Q: 忘记管理员密码？

不要删除业务数据库。先备份，再通过授权运维流程处理，避免破坏用户、会话与知识库关联。

### Q: docker build 拉取基础镜像报 403 / 超时？

两个 Dockerfile 默认通过 `ARG REGISTRY` 走 DaoCloud 公共代理（`docker.m.daocloud.io/library/`）；代理失效时可换源重建：`REGISTRY=docker.io/library/ bash scripts/deploy-aliyun.sh build`。

### Q: 本地开发服务过一段时间就消失、页面一直转圈？

进程挂在临时终端上被回收所致。改用驻留脚本并验证：`bash scripts/dev-daemon.sh start && bash scripts/dev-daemon.sh status`。

### Q: 如何备份整个系统？

先停止业务写者并确认容器/线程退出，再一致性备份SQLite（含WAL）、FAISS、上传和配置；不能在线简单复制充当一致性快照。监控命名卷需另行备份，并在隔离环境演练恢复。

---

## 开发说明

**架构原则**：分层单向依赖（API → 服务 → 核心），LLM/OCR/Embedding/搜索等外部依赖全部工厂模式抽象，新增提供商只需新增实现类；配置双源（`.env` 默认值 + `runtime_config.json` 运行时覆盖，页面设置优先）。

离线回归（不读取真实env、不连接业务DB或外网）：

```bash
./backend/.venv/bin/python backend/tests/run_isolated.py
npm --prefix frontend test
npm --prefix frontend run build
python3 scripts/sync_deploy.py --check
```

以上确定性验证已沉淀为 `.github/workflows/ci.yml`（回归/前端/快照一致性）与 `docker-images.yml`（镜像构建导出），push/PR/tag 自动触发，见 [CI/CD 流水线与工作流](#cicd-流水线与-github-工作流)。

Compose静态测试使用合成env，不连接Docker daemon；未安装对应工具会明确跳过。可显式设置 `AB_NGINX_IMAGE=已有本地Nginx镜像` 运行隔离容器语法检查（不拉镜像、不联网、不挂载业务数据）。静态/Mock不代替真实六容器、Linux网络、停服告警、迁移恢复与业务质量/容量验收，本轮整改不代表已经生产发布。

**关键设计决策**：

| 决策 | 选择 | 理由 |
|------|------|------|
| 向量数据库 | FAISS | 轻量本地运行，无需额外服务 |
| 业务存储 | SQLite (WAL) | 单文件零运维，支撑多用户业务数据 |
| 流式通信 | SSE | 比 WebSocket 轻量，浏览器原生支持 |
| 鉴权方案 | Bearer Token | 服务端会话 + 滑动续期，适合前后端分离 |
| 检索权限 | 按可见性构造子索引 | 检索范围只包含可见库，不等于每租户独立物理存储或全路径权限验收 |

---

> 见字如面 — 将手写笔记转化为智慧，用知识滋养身心。
