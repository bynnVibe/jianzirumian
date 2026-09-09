# 见字如面 — 手写笔记知识库问答系统

> **「见字如面」** —— 将手写笔记转化为可检索的智慧，用知识滋养身心。
>
> 见字如面，是一款面向个人知识管理的智能问答系统。它能够将手写的笔记图片通过 OCR 技术转化为文字，自动构建向量知识库，并结合大语言模型（LLM）实现基于知识库的精准问答。同时支持联网搜索，在知识库无法覆盖时获取最新信息。
>
> 🚀 **第一次接触本项目？** 直接跳到 [快速启动（开发环境）](#快速启动开发环境)，5 步 10 分钟跑起来；部署上云见 [Docker 部署](#docker-部署本地)。
>
> 👀 **还不想装环境、只想先看一眼长什么样？** 用浏览器直接打开项目根目录的 [`系统样例.html`](./系统样例.html)（双击即可，无需任何依赖），即可点击导航浏览对话、上传、知识库、知识百科等各界面的高保真静态效果。

---

## 目录

- [系统架构](#系统架构)
- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [快速启动（开发环境）](#快速启动开发环境)
- [Docker 部署（本地）](#docker-部署本地)
- [阿里云服务器部署（生产环境）](#阿里云服务器部署本地打包推送)
  - [前置准备](#前置准备)
  - [部署流程概览](#部署流程概览)
  - [第一步：配置服务器信息](#第一步配置服务器信息)
  - [第二步：在本地构建并上传](#第二步在本地构建并上传一键操作)
  - [第三步：在服务器上加载镜像并启动](#第三步在服务器上加载镜像并启动)
  - [第四步：更新部署](#第四步更新部署)
  - [第五步：配置域名与 HTTPS](#第五步配置域名与-https推荐)
  - [第六步：配置 Ollama 服务](#第六步配置-ollama-服务仅本地模型需要)
  - [第七步：定时备份](#第七步定时备份推荐)
  - [部署检查清单](#部署检查清单)
  - [阿里云部署注意事项](#阿里云部署注意事项)
- [使用指南](#使用指南)
- [API 文档](#api-文档)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [开发说明](#开发说明)

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                       前端 (Vue 3 + Vite)                        │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │   侧边栏 AppSidebar       │        主内容区                  │  │
│  │  ┌───────────────────┐   │  ┌───────────────────────────┐  │  │
│  │  │ 品牌标识           │   │  │  问答界面 (ChatView)      │  │  │
│  │  │ 新对话             │   │  │  ├ 消息列表               │  │  │
│  │  │ 知识库上传          │   │  │  ├ 流式渲染 Markdown     │  │  │
│  │  │ 知识库管理          │   │  │  ├ 来源引用 + 原始图片   │  │  │
│  │  │ 知识收藏            │   │  │  └ 收藏按钮              │  │  │
│  │  │ 系统管理(管理员)    │   │  │                           │  │  │
│  │  │ 对话历史列表        │   │  │  ┌───────────────────┐   │  │  │
│  │  │ 用户信息 + 登出     │   │  │  │ 输入区 + 模式开关  │   │  │  │
│  │  └───────────────────┘   │  │  └───────────────────┘   │  │  │
│  └──────────────────────────┼──────────────────────────────┘  │  │
│                             │                                  │
│  ┌──────────────────────────┴──────────────────────────────┐   │
│  │  /upload (知识上传)  /bookmarks (知识收藏)               │   │
│  │  /management (系统设置)  /admin/users (用户管理)        │   │
│  └─────────────────────────────────────────────────────────┘   │
├────────────────── HTTP + SSE ─────────────────────────────────┤
│                     后端 (FastAPI)                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  API 层                     │  服务层                    │  │
│  │  /api/chat/*   → 流式问答  │  ChatService               │  │
│  │  /api/auth/*   → 登录注册   │  AuthService               │  │
│  │  /api/config/* → 系统配置   │  BookmarkService           │  │
│  │  /api/bookmarks → 知识收藏  │  KnowledgeService          │  │
│  │  /api/knowledge/* → 上传OCR │  ConfigManager             │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  核心抽象层                                               │  │
│  │  LLM (Ollama/OpenAI) │ OCR (本地/阿里云) │ Embedding     │  │
│  │  VectorStore (FAISS) │ WebSearch (DDG/Bing) │ Reranker  │  │
│  └──────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 功能特性

### 1. 用户认证系统

| 功能 | 说明 |
|------|------|
| **用户注册** | 首个注册用户自动成为管理员，后续用户为普通用户 |
| **用户登录** | 基于 Token 的认证，支持持久登录态（默认 24 小时滑动续期） |
| **游客模式** | 未注册可直接以游客身份体验对话，受免费次数限制 |
| **注册开关** | 管理员可在系统设置中关闭开放注册 |
| **用户管理** | 管理员可查看/启用/禁用/删除用户 |
| **个人中心** | 点击左下角用户名：修改个人信息（用户名/联系方式/密码）、查看个人使用看板 |
| **路由守卫** | 未登录自动跳转登录页，管理员路由权限控制 |

### 2. 知识上传模块

将手写笔记转化为可检索的结构化知识。

| 功能 | 说明 |
|------|------|
| **两段式流程** | 页面顶部方框切换「文件上传 / 知识入库」两模块：文件上传后立刻落盘，后台多线程并发解析（图片走 OCR、Word/PDF 按页提取）；确认解析效果后再入库，解析与入库解耦 |
| **图片上传** | 支持 JPG、PNG、BMP、TIFF 等常见图片格式；单张选择后先进入图片编辑器（旋转 / 复位 / 框选裁剪 / 长按拖动），批量模式多选后在待选列表逐张编辑或一键全部上传 |
| **Word / PDF 导入** | 上传 .doc/.docx/.pdf 文件，后台按页提取文字与表格；解析列表实时显示进展，完成后可分页预览原始排版并支持页码跳转；表格自动解析为 Markdown 格式保留结构 |
| **多引擎 OCR** | 支持多种 OCR 引擎，用户可在上传界面自由切换 |
| **查看/编辑 + 润色** | 入库前可查看解析效果，直接编辑或一键 AI 润色，**入库的是最终编辑后的文本** |
| **目标知识库选择** | 两级筛选：先选公共/个人，再选具体知识库；支持新建个人知识库 |
| **自动向量化** | 文本自动分割 → Embedding 向量化 → 存入 FAISS 向量库，并同步编译 llm-wiki 百科卡片 |
| **入库实时进度** | 逐文件或一键全部入库，进度条实时显示 x / y 与当前文件，直到全部成功；失败项单独标红可重试 |
| **批量处理** | 批量选择多张图片：批量识别 → 逐条查看/编辑/裁剪结果 → 单条或一键批量入库（识别与入库解耦，确认后才写入知识库） |
| **图片模糊检测** | 选择图片后前端即时做清晰度分析（拉普拉斯方差），低于阈值弹窗提示「图片较模糊，可能影响 OCR 识别效果」并引导重新选图，避免模糊件入库拉低识别率 |
| **入库历史** | 「知识库上传」页第三个 Tab：按入库时间倒序展示本人成功入库的记录（入库时间 / 知识来源 / 入库片段数）；来源可预览原始文件（源文件被删则置灰标注），「查看解析文档」跳转详情页按页回溯解析文本片段 |

**支持 OCR 引擎：**

| 引擎 | 类型 | 优点 | 缺点 |
|------|------|------|------|
| PaddleOCR | 本地离线 | 免费、离线、隐私好 | 需要安装（pip install paddleocr） |
| 阿里云 OCR | 云端 API | 识别精度高、支持手写体 | 需要 API Key 和网络 |
| 自定义 OCR API | 云端 API | 灵活接入任意 OCR 服务 | 需要自行搭建或购买 |

### 3. 知识检索问答

智能问答界面，支持双模式灵活切换。

- **知识检索**（默认开启）：采用「FAISS 稠密召回 + BM25 稀疏召回 + RRF 融合 + Rerank」的混合检索链路
  - **稠密召回**：到 FAISS 向量库中搜索语义相关知识
  - **稀疏召回**：项目内轻量实现的 BM25（中文单字+二元片段、英文/数字整词分词，无需额外依赖），弥补稠密检索对关键词强匹配场景的召回不足
  - **RRF 融合**：`rrf_score = Σ 1/(k0+rank)` 合并两路候选，去重后再进入 Rerank，不改变最终上下文格式
  - 可在管理员「系统设置」中切换 `dense`（仅稠密）/`hybrid`（混合，默认）检索模式，调整 top_k、RRF 参数
  - **权限前置**：检索从一开始就只在用户可见范围内进行（公共库 + 自己的私人库），不会触及他人私有知识
  - 知识存在 → 回答需引用原文 + 标注来源编号 + 可查看原始图片
  - 知识不存在 → 委婉告知用户，不编造答案
- **意图改写**：发送问题前，LLM 自动将用户问题改写为更利于检索的表述，提高召回率
- **信息缺口判断 + 轻量 Self-RAG**：检索后由 LLM 判断证据是否足以回答问题（15s 超时保护，失败按充分处理）；不足时结合缺失要点改写查询**重检一次**，新旧结果去重合并后整体重排序
- **引用编号核对**：生成结束后自动扫描回答中的 `[来源 X]` 编号，发现越界引用（编号超出实际来源条数）时追加醒目提醒，防止模型编造引用
- **流水线进度展示**：问答过程以步骤条实时展示「理解问题 → 知识检索 → 证据评估 → 联网搜索 → 生成回答」每一步状态（进行中/已完成），不再是干等
- **结果重排**：Reranker 对检索结果重新排序，支持本地模型与阿里云 DashScope 在线接口
- **反馈评价**：对回答可点赞/点踩，帮助改进系统
- **关闭知识检索**：基于 LLM 自身知识作答

### 4. 联网搜索模块

参考 DeepSeek 界面的联网搜索开关设计：

- 可独立开关的「联网搜索」模式
- 多种搜索源可选：
  - **DuckDuckGo** — 免费，无需 API Key
  - **AnySearch** — 统一实时搜索，Key 可选
  - **Bing Search API** — 需要 Key，准确率高
  - **SerpAPI** — Google 搜索，需要 Key

### 5. 知识收藏模块

| 功能 | 说明 |
|------|------|
| **收藏回答** | 对 AI 生成回答可直接收藏到个人知识库 |
| **收藏知识片段** | 对知识来源中的文本片段可单独收藏 |
| **来源跳转** | Word/PDF 片段收藏时保留文档类型与页码，收藏弹窗与收藏页均显示「在线预览（第 N 页）」跳转链接；图片类来源显示原图 |
| **详情查看** | 点击收藏卡片弹出详情对话框，Markdown 全文渲染（含表格），并展示标签、收藏时间与来源跳转 |
| **自定义标签** | 收藏时可添加标签，便于分类管理 |
| **标签筛选** | 按标签快速检索收藏内容 |
| **全文搜索** | 支持标题和内容关键词搜索 |
| **分页浏览** | 按时间倒序排列，分页展示 |
| **用户隔离** | 每个用户只能看到自己的收藏 |

### 6. 侧边栏会话管理

| 功能 | 说明 |
|------|------|
| **新对话** | 创建新的问答会话，界面清空重新开始 |
| **历史记录** | 所有对话自动保存，按日期分组（今天/昨天/更早），点击即恢复 |
| **切换会话** | 切换时历史消息完好保留 |
| **删除会话** | 删除不再需要的对话记录 |
| **退出确认** | 点击退出按钮先弹窗确认，防止误操作 |
| **用户隔离** | 每个用户只能看到自己的对话历史 |

### 7. 知识库权限与可见性

| 功能 | 说明 |
|------|------|
| **公共知识库** | 所有用户可见可检索 |
| **个人知识库** | 仅创建者本人可见，支持新建/删除自己的知识库 |
| **检索隔离** | 向量检索阶段物理隔离，任何用户（含管理员）都不会检索到他人私人库内容 |
| **两级筛选** | 上传/管理界面先选公共或个人，再选具体知识库 |
| **条目删除** | 用户可删除自己个人知识库中的知识条目 |
| **文档管理** | 展开「知识库管理」后，每条知识文档可删除或移动到其它知识库；移动后条目可见性自动跟随目标库（公共↔个人），权限与删除一致（公共库条目仅管理员可操作） |

### 8. 知识库搜索

知识库管理页提供两种搜索模式，交互直观：

| 模式 | 触发方式 | 行为 |
|------|---------|------|
| **实时标题过滤** | 在搜索框中输入关键词（无需按 Enter） | 立即过滤列表，仅展示标题/文件名匹配的知识文档；支持字符级模糊匹配（命中率 ≥ 60%），如「环境保护法」可模糊匹配「环境保护税」 |
| **内容语义检索** | 输入关键词后按 Enter | 对知识库进行向量语义搜索，结果以**片段卡片**形式展示：每个匹配片段独立呈现、显示相关度、可展开查看全文；文档片段支持直接跳转到原始 PDF/Word 对应页码 |

搜索结果区域：
- 标题匹配到的文件卡片 + 语义检索片段同时展示，两类结果不重复
- 点击「返回全部」退出搜索模式

### 9. 个人使用看板

点击左下角用户名 → 「查看使用情况」：

- 总量统计：提问次数、知识库检索次数、联网搜索次数、知识上传数、Token 消耗、活跃天数、会话数、收藏数、个人知识库数、点赞/点踩
- 每日趋势图 + 每日明细表，支持近 7/30/90 天切换

### 10. 系统管理（管理员）

- **系统设置**：运行时切换 LLM/OCR/搜索/Embedding/Rerank 提供商，开启/关闭用户注册，设定游客免费次数，配置混合检索模式与参数（`retrieval_mode`、`dense_top_k`、`sparse_top_k`、`rrf_k`、`rerank_top_k`）
- **OpenRouter 免费模型**：一键刷新免费模型列表（严格过滤非免费模型）并应用；当前免费模型调用失败时自动重新拉取列表并随机切换一个免费模型重试，不影响使用
- **用户管理**：查看用户列表与统计看板，启用/禁用/删除用户

### 11. 聊天框工具调用

聊天输入框支持拖拽/粘贴/选择图片、Word、PDF 附件，结合用户文本意图自动路由到不同工具：

| 用户文本意图 | 触发工具 | 行为 |
|------|------|------|
| 无明确关键词（默认，安全降级） | `image_ocr` / `knowledge_summarize`（不入库） | 仅识别图片文字或提取文档内容，不写入知识库 |
| 包含“上传/保存/入库/加入知识库”等 | `image_upload` / `document_import` | 识别/解析后完整入库（复用与知识库上传页一致的服务逻辑） |
| 包含“总结/提炼/概括/提取重点”等 | `knowledge_summarize` | 调用 LLM 生成结构化摘要，默认不入库 |

- 工具执行结果以系统消息形式注入对话上下文，并通过 SSE `tool_processing`/`tool_done` 事件实时展示处理状态
- 每次工具调用（工具名、输入、输出、状态）落库到 `tool_calls` 表，便于追溯
- 出于安全考虑，游客身份禁止通过聊天框附件写入知识库，自动降级为仅识别/提取

### 12. 用户记忆与会话摘要

- **跨会话记忆**：从对话中抽取用户明确的长期偏好/事实（如饮食禁忌、学习目标），存入 `user_memories` 表；后续提问时按关键词重叠度 + 重要度检索相关记忆，作为独立上下文注入 LLM，且不逐字复述
- **敏感信息保护**：正则拦截密码、token、身份证、银行卡等敏感内容，不落库
- **会话摘要压缩**：会话消息数超过 20 条或历史字符数超过 12000 时，自动调用 LLM 将较早消息压缩为摘要，后续对话仅携带「摘要 + 最近消息」，避免上下文无限增长；摘要失败时静默降级为按条数截断，不阻断聊天
- 记忆抽取与摘要生成均为对话结束后的后台异步任务，不影响当前回答的响应速度

### 13. 系统持久化与稳定性

- **认证会话持久化**：登录会话（token 哈希、过期时间）落库到 SQLite `auth_sessions` 表，短 TTL 内存缓存加速校验；服务重启后已登录用户无需重新登录
- **游客计数持久化**：游客每日查询次数落库到 `guest_usage` 表（IP 做哈希脱敏），服务重启不会重置计数
- **聊天会话懒加载**：服务启动时不再全量加载所有历史会话到内存，改为按需加载单个会话并短期缓存；会话列表直接从数据库分页查询，避免数据规模增长后的内存压力

### 14. llm-wiki 知识编译层

借鉴 [llm-wiki](https://github.com/liangdabiao/llm-wiki-claude-agent-sdk-agentic-rag)（LLM 作为「知识编译器」）的核心思想：传统 RAG 每次提问都要重新捞取原始碎片，而 llm-wiki 先让 LLM 把资料读一遍、编译成结构化、彼此关联的百科全书，之后检索直接命中高质量百科词条。本系统以**增量共存**方式将其落地到现有问答库 RAG 之上（不照搬其 Claude Agent SDK 实现，翻译为 FastAPI + 自有 LLM Provider 体系）：

| 能力 | 说明 |
|------|------|
| **source 知识卡片** | 图片/Word/PDF 入库成功后，后台自动调 LLM 把资料提炼为结构化百科词条（概述/关键要点/相关概念/原文精选），同时写入 `wiki_pages` 表与向量库 |
| **digest 综合词条** | 对指定主题跨素材深度综合，生成带 [来源 X] 引用的持久化百科条目；既可在设置页浏览，也回写向量库参与后续问答检索 |
| **检索优先命中** | 问答检索时百科卡片稳定排序前置（不改变同组相对顺序），原始碎片兜底；卡片元数据沿用原始来源类型（image/word/pdf），来源卡仍可跳回原文 |
| **失败静默降级** | 编译完全异步、可关闭，LLM 失败/内容过短/输出过短均不阻断上传与问答链路，存量流程行为不变 |
| **在线开关** | 系统设置页可开关「知识编译」与「检索优先」（保存即生效，优先级高于 .env）；支持存量资料一键补编译（自动限流错峰） |
| **Markdown 镜像** | 编译产物同时落盘 `data/wiki/{sources,digests}/*.md`，可浏览/导出为静态知识站点 |
| **联动清理** | 删除知识来源时同步删除其百科卡片及向量条目；重新编译时先清理旧卡再写入新卡 |

**接口一览**（`/api/wiki`，需登录；标注除外）：

| 接口 | 说明 |
|------|------|
| `GET /api/wiki/status` | 编译开关与已编译词条数 |
| `GET /api/wiki/pages` | 可见百科列表（私人词条仅属主/管理员可见），支持 `kb_id`/`page_type` 过滤 |
| `GET /api/wiki/pages/{id}` | 词条详情（完整 Markdown） |
| `DELETE /api/wiki/pages/{id}` | 删除词条及其向量条目 |
| `POST /api/wiki/digest` | 生成跨素材综合词条 |
| `POST /api/wiki/compile` | 重新编译单个来源 |
| `POST /api/wiki/recompile-all` | 存量批量补编译（仅管理员，后台调度） |
| `GET・POST /api/wiki/config` | 读取/保存编译配置（保存仅管理员） |

**配置项**（backend/.env，页面设置优先）：`WIKI_COMPILE_ENABLED`、`WIKI_MAX_SOURCE_CHARS`（编译输入截断）、`WIKI_MIN_SOURCE_CHARS`（过短不编译）、`WIKI_COMPILE_INTERVAL`（相邻编译请求最小间隔秒数，缓解免费模型限流）。

### 15. 回归评测（管理员）

对问答主链路跑离线回归，量化「检索命中 / 引用忠实度 / 答案质量」，防止 prompt、检索、重排等优化引入回退。入口：侧边栏「回归评测」（仅管理员可见，路由 `/eval`）。

| 功能 | 说明 |
|------|------|
| **评测集管理** | 新建/删除评测集；用例支持单条新增、JSON 批量导入，以及**从点踩反馈一键导入**（把聊天中被点踩的用户问题去重后转为用例，真实坏例直接进回归集） |
| **用例字段** | 问题、参考答案、期望关键词、期望来源（文件名/标题），后两者用于检索命中判定 |
| **一键回归** | 对评测集全部用例重放问答链路（检索 → 生成），同一评测集同时只允许一个 run 在跑；可选指定 LLM provider 对比不同模型 |
| **run 级指标** | 检索命中率（期望关键词/期望来源命中）、引用忠实度通过率（LLM 判断答案是否被检索上下文支撑）、平均 judge 分（LLM 对照参考答案打分）、平均耗时 |
| **运行对比** | 运行历史列表 + 用例结果明细（每条用例的检索命中/忠实度/judge 分/耗时/错误），跨 run 对比指标变化判断是否回退 |

**接口一览**（`/api/eval`，仅管理员）：

| 接口 | 说明 |
|------|------|
| `GET・POST /api/eval/datasets` / `DELETE /api/eval/datasets/{id}` | 评测集列表/新建/删除 |
| `GET・POST /api/eval/datasets/{id}/cases` / `DELETE /api/eval/cases/{case_id}` | 用例列表/新增/删除 |
| `POST /api/eval/datasets/{id}/cases/import` | JSON 批量导入用例 |
| `POST /api/eval/datasets/{id}/cases/from-feedback` | 从点踩反馈（rating=dislike）去重导入用例 |
| `POST /api/eval/datasets/{id}/runs` | 发起一次回归运行（并发运行返回 409） |
| `GET /api/eval/runs` / `GET /api/eval/runs/{run_id}` | 运行历史 / 运行详情与用例结果明细 |

### 16. Redis 文件缓存

知识库原始文件（图片/Word/PDF/Word 转换预览 PDF）经 Redis 缓存加速问答来源跳转与预览：

| 特性 | 说明 |
|------|------|
| **隔离策略** | 公共知识库文件全局共享缓存；个人知识库文件按属主隔离，仅属主/管理员可命中 |
| **自动降级** | Redis 不可用时进入 60s 冷却并直读磁盘，功能不受影响；恢复后自动重试 |
| **后台写入** | 缓存写入放后台线程（远程 Redis 上行慢时不阻塞请求事件循环） |
| **可观测** | 后端日志按 `[HIT]/[MISS]/[SKIP]/[BYPASS]` 标签记录每次下发的来源与耗时；响应头 `X-File-Cache` 同值 |
| **部署形态** | compose 内置 `redis:7-alpine`（512MB + allkeys-lru 纯缓存、无持久化）；本地开发可用 `jzry-redis-dev` 容器或自有 Redis |

**配置项**（backend/.env，系统设置页优先）：`REDIS_URL`、`FILE_CACHE_ENABLED`、`FILE_CACHE_TTL`（默认 86400 秒）、`FILE_CACHE_MAX_MB`（单文件上限，默认 20MB）。

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
| **业务存储** | SQLite | 用户/会话/用量/知识库元数据/认证会话/游客计数/用户记忆/会话摘要/工具调用记录 |
| **Embedding** | Ollama / OpenAI 兼容 API | 文本向量化 |
| **LLM** | Ollama 本地 / OpenAI 兼容 API / OpenRouter 免费模型 | 问答生成 |
| **OCR** | PaddleOCR / 阿里云百炼 / 自定义 | 手写体文字识别 |
| **流式通信** | Server-Sent Events (SSE) | 实时流式输出 |
| **联网搜索** | DuckDuckGo / AnySearch / Bing / SerpAPI | 实时信息获取 |
| **容器化** | Docker + Docker Compose | 生产级部署 |
| **反向代理** | Nginx | 静态文件 + API 代理 + SSE |

---

## 快速启动（开发环境）

> 从零到能提问约 10 分钟（不含模型下载）。按 5 步顺序执行即可，无需修改任何代码。

### 第 1 步 · 环境准备

| 依赖 | 版本要求 | 验证命令 | 安装方式 |
|------|---------|---------|---------|
| Python | **3.10 ~ 3.12**（推荐 3.11） | `python3 --version` | [python.org](https://www.python.org/) 或 pyenv |
| Node.js | >= 20.19（推荐 22，Vite 8 要求） | `node --version` | `brew install node` 或 [nodejs.org](https://nodejs.org/) |
| Git | 任意 | `git --version` | 系统自带或 [git-scm.com](https://git-scm.com/) |
| （方案 A 需要）Ollama | latest | `ollama --version` | [ollama.com](https://ollama.com/) |

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

任选 OpenAI 兼容提供商（DeepSeek / 通义千问 / OpenRouter 免费模型等），在 `.env` 中修改：

```bash
LLM_PROVIDER=openai
API_BASE_URL=https://api.deepseek.com/v1
API_KEY=sk-你的密钥
API_MODEL=deepseek-chat

# Embedding 也需指向可用服务（二选一）：
#   本地：EMBEDDING_PROVIDER=ollama 并执行 `ollama pull bge-m3`
#   云端：EMBEDDING_PROVIDER=openai + EMBEDDING_API_BASE / EMBEDDING_API_KEY / EMBEDDING_API_MODEL
```

> OCR 默认 `OCR_PROVIDER=local`（PaddleOCR，已随 requirements 安装，首次识别时自动下载模型）；也可切 `aliyun`（需 AccessKey）或 `custom_api`。
> 以上所有配置启动后均可在「系统管理 → 系统设置」页在线切换，无需改文件重启。

### 第 4 步 · 启动

**推荐：后台驻留启动**（进程脱离终端，关闭终端后服务不退出）：

```bash
bash scripts/dev-daemon.sh start    # 启动后端 + 前端（自动先清理旧进程）
bash scripts/dev-daemon.sh status   # 查看运行状态与端口健康
bash scripts/dev-daemon.sh stop     # 停止服务
```

日志位于 `.dev-logs/backend.log` 与 `.dev-logs/frontend.log`。

**备选：前台启动**（适合调试，终端关闭服务即停止）：

```bash
# 终端 1 — 后端
cd backend && source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2 — 前端
cd frontend && npm run dev
```

也可一键前台启动：`bash start.sh`（Ctrl+C 停止全部服务）。

### 第 5 步 · 首次访问与验证清单

打开 <http://localhost:5173>，点击「立即注册」——**第一个注册账号自动成为管理员**（拥有系统管理/用户管理权限，后续用户由管理员在用户管理中维护）。

- [ ] `curl http://localhost:8000/api/health` 返回正常
- [ ] <http://localhost:8000/docs> 能看到交互式 API 文档
- [ ] 登录页提供游客模式，不注册也能直接体验对话
- [ ] 上传一张手写笔记照片 → 后台 OCR 识别出文字 → 切「知识入库」确认入库（进度实时可见）→ 回到对话页提问，能得到带来源引用的回答
- [ ] 「知识库上传 → 入库历史」Tab 能看到刚入库的记录，「查看解析文档」可回溯解析文本
- [ ] 管理员账号侧边栏出现「回归评测」，可新建评测集并发起运行

### 关于仓库里“没有”的东西

`.env`（密钥）、`data/`（用户数据/向量库/上传图片）、日志与依赖目录均被 `.gitignore` 排除，不会随仓库分发；新克隆后运行时目录全部自动创建，无需手动准备。唯一需要自己准备的就是 `backend/.env`。

| 地址 | 说明 |
|------|------|
| http://localhost:5173 | 前端主界面 |
| http://localhost:8000/docs | 交互式 API 文档 |

---

## Docker 部署（本地）

使用 Docker Compose 可一键部署整个系统，无需手动安装 Python 和 Node.js。

### 前置要求

| 依赖 | 说明 |
|------|------|
| **Docker** | >= 24.0，[安装 Docker Desktop](https://docs.docker.com/get-docker/) |
| **Docker Compose** | Docker Desktop 已内置 |

### 快速开始

```bash
# 1. 进入项目目录
cd jianziruyang

# 2. 配置 .env（可选，默认使用宿主机 Ollama）
#    cp backend/.env.example backend/.env

# 3. 构建并启动
docker compose up -d

# 4. 查看日志
docker compose logs -f
```

访问 `http://localhost:5173` 即可使用。**首次注册的用户自动成为管理员**。

### 后端镜像体积：瘦身构建（可选）

后端镜像默认全量构建（含本地重排序模型依赖 torch 与 LibreOffice），体积约 2.3GB。如果你的重排序使用远端提供商（如 `RERANKER_PROVIDER=dashscope`），可用瘦身模式构建，体积降到 ~574MB（实测缩小约 75%）：

```bash
docker compose build --build-arg SLIM=1 backend
docker compose up -d
```

瘦身模式的影响（均有自动降级，不影响主流程）：
- 本地 CrossEncoder 重排序不可用，需使用远端重排序提供商；
- Word 原始排版 PDF 预览自动降级：管理页按解析内容逻辑分页展示，问答来源页提供下载原文（不影响解析与问答）。

### 数据持久化

compose 已配置以下挂载，重启/重建容器数据不丢失：

| 宿主机目录 | 容器内路径 | 内容 |
|-----------|------------|------|
| `./data` | `/app/data` | FAISS 向量库、上传图片、收藏等 |
| `./data/db` | `/app/backend/data` | SQLite 数据库（用户/会话/用量/知识库元数据） |
| `./logs/backend` | `/app/backend/log` | 后端日志（按日期 `YYYY-MM-DD.log`） |
| `./logs/frontend` | `/var/log/nginx` | Nginx 访问/错误日志 |
| `./backend/.env` | `/app/backend/.env` | 环境变量（只读挂载） |

> **注意**：Nginx 日志写入挂载目录 `logs/frontend/`（`access.log` / `error.log`），因此 `docker logs <frontend 容器>` 为空属设计行为，排查前端请求请查看该目录。
>
> **Redis 无需持久化**：compose 中的 `redis` 服务为纯文件缓存（512MB + allkeys-lru、`--save ""` 关闭落盘），不挂载任何 volume，重启/丢失缓存仅影响预热速度，不影响数据正确性。

### 自定义配置

**使用云端 API：**

```bash
# 修改 backend/.env
LLM_PROVIDER=openai
API_BASE_URL=https://api.deepseek.com/v1
API_KEY=sk-your-key-here
API_MODEL=deepseek-chat

# 重启
docker compose restart
```

**使用本地 Ollama：**

后端容器通过 `host.docker.internal` 访问宿主机 Ollama，默认配置即可。

---

## 阿里云服务器部署（本地打包推送）

如果你的开发机是 Mac（M 芯片或 Intel），推荐采用**本地构建 → 导出镜像 → 上传到服务器 → 加载运行**的方式。这样服务器只需运行容器，不需要执行构建过程，在低配 ECS 上尤其适用。

项目已提供自动化部署脚本 `scripts/deploy-aliyun.sh`，支持一键完成整个流程。

### 前置准备

| 资源 | 说明 | 推荐配置 |
|------|------|---------|
| **阿里云 ECS** | 云服务器 | 2 核 4G 以上（如用 Ollama 本地模型需 8G 以上） |
| **操作系统** | 建议 Ubuntu 22.04 LTS | 预装 Docker 的镜像 |
| **服务器 Docker** | >= 24.0，含 Compose v2 | 未安装时执行：`curl -fsSL https://get.docker.com \| sh && systemctl enable --now docker` |
| **安全组规则** | 开放端口 | 5173（默认前端端口）；配置域名后另开 80/443 |
| **本地环境** | Docker Desktop | macOS 上安装 Docker Desktop |
| **SSH 密钥** | 免密登录服务器 | 建议配置，方便 `scp` 传输 |

---

### 部署流程概览

```
┌──────────────────────────────┐     ┌──────────────────────────────┐
│         本地 Mac             │     │       阿里云 ECS             │
│                              │     │                              │
│  1. docker build 构建镜像     │     │  5. tar xzf 解压部署包        │
│  2. docker save → tar.gz     │     │  6. server-load 加载镜像      │
│  3. 打包部署文件（含脚本）     │     │  7. 配置 backend/.env        │
│  4. scp 上传到服务器  ──────→ │     │  8. server-start 启动服务     │
│                              │     │  9. 配置域名 & HTTPS（可选）   │
└──────────────────────────────┘     └──────────────────────────────┘
```

---

### 第一步：配置服务器信息

编辑 `scripts/deploy-aliyun.sh`，修改开头的配置项：

```bash
REMOTE_USER="root"              # 服务器 SSH 用户名
REMOTE_HOST="your-server-ip"    # 服务器公网 IP 或域名
REMOTE_DIR="/opt/jianziruyang"  # 服务器上的部署目录
```

> 建议先配置 SSH 免密登录，避免后续上传时反复输入密码：
> ```bash
> ssh-copy-id root@your-server-ip
> ```

### 第二步：在本地构建并上传（一键操作）

```bash
cd /path/to/jianziruyang

# 一键完成：构建镜像 → 导出 tar.gz → 上传到服务器
bash scripts/deploy-aliyun.sh all
```

该命令依次执行：

| 步骤 | 命令 | 说明 |
|------|------|------|
| **构建** | `docker build ... --platform linux/amd64` | 构建后端和前端镜像，指定 amd64 架构以兼容服务器 |
| **导出** | `docker save ... | gzip` | 导出为 `docker-images/jianzirumian-deploy.tar.gz` |
| **上传** | `scp 部署包 root@服务器:/opt/jianziruyang/` | 通过 SSH 上传 |

> **关于架构**：Mac M 芯片是 arm64 架构，阿里云 ECS 通常是 amd64。脚本中的 `--platform linux/amd64` 确保镜像能在服务器上运行。如果你确定服务器也是 arm64，可以去掉此参数以提升性能。
>
> **首次构建**需要下载 Python 3.11 和 Node.js 22 的基础镜像，以及安装 Python 依赖，耗时 2~5 分钟。后续构建会利用缓存，仅需数秒。
>
> **关于基础镜像源**：国内访问 Docker Hub 经常失败（镜像加速器返回 403/超时），两个 Dockerfile 默认通过 `ARG REGISTRY` 走 DaoCloud 公共代理（`docker.m.daocloud.io/library/`），不经过 docker.io。如该代理失效，可换源重建：
>
> ```bash
> REGISTRY=docker.io/library/ bash scripts/deploy-aliyun.sh build   # 或其他可用代理前缀
> ```
>
> **关于镜像体积（瘦身构建）**：脚本默认开启 `SLIM=1` 瘦身模式，后端镜像不装 torch / sentence-transformers / LibreOffice，实测镜像从 2.27GB 降到 574MB，导出包从 ~745MB 降到 ~206MB，低带宽上传耗时大幅缩短。前提是 `backend/.env` 中 `RERANKER_PROVIDER` 使用远端提供商（如 `dashscope`）；Word 原始排版预览会自动降级为解析内容逻辑分页展示。需要本地重排序 + 原始排版 PDF 预览时：
>
> ```bash
> SLIM=0 bash scripts/deploy-aliyun.sh all   # 全量模式构建（体积大）
> ```

#### 分步执行（如需手动控制）

你也可以分步执行，更灵活地控制流程：

```bash
# 1. 只构建镜像
bash scripts/deploy-aliyun.sh build

# 2. 只导出部署包
bash scripts/deploy-aliyun.sh save

# 3. 只上传到服务器（需要在脚本中配置好服务器信息）
bash scripts/deploy-aliyun.sh upload
```

导出后的文件在 `docker-images/` 目录：

```
docker-images/
├── jianzirumian-deploy.tar.gz       # 完整部署包（推荐使用）
├── jianzirumian-backend.tar.gz      # 后端镜像
├── jianzirumian-frontend.tar.gz     # 前端镜像
├── docker-compose.yml               # Docker Compose 配置
├── nginx.conf                       # Nginx 配置
└── .env.example                     # 环境变量模板
```

也可手动拷贝部署包到服务器：

```bash
scp docker-images/jianzirumian-deploy.tar.gz root@your-server-ip:/opt/
```

### 第三步：在服务器上加载镜像并启动

SSH 登录服务器：

```bash
ssh root@your-server-ip
```

以下所有操作在服务器上执行：

#### 3.1 解压部署包

```bash
cd /opt/jianziruyang
tar xzf jianzirumian-deploy.tar.gz
```

解压后目录结构：

```
/opt/jianziruyang/
├── jianzirumian-backend.tar.gz    # 后端镜像文件
├── jianzirumian-frontend.tar.gz   # 前端镜像文件
├── docker-compose.yml             # Docker Compose 配置
├── nginx.conf                     # Nginx 配置
├── .env.example                   # 环境变量模板
└── scripts/
    └── deploy-aliyun.sh           # 部署助手脚本
```

#### 3.2 加载 Docker 镜像

```bash
bash scripts/deploy-aliyun.sh server-load
```

此命令会将两个 tar.gz 文件解压并通过 `docker load` 导入到本地镜像仓库。

验证镜像已加载：

```bash
docker images | grep jianzirumian
```

输出示例：

```
jianzirumian-backend    latest    2a3b4c5d6e7f    10 minutes ago    1.2GB
jianzirumian-frontend   latest    8f9a0b1c2d3e    10 minutes ago     45MB
```

#### 3.3 配置环境变量

首次执行 `server-start` 时，脚本会自动用 `.env.example` 创建 `backend/.env` 并退出提示配置，无需手动复制。按实际使用的服务编辑：

```bash
vim backend/.env
```

**如果使用云端 API（推荐，适合低配服务器）：**

```env
LLM_PROVIDER=openai
API_BASE_URL=https://api-inference.modelscope.cn/v1
API_KEY=sk-your-modelscope-key
API_MODEL=deepseek-ai/DeepSeek-V4-Flash

EMBEDDING_PROVIDER=openai
EMBEDDING_API_BASE=https://api-inference.modelscope.cn/v1
EMBEDDING_API_KEY=sk-your-modelscope-key
EMBEDDING_MODEL=BAAI/bge-m3

OCR_PROVIDER=aliyun
ALIYUN_OCR_AK_ID=your-access-key-id
ALIYUN_OCR_AK_SECRET=your-access-key-secret

SEARCH_PROVIDER=duckduckgo
```

**如果使用本地 Ollama（需要高配服务器，建议 8G 内存以上）：**

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen2.5:7b

EMBEDDING_PROVIDER=ollama
EMBEDDING_API_BASE=http://host.docker.internal:11434
EMBEDDING_MODEL=bge-m3:latest
```

> **关于 `host.docker.internal`**：Docker 容器通过此地址访问宿主机上运行的服务（如 Ollama）。如果 Ollama 也运行在 Docker 中，需将 `OLLAMA_BASE_URL` 改为 `http://ollama:11434` 并在 docker-compose.yml 中添加 Ollama 服务。

#### 3.4 启动服务

```bash
bash scripts/deploy-aliyun.sh server-start
```

如果尚未配置 `backend/.env`，脚本会提示并退出。配置好后重新执行即可。

首次启动会自动创建持久化数据目录 `data/`（向量库、上传图片）与 `data/db/`（SQLite 数据库）。

查看启动状态：

```bash
docker compose ps
```

预期输出：

```
NAME                  IMAGE                        STATUS         PORTS
jianziruyang-redis    redis:7-alpine               Up 2 minutes   6379/tcp
jianziruyang-backend  jianzirumian-backend:latest  Up 2 minutes   0.0.0.0:8000->8000/tcp
jianziruyang-frontend jianzirumian-frontend:latest Up 2 minutes   0.0.0.0:5173->80/tcp
```

访问 `http://服务器IP:5173` 即可打开系统。

> 若无法打开，请检查阿里云安全组是否放行了 **5173** 端口。
>
> **首次注册的用户自动成为管理员**，请先注册自己的账号。

#### 3.5 其他服务器操作

```bash
# 查看实时日志
bash scripts/deploy-aliyun.sh server-logs

# 仅查看后端日志
docker compose logs -f backend

# 查看资源占用
bash scripts/deploy-aliyun.sh server-status

# 停止服务
bash scripts/deploy-aliyun.sh server-stop

# 重启服务
bash scripts/deploy-aliyun.sh server-restart
```

---

### 第四步：更新部署

当代码有变更时，按以下步骤更新服务器上的服务：

```bash
# 1. 本地重新构建并上传
bash scripts/deploy-aliyun.sh all

# 2. SSH 到服务器，更新并重启
ssh root@your-server-ip
cd /opt/jianziruyang

# 解压新部署包（覆盖旧文件）
tar xzf jianzirumian-deploy.tar.gz

# 加载新镜像并重启服务
bash scripts/deploy-aliyun.sh server-update
```

> 数据目录 `data/` 与 `data/db/` 不会被覆盖，你的向量库、上传图片、SQLite 业务数据都安全保留。
>
> 更新会同步生效最新优化：后端连接池复用与并行问答流水线、前端构建拆包，以及 nginx 的 gzip/缓存/50m 上传上限配置。

---

### 第五步：配置域名与 HTTPS（推荐）

#### 5.1 选择访问方式

- **方式 A（简单，无域名）**：将前端端口映射由 `5173:80` 改为 `80:80`，直接用 `http://服务器IP` 访问：

  ```bash
  sed -i 's/"5173:80"/"80:80"/' docker-compose.yml
  docker compose up -d
  ```

- **方式 B（域名 + HTTPS，推荐）**：保持 `5173:80` 不变，在宿主机安装 Nginx 做反向代理（见 5.2/5.3），容器继续监听 5173。

#### 5.2 安装 Nginx + 申请 SSL 证书

```bash
# 安装 Nginx 和 certbot
apt update && apt install -y nginx certbot python3-certbot-nginx

# 申请 Let's Encrypt 证书并自动配置 Nginx
certbot --nginx -d your-domain.com
```

#### 5.3 配置反向代理

创建 `/etc/nginx/sites-available/jianziruyang`：

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    # 前端
    location / {
        proxy_pass http://127.0.0.1:5173;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # API 代理（SSE 必须禁用缓冲）
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_cache off;
    }

    # 上传文件
    location /uploads/ {
        proxy_pass http://127.0.0.1:8000/uploads/;
    }
}

# HTTP → HTTPS 重定向
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}
```

启用并重载 Nginx：

```bash
ln -s /etc/nginx/sites-available/jianziruyang /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

---

### 第六步：配置 Ollama 服务（仅本地模型需要）

如果选择在服务器上本地运行 LLM 模型：

```bash
# 安装 Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 拉取所需模型
ollama pull qwen2.5:7b       # LLM 模型（约 4GB）
ollama pull bge-m3:latest    # Embedding 模型（约 1.2GB）

# 验证服务可用
curl http://localhost:11434/api/tags
```

> **内存提醒**：`qwen2.5:7b` 运行约需 6-8GB 内存。如服务器内存不足（4GB），建议：
> - 使用云端 API 替代本地 Ollama（推荐）
> - 或使用更小的模型：`ollama pull qwen2.5:0.5b`

---

### 第七步：定时备份（推荐）

设置 crontab 自动备份数据：

```bash
# 编辑 crontab
crontab -e

# 每天凌晨 3 点备份数据（添加以下行）
0 3 * * * cp -r /opt/jianziruyang/data /opt/jianziruyang/backups/data_$(date +\%Y\%m\%d) && find /opt/jianziruyang/backups -type d -mtime +30 -exec rm -rf {} +
```

> 以上命令自动保留最近 30 天的备份。

---

### 部署检查清单

部署完成后，逐项确认：

- [ ] 访问 `http://服务器IP:5173` 能打开登录页面
- [ ] 登录页「系统使用指导」可正常打开指南页（/guide）
- [ ] 首次注册的账号自动成为管理员
- [ ] 可以在"知识库管理"中看到已上传的知识条目
- [ ] 知识库搜索框输入关键词可实时过滤标题列表（支持模糊匹配）
- [ ] 按 Enter 后显示语义片段卡片，可展开查看全文及来源
- [ ] 可以正常发送问答消息（流式输出）
- [ ] 知识来源能正确显示和展开
- [ ] 知识收藏功能正常（收藏/列表/删除）
- [ ] 管理员可在系统设置中开关用户注册
- [ ] HTTPS 证书有效（如已配置域名）
- [ ] 数据目录 `data/` 已持久化（重启容器后数据不丢失）
- [ ] 日志正常落盘：`logs/backend/YYYY-MM-DD.log` 与 `logs/frontend/access.log` 有实时输出
- [ ] `docker compose ps` 显示 redis / backend / frontend 三个容器均为 Up
- [ ] 「知识库上传 → 入库历史」Tab 列出本人成功入库记录，可跳转解析文档详情
- [ ] 管理员侧边栏可见「回归评测」，能新建评测集、导入用例并发起回归运行

---

### 阿里云部署注意事项

| 事项 | 说明 |
|------|------|
| **数据持久化** | 向量库/上传保存在 `./data/`，SQLite 在 `./data/db/`，日志在 `./logs/`（均映射自容器），更新或重启容器不会丢失 |
| **架构兼容** | 本地构建时需指定 `--platform linux/amd64` 以匹配服务器 CPU 架构 |
| **内存管理** | 若使用 Ollama 本地模型，建议 ECS 至少 8GB 内存。推荐使用云端 API 节省内存 |
| **磁盘扩容** | 笔记图片较多时注意监控磁盘使用，可在阿里云控制台扩容云盘 |
| **安全组** | 仅开放必要的端口（80/443），SSH 端口建议改为非标准端口或使用密钥登录 |
| **自动重启** | Docker Compose 已配置 `restart: unless-stopped`，服务器重启后服务自动恢复 |
| **监控告警** | 可在阿里云控制台设置云监控，监控 CPU / 内存 / 磁盘使用率 |

---

## 使用指南

> 系统内置了面向新用户的完整使用指南：登录页点击「**系统使用指导**」即可打开（地址 `/guide`，无需登录）。
> 以下为功能流程的简要说明。

### 知识上传流程（两段式）

```
【文件上传】选择图片（单张→图片编辑器旋转/裁剪；批量→待选列表）或 Word / PDF
→ 落盘登记 → 后台多线程并发解析（图片 OCR / 文档按页提取，表格转 Markdown）
→ 文件列表实时显示解析进展，完成后可预览原始文档（支持页码跳转）
【知识入库】选择目标知识库（公共/个人 → 具体库）
→ 查看/编辑解析效果（可一键润色）→ 确认入库（进度实时显示直到全部成功）
→ 文本分割 → 向量化 → FAISS 存储 + （后台）llm-wiki 百科卡片自动编译（可开关）
```

### 入库历史与解析文档回溯

「知识库上传」页第三个 Tab「入库历史」，按入库时间倒序列出本人成功入库的记录：

- **知识来源**：点击预览原始文件（图片 / Word 原始排版 PDF / PDF 原件）；源文件已被删除时置灰标注「源文件已删除」
- **解析文档**：点击「查看解析文档」跳转详情页，按页回溯解析后的文本片段（图片为整篇单片段），并显示入库片段数

### 回归评测（管理员）

侧边栏「回归评测」：建评测集 → 加用例（手工新增 / JSON 批量导入 / 从聊天点踩反馈一键导入）→ 一键回归 → 查看 run 级指标（检索命中率 / 引用忠实度通过率 / 平均 judge 分 / 平均耗时）与用例明细，跨 run 对比判断优化是否引入回退。同一评测集同时只允许一个 run 运行。

### 问答交互流程

```
输入问题 → 意图改写(超时保护) → 〔知识库检索(百科词条优先前置) → Rerank 重排 → 证据充分性判断(不足则改写重检一次) ∥ 联网搜索〕并行
→ 注入上下文 → LLM 流式生成 → 引用编号核对(越界追加提醒)
全程流水线进度条实时展示每一步状态
```

### 双模式切换

输入框上方有两个开关：

- **知识检索**：绿色为开启，基于知识库内容回答
- **联网搜索**：绿色为开启，从互联网获取实时信息

### 知识库搜索

在「知识库管理」页面，搜索框提供两种模式：

| 操作 | 效果 |
|------|------|
| **直接输入关键词** | 实时过滤列表，仅显示标题/文件名匹配的知识文档；支持字符级模糊匹配，无需按 Enter |
| **输入后按 Enter** | 触发向量语义搜索，结果以**片段卡片**展示：含相关度评分、点击「展开」可查看全文，图片来源可查看原图，PDF/Word 文档片段可直接跳转到对应页码 |

点击「返回全部」或清除搜索框内容即可退出搜索模式。

### llm-wiki 知识编译（百科词条）

笔记入库后，系统会在后台自动把资料「编译」为结构化的百科词条（概述/关键要点/相关概念/原文精选）；提问时检索会优先命中这些百科卡片，回答更完整、更有条理。

- **自动生效**：编译默认开启、全程后台进行，无需任何操作；词条可见范围与原始资料一致（公共库全员可见，私人笔记仅属主与管理员可见）
- **知识百科页**：所有登录用户可在左侧「知识百科」浏览可见词条（含自己个人知识库编译出的私人词条），支持类型/知识库筛选、Markdown 详情查看与删除自有词条
- **问答来源标记**：回答底部展开来源时，百科卡片来源会标注词条标题，仍可跳回原始文档/图片/页码
- **管理员入口**（系统管理 → 系统设置 →「llm-wiki 知识编译」区块）：开关编译/检索优先、对存量资料一键补编译、按主题生成跨素材综合词条（digest）、浏览与删除词条（详情见功能特性「14. llm-wiki 知识编译层」）

### 知识收藏

- 在问答界面，点击 AI 回答底部的"收藏回答"按钮可收藏整段回答
- 展开知识来源后，可点击"收藏片段"收藏具体的知识文本
- 来源为 Word/PDF 时，收藏弹窗显示「来源文档」跳转链接（在线预览并定位页码），不再显示破损图片；图片类来源仍显示来源图片
- 在左侧"知识收藏"页面可浏览、搜索、管理所有收藏；点击卡片可查看详情（Markdown 全文 + 来源跳转）
- 收藏时可添加自定义标签，后续可按标签筛选

### 个人中心

- 点击左下角用户名弹出菜单：**修改个人信息**（用户名/联系方式/密码）、**查看使用情况**（使用看板）
- 使用看板展示提问/检索/搜索/上传次数、Token 消耗、活跃天数等，支持每日趋势图与近 7/30/90 天切换

---

## API 文档

### 认证相关

#### `POST /api/auth/register` — 注册

```json
// 请求
{ "username": "user", "password": "pass123", "contact": "phone或邮箱" }
// 响应
{ "success": true, "user": { "id": "...", "username": "user", "role": "admin" } }
```

#### `POST /api/auth/login` — 登录

```json
// 请求
{ "username": "user", "password": "pass123" }
// 响应
{ "success": true, "token": "xxx", "user": { "id": "...", "username": "user", "role": "admin" } }
```

#### `GET /api/auth/me` — 获取当前用户信息

#### `PUT /api/auth/me` — 修改当前用户个人信息

```json
// 请求（改密码时需 old_password + new_password）
{ "username": "新用户名", "contact": "联系方式", "old_password": "...", "new_password": "..." }
// 响应
{ "success": true, "user": { ... } }
```

#### `GET /api/auth/me/stats?days=30` — 个人使用看板

返回每日用量明细（提问/知识库检索/联网搜索/上传/Token）与总量统计（活跃天数、会话数、收藏数、知识库数、点赞点踩等）。

#### `GET /api/auth/users` — 获取用户列表（管理员）

#### `PUT /api/auth/users/{id}/active` — 启用/禁用用户（管理员）

#### `DELETE /api/auth/users/{id}` — 删除用户（管理员）

### 聊天相关

#### `POST /api/chat/send` — 发送消息（SSE 流式）

请求体：

```json
{
  "session_id": "可选，不传自动创建",
  "query": "用户问题",
  "use_knowledge": true,
  "use_search": false,
  "llm_provider": null
}
```

SSE 事件流（`status.phase` 即流水线阶段，前端据此渲染步骤进度条）：

```
data: {"event":"status","phase":"tool_start","message":"正在处理附件..."}      # 有附件时
data: {"event":"status","phase":"tool_done","tool_calls":[...]}
data: {"event":"status","phase":"rewriting","message":"正在理解你的问题..."}
data: {"event":"status","phase":"retrieving","message":"正在检索知识库..."}
data: {"event":"status","phase":"reranking","message":"正在对检索结果重排序..."}
data: {"event":"status","phase":"gap_checking","message":"正在评估证据是否充分..."}
data: {"event":"status","phase":"rechecking","message":"证据不足，正在改写重检..."}  # 仅证据不足时
data: {"event":"status","phase":"retrieved","sources":[...]}
data: {"event":"status","phase":"generating","message":"正在生成回答..."}
data: {"event":"token","content":"回答文本..."}
data: {"event":"status","phase":"citations_checked","invalid_citations":[...]}   # 仅发现越界引用时
data: {"event":"done","session_id":"xxx","message":{...}}
```

#### `GET /api/chat/sessions` — 获取会话列表（仅当前用户）

#### `POST /api/chat/sessions` — 创建新会话

#### `DELETE /api/chat/sessions/{id}` — 删除会话（仅本人）

#### `GET /api/chat/history/{id}` — 获取聊天历史

### 知识库相关

#### `POST /api/knowledge/upload` — 上传图片并处理

表单参数：`file`（图片文件）、`ocr_provider`（可选，OCR 引擎）、`visibility`、`kb_id`（目标知识库）、`override_text`（可选，用户编辑/润色后的文本，非空时跳过重新 OCR）

#### `POST /api/knowledge/upload/batch` — 批量上传

#### `POST /api/knowledge/preview-ocr` — 预览 OCR 结果（不存储）

#### `GET /api/knowledge/stats` — 知识库统计

#### `GET /api/knowledge/entries` — 知识库条目列表（按来源图片分组）

支持 `q` 参数按标题/文件名模糊搜索（字符级模糊匹配，命中率 ≥ 60%）。

#### `POST /api/knowledge/search` — 知识库语义检索

返回匹配的文档片段列表，每个片段包含：`text`（片段文本）、`relevance`（相关度 0~99%）、`source_image`（来源文件）、`page_number`（所在页码）、`source_type`（image/word/pdf）、`pdf_preview_path`（PDF 预览路径）、`title`（文档标题）、`kb_name`（知识库名称）等完整上下文字段。

#### `GET /api/knowledge/ingest-history` — 入库历史列表（仅本人）

参数 `limit` / `offset`；返回本人成功入库记录（入库时间、来源文件、标题、类型、入库片段数、源文件是否仍存在、是否有解析内容）。

#### `GET /api/knowledge/ingest-history/{record_id}/content` — 入库记录解析文档（仅本人）

返回该记录的解析文本片段列表（Word/PDF 带页码，图片为整篇单片段）与记录概要。

### 回归评测相关（仅管理员）

#### `GET・POST /api/eval/datasets`、`DELETE /api/eval/datasets/{id}` — 评测集管理

#### `GET・POST /api/eval/datasets/{id}/cases`、`DELETE /api/eval/cases/{case_id}` — 用例管理

#### `POST /api/eval/datasets/{id}/cases/import` — JSON 批量导入用例

#### `POST /api/eval/datasets/{id}/cases/from-feedback` — 从点踩反馈去重导入用例

#### `POST /api/eval/datasets/{id}/runs` — 发起回归运行（并发运行 409）

#### `GET /api/eval/runs`、`GET /api/eval/runs/{run_id}` — 运行历史 / 运行详情与用例明细

### 知识收藏相关

#### `POST /api/bookmarks` — 创建收藏

```json
// 请求
{
  "title": "收藏标题",
  "tags": ["标签1", "标签2"],
  "content": "收藏的文本内容",
  "source_type": "answer | knowledge",
  "source_info": { "source_image": "图片路径.jpg" }
}
```

#### `GET /api/bookmarks` — 获取当前用户收藏列表

#### `DELETE /api/bookmarks/{id}` — 删除收藏（仅本人）

### 系统配置相关

#### `GET /api/config` — 获取完整系统配置

```json
{
  "llm_provider": "ollama",
  "registration_enabled": true,
  "llm_config": { ... },
  "search_config": { ... }
}
```

#### `GET /api/config/public` — 公开配置（无需登录）

仅返回 `{ "registration_enabled": ..., "guest_query_limit": ... }`，供登录页与游客提示条展示管理员配置的最新值。

#### `POST /api/config/provider` — 切换运行时提供商

#### `POST /api/config/registration` — 开启/关闭用户注册

#### `POST /api/config/test-llm` — 测试 LLM 连接

#### `POST /api/config/test-search` — 测试搜索连接

#### `POST /api/config/test-embedding` — 测试 Embedding 连接

#### `GET /api/health` — 健康检查

---

## 项目结构

```
jianziruyang/
│
├── backend/                          # Python 后端 (FastAPI)
│   ├── .env / .env.example           # 环境变量
│   ├── requirements.txt              # Python 依赖
│   └── app/
│       ├── main.py                   # 应用入口 + CORS + 鉴权中间件 + 路由注册
│       ├── config.py                 # 全局配置（读取 .env）
│       │
│       ├── api/                      # HTTP 路由层
│       │   ├── chat.py               #   聊天 API（SSE 流式 + 会话管理）
│       │   ├── auth_api.py           #   认证 API（注册/登录/用户管理）
│       │   ├── config_api.py         #   配置 API（提供商切换 + 连接测试）
│       │   ├── knowledge.py          #   知识库 API（上传 + OCR + CRUD + 入库历史）
│       │   ├── wiki.py               #   知识百科 API（llm-wiki 编译层）
│       │   ├── eval.py               #   回归评测 API（仅管理员）
│       │   └── bookmark_api.py       #   知识收藏 API
│       │
│       ├── core/                     # 核心抽象层（工厂模式）
│       │   ├── db.py                 #   SQLite 连接管理（WAL + 自动迁移）
│       │   ├── llm.py                #   LLM 抽象：Ollama / OpenAI / Custom
│       │   ├── openrouter.py         #   OpenRouter 免费模型拉取/过滤/降级切换
│       │   ├── ocr.py                #   OCR 抽象：PaddleOCR / 阿里云 / 自定义
│       │   ├── embeddings.py         #   Embedding 抽象
│       │   ├── vector_store.py       #   FAISS 向量库（检索权限前置）
│       │   ├── intent_rewriter.py    #   意图改写（LLM 查询重写）
│       │   ├── evidence_checker.py   #   轻量 Self-RAG（证据充分性判断 + 引用编号核对）
│       │   └── reranker.py           #   检索结果重排序（本地/DashScope）
│       │
│       ├── services/                 # 业务服务层
│       │   ├── auth.py               #   认证服务（用户管理 + Token 会话 + 个人信息修改）
│       │   ├── chat.py               #   聊天服务（多会话 + 知识检索 + SSE）
│       │   ├── bookmark.py           #   知识收藏服务
│       │   ├── knowledge.py          #   知识处理（OCR/文档解析 + 向量化 + 权限检索）
│       │   ├── config_manager.py     #   运行时配置管理器
│       │   ├── guest_limiter.py      #   游客免费次数限制
│       │   ├── records.py            #   上传记录管理（含入库历史查询）
│       │   ├── evaluation.py         #   回归评测引擎（检索命中/忠实度/judge 打分）
│       │   ├── usage.py              #   用量统计（按用户按天聚合）
│       │   └── web_search.py         #   联网搜索
│       │
│       └── models/
│           └── schemas.py            # Pydantic 数据模型
│
├── frontend/                         # Vue 3 前端
│   ├── index.html
│   ├── vite.config.js                # Vite 配置（含 API 代理）
│   └── src/
│       ├── main.js                   # 应用入口
│       ├── App.vue                   # 根组件
│       ├── style.css                 # 全局样式
│       ├── router/index.js           # 路由配置
│       ├── stores/
│       │   ├── chat.js               # 聊天状态（Pinia）
│       │   └── auth.js               # 认证状态（Pinia）
│       ├── api/index.js             # API 封装
│       ├── views/
│       │   ├── ChatView.vue          # 问答主界面
│       │   ├── LoginView.vue         # 登录页（含系统使用指导入口）
│       │   ├── RegisterView.vue      # 注册页
│       │   ├── GuideView.vue         # 系统使用指南页（/guide，免登录）
│       │   ├── KnowledgeUpload.vue   # 知识上传页（文件上传/知识入库/入库历史三 Tab）
│       │   ├── IngestDetailView.vue  # 入库记录解析文档详情页
│       │   ├── ManagementView.vue    # 知识库管理 + 系统设置
│       │   ├── WikiView.vue          # 知识百科页（llm-wiki 词条浏览）
│       │   ├── EvalView.vue          # 回归评测页（仅管理员）
│       │   ├── BookmarksView.vue     # 知识收藏页
│       │   ├── UserManagementView.vue # 用户管理页
│       │   └── ConfigView.vue        # 提供商配置页
│       ├── utils/
│       │   ├── image.js              # 图片压缩/预处理
│       │   └── imageQuality.js       # 图片清晰度（模糊度）检测
│       └── components/
│           ├── AppSidebar.vue        # 侧边栏（含用户菜单/信息修改/使用看板弹窗）
│           ├── ChatMessage.vue       # 消息组件（含收藏/点赞点踩）
│           └── ChatInput.vue         # 输入组件
│
├── backend/data/                     # SQLite 数据库 app.db（自动生成）
├── data/                             # 运行时数据（自动生成）
│   ├── vector_store/                 # FAISS 向量索引
│   ├── uploads/                      # 上传图片
│   └── knowledge/                    # 知识目录
│
├── scripts/
│   ├── deploy-aliyun.sh              # 阿里云部署助手（构建/导出/上传/服务器端操作）
│   └── dev-daemon.sh                 # 本地开发服务后台驻留管理（start/stop/status）
├── docker-images/                    # 导出的镜像与部署包（脚本生成）
├── Dockerfile.backend                # 后端 Dockerfile（基础镜像默认走 DaoCloud 代理）
├── Dockerfile.frontend               # 前端 Dockerfile（基础镜像默认走 DaoCloud 代理）
├── docker-compose.yml                # Docker Compose 编排
├── nginx.conf                        # Nginx 配置（SPA + API 代理 + SSE）
├── start.sh                          # 开发环境一键启动（前台，Ctrl+C 停止）
└── README.md                         # 本文件
```

---

## 常见问题

### Q: faiss-cpu 安装失败

```bash
pip install faiss-cpu --only-binary :all:
```

或使用 Python 3.11（有完整预编译 wheel 支持）。

### Q: 前端页面空白或请求失败

1. 确认后端启动：访问 `http://服务器IP:8000/docs`
2. 检查前端代理配置（开发环境）或 Nginx 配置（生产环境）
3. 阿里云需检查安全组是否开放了相应端口

### Q: 知识库搜索搜不到预期的文档？

知识库管理页的搜索分两种模式：

- **输入即过滤**：只要输入文字，列表会实时按标题/文件名过滤，支持字符级模糊匹配（"环境保护法"能匹配"环境保护税"等字符重叠度 ≥ 60% 的标题）
- **Enter 触发语义检索**：按回车后才进行向量语义搜索，返回相关文档片段（可展开查看全文/页码/原图）

如果输入关键词但未按 Enter 就看到「未找到」，是正常现象——说明没有标题匹配，可按 Enter 再试内容检索。

### Q: 上传的笔记为什么没有出现百科词条？

知识编译在保存成功后后台异步执行，受 LLM 调用耗时影响，通常需几十秒到几分钟。未出现词条的常见原因：

1. **内容过短**：识别文字少于编译门槛（默认 80 字）的资料不编译，只参与普通检索，属正常跳过；
2. **编译开关被关闭**：在系统设置「llm-wiki 知识编译」区块确认开关状态，开启后可用「存量补编译」一键补齐；
3. **LLM 限流/失败**：免费模型额度有限时编译会静默降级（不影响原始资料与检索），稍后重新补编译即可。

### Q: 上传 Word/PDF 后表格变成乱文本？

系统已内置表格 Markdown 化处理：

- **PDF**：通过 PyMuPDF 自动检测表格区域，将单元格内容转换为 `| 列1 | 列2 |` 格式的 Markdown 表格，同时过滤掉该区域的行内文本避免重复
- **Word**：按文档流顺序提取段落和表格，表格同样输出为 Markdown 格式

在上传页面「预览/编辑」区域可以直接确认解析效果并手动修正后再入库。

### Q: 如何切换 LLM 模型？

编辑 `backend/.env` 后重启容器：

```bash
docker compose restart
```

或在系统管理 → 系统设置中运行时切换。

### Q: 忘记管理员密码？

用户数据存放在 SQLite 数据库（开发环境 `backend/data/app.db`，Docker 部署为 `data/db/app.db`）。
停止服务后删除该数据库文件再重启，重新注册的第一个用户即为管理员（注意：这会同时清除会话/用量等历史数据，向量库不受影响）。

### Q: 解压部署包提示 `tar: Ignoring unknown extended header keyword 'LIBARCHIVE.xattr...'`

无害警告：macOS 打包时把 Finder 扩展属性写进了压缩包，Linux 的 GNU tar 提示忽略，**解压本身正常完成**，不影响部署。新版部署包已用 `COPYFILE_DISABLE=1` 打包，不会再出现该警告。

### Q: docker build 拉取基础镜像报 403 / 超时？

国内访问 Docker Hub 常受限（旧镜像加速器返回 403）。两个 Dockerfile 默认通过 `ARG REGISTRY` 走 DaoCloud 公共代理（`docker.m.daocloud.io/library/`），不经过 docker.io；如该代理失效，可换源重建：

```bash
REGISTRY=docker.io/library/ bash scripts/deploy-aliyun.sh build   # 或其他可用代理前缀
```

### Q: 本地开发服务过一段时间就消失、页面一直转圈？

服务进程挂在临时终端上，终端被回收时进程随之退出，浏览器持有的是已死连接（表现为转圈、"越来越慢"）。改用驻留脚本启动并用 `status` 验证：

```bash
bash scripts/dev-daemon.sh start
bash scripts/dev-daemon.sh status
```

### Q: 用 IP 访问很慢/卡死，localhost 却正常？

服务本身正常（同机 curl 两个地址均毫秒级响应）。常见原因是浏览器的代理扩展（如 Clash）劫持了非 localhost 流量并走代理节点。解决：临时禁用代理扩展重试；或在代理 bypass/直连规则中加入 `localhost, 127.0.0.1, 172.16.0.0/12, 192.168.0.0/16, 10.0.0.0/8, 服务器公网IP`。

### Q: 如何备份整个系统？

```bash
# 备份所有数据
tar czf jianziruyang_backup_$(date +%Y%m%d).tar.gz \
  -C /opt/jianziruyang \
  --exclude='frontend/node_modules' \
  --exclude='backend/.venv' \
  data/ docker-compose.yml backend/.env nginx.conf
```

> `data/` 目录已包含 `db/app.db`（SQLite 业务数据），一次备份即可覆盖全部数据。

---

## 开发说明

### 架构设计原则

分层架构 + 工厂模式：

```
API 层 (路由) → 服务层 (业务逻辑) → 核心层 (抽象接口)
```

每个主要外部依赖（LLM、OCR、Embedding、搜索）都通过工厂模式抽象，添加新提供商只需新增实现类。

### 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 向量数据库 | FAISS | 轻量级，本地运行，无需额外服务 |
| 业务存储 | SQLite (WAL) | 单文件、零运维，支撑多用户业务数据 |
| 流式通信 | SSE | 比 WebSocket 轻量，浏览器原生支持 |
| 前端状态管理 | Pinia | Vue 3 官方推荐，类型安全 |
| 鉴权方案 | Bearer Token | 服务端会话 + 滑动续期，适合前后端分离 |
| 会话隔离 | 按 user_id 过滤 | 多用户数据安全 |
| 检索权限 | 向量库物理隔离 | 检索阶段只包含可见库向量，从源头杜绝越权 |

### 性能优化设计

| 模块 | 优化手段 | 效果 |
|------|---------|------|
| LLM / OCR / 搜索 / Rerank / Embedding | HTTP 客户端连接池复用（按配置缓存实例） | 消除每次请求重复的 TCP+TLS 握手 |
| 问答流水线 | 知识库检索与联网搜索并行；同步检索移入线程池；意图改写/证据判断 15s 超时保护 | 检索期间不阻塞事件循环，双开时省一段完整搜索耗时 |
| 事件循环解阻塞 | 本地 OCR 推理、Rerank 打分、文本分割 + Embedding 向量化、批量上传等所有同步重计算统一 `asyncio.to_thread` | 长耗时 OCR/向量化不再卡住其他并发请求，多用户同时使用不互相拖慢 |
| 向量检索 | metadata.json 按 mtime 失效的内存缓存 | 检索路径零磁盘 IO |
| 前端构建 | 路由级动态 import 自然分块（不配 manualChunks：Vite 8 rolldown 下强拆 vendor 会引发跨 chunk 循环初始化运行时错误）；mammoth 等重依赖按路由懒加载 | 首屏约 78KB gzip，并行下载、独立缓存 |
| Nginx（生产） | gzip + `/assets/` 30 天 immutable 缓存 + index.html no-cache + 上传上限 50m | 首屏显著提速，大图上传不再被 413 拒绝 |

---

> 见字如面 — 将手写笔记转化为智慧，用知识滋养身心。
