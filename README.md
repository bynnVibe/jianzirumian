# 见字如面 — 手写笔记知识库问答系统

> **「见字如面」** —— 将手写笔记转化为可检索的智慧，用知识滋养身心。
>
> 面向个人知识管理的智能问答系统：手写笔记图片经 OCR 转为文字，自动构建向量知识库，结合大语言模型（LLM）实现基于知识库的精准问答；支持联网搜索补充知识库未覆盖的信息。
>
> 🚀 **第一次接触本项目？** 直接跳到 [快速启动（开发环境）](#快速启动开发环境)，5 步 10 分钟跑起来；部署上云见 [Docker 部署](#docker-部署本地)。
>
> 👀 **只想先看一眼界面？** 浏览器直接打开根目录 [`系统样例.html`](./系统样例.html)（无需任何依赖）；系统也内置面向新用户的完整使用指南（登录页「系统使用指导」，地址 `/guide`）。

---

## 目录

- [系统架构](#系统架构)
- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [快速启动（开发环境）](#快速启动开发环境)
- [Docker 部署（本地）](#docker-部署本地)
- [阿里云服务器部署](#阿里云服务器部署本地打包推送)
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
- Agent 可选通过 `LLM_PROVIDER=gateway` 使用 Proxy；默认仍直连已配置提供商。
- 不支持多 Agent/worker 或横向扩容。Proxy 重启不会恢复或重置 Agent 业务任务。
- 所有宿主端口默认回环；Grafana 无默认密码。存活使用即时 `up`，readiness 只检查本地资源，不探测付费模型。

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
| 查看/编辑/润色 | 入库前可查看解析效果，直接编辑或一键 AI 润色，**入库的是最终确认文本**；Word/PDF 支持原始排版分页预览与页码跳转 |
| 目标知识库 | 两级筛选（公共/个人 → 具体库），支持新建个人知识库；入库进度实时显示，失败项可重试 |
| 入库历史 | 按时间倒序回溯本人成功入库记录，可预览原始文件、按页查看解析文档 |

### 检索问答（RAG）

- **混合检索链路**：FAISS 稠密召回 + 项目内轻量 BM25 稀疏召回 + RRF 融合 + Rerank 重排（本地 CrossEncoder 或阿里云 DashScope）；管理员可切换 `dense`/`hybrid` 模式并调整 top_k、RRF 参数
- **问答流水线**：意图改写 → 检索 → 证据充分性判断（轻量 Self-RAG，不足时改写重检一次）→ 与联网搜索并行 → 注入上下文 → LLM 流式生成 → 引用编号核对（越界引用追加提醒），全程步骤进度条实时展示
- **引用与反馈**：回答标注来源编号、可展开查看原始片段/图片/页码；支持点赞点踩（点踩可一键导入回归评测集）
- **联网搜索**：独立开关，DuckDuckGo（免费）/ AnySearch / Bing / SerpAPI 多源可选
- **会话管理**：多会话历史按日期分组、用户隔离；长会话自动 LLM 摘要压缩（摘要 + 最近消息），上下文不无限增长
- **用户记忆**：跨会话抽取用户长期偏好/事实注入后续回答，敏感信息（密码/token/证件号等）正则拦截不落库
- **聊天附件确认入库**：默认只识别或总结；明确保存意图只生成确认卡，点击确认后才写入可写知识库。默认私人库，24小时有效，刷新保留状态；游客不可写，失败/中断不自动重试。
- **停止生成**：取消、超时或异常 EOF 保留部分文本并标为中断；不撤销已确认的入库。聊天默认总预算300秒，Proxy240秒，重试不重置。

### llm-wiki 知识编译层

借鉴 llm-wiki「LLM 作为知识编译器」思想，增量共存于问答库 RAG 之上：入库成功后后台自动将资料编译为结构化百科词条（概述/关键要点/相关概念/原文精选），写入 `wiki_pages` 表与向量库；检索时百科卡片稳定前置命中，原始碎片兜底。支持跨素材综合词条（digest）、存量一键补编译、在线开关（编译/检索优先）、Markdown 落盘镜像（`data/wiki/`）、删除来源联动清理；编译失败静默降级，不阻断上传与问答。

### 知识收藏

收藏 AI 回答或知识片段，支持自定义标签、标签筛选、全文搜索、分页浏览、用户隔离；Word/PDF 来源保留文档类型与页码，收藏页可「在线预览（第 N 页）」跳回原文；卡片详情 Markdown 全文渲染。

### 管理员 / 运维能力

| 模块 | 说明 |
|------|------|
| 系统管理控制台（`/admin`） | 独立布局：用户管理、系统设置（运行时切换 LLM/OCR/搜索/Embedding/Rerank 提供商、注册开关、游客次数、检索参数）、OpenRouter 免费模型一键刷新与失败自动切换 |
| 回归评测（`/admin/eval`） | 仿 Ragas 四指标（忠实度/回答相关性/上下文精确率/上下文召回率）+ 检索命中率，对问答链路离线回归；评测集支持手工/JSON 批量/点踩反馈导入用例，跨 run 对比防优化回退 |
| 模型网关（`/admin/gateway`） | 见下节 |
| Agent 可观测性（`/admin/observability`) | 结构化日志 / 指标 / 调用级 Trace 三层可观测，管理员在线查看 |
| CI/CD 流水线（`/admin/pipeline`） | 评估驱动部署（EDD），仅运维角色 |

### 模型网关（LLM Local Gateway，层3 LLM Proxy）

对下游工具/应用提供 OpenAI 兼容模型入口，凭本地密钥鉴权，托管真实上游 Key：

| 能力 | 说明 |
|------|------|
| 密钥与配额 | 请求次数原子认领；Token 为事后计量软限额，不是硬费用上限。模型授权、过期校验；密钥仅存哈希，明文只展示一次 |
| 负载均衡与故障切换 | 渠道优先级分层 + 同层权重加权随机（A-Res）；失败自动切换，连续失败 ≥3 次熔断、成功一次恢复；openai/anthropic/ollama 三协议归一化为 OpenAI 形态（含流式 SSE） |
| 日志与审计 | 每次调用全量入库（状态/Token/路由/参数/风险/七阶段可视化调用链路），可搜索筛选分页展开明细 |
| 安全审计中心 | 约 25 条内置规则、6 大类别（凭证泄露/敏感路径/危险命令/外联追踪/Unicode 隐写/公网 IP），审计/警告/脱敏/阻断四模式，规则可启停、支持黑白名单与扫描试跑 |

鉴权/模型授权 → 安全审计 → 候选渠道 → 原子请求认领 → 上游。审计拒绝、无渠道不扣请求，进入上游后失败不退，重试不重复扣；首块输出后不切换或重放。管理页「Proxy 连通测试」由独立 Proxy 执行，可能产生费用；未配置内部诊断凭据时明确拒绝。

### 缓存与稳定性

- **Redis 文件缓存**：知识库原始文件（图片/Word/PDF/预览 PDF）经 Redis 加速来源跳转与预览；公共库全局共享、个人库按属主隔离；Redis 不可用自动降级直读磁盘；响应头 `X-File-Cache` 标注命中状态
- **持久化**：认证会话（`auth_sessions`）、游客计数（`guest_usage`）、工具调用、用户记忆等均落库 SQLite，服务重启不丢状态；聊天会话懒加载 + 短期缓存，避免内存压力

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
| **LLM** | Ollama 本地 / OpenAI 兼容 API / OpenRouter 免费模型 / 模型网关 | 问答生成（gateway 时经层3 LLM Proxy 出网） |
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

```bash
LLM_PROVIDER=openai
API_BASE_URL=https://api.deepseek.com/v1
API_KEY=sk-你的密钥
API_MODEL=deepseek-chat

# Embedding 也需指向可用服务（二选一）：
#   本地：EMBEDDING_PROVIDER=ollama 并执行 `ollama pull bge-m3`
#   云端：EMBEDDING_PROVIDER=openai + EMBEDDING_API_BASE / EMBEDDING_API_KEY / EMBEDDING_API_MODEL
```

> OCR 默认 `OCR_PROVIDER=local`（PaddleOCR，首次识别时自动下载模型）；也可切 `aliyun`（需 AccessKey）或 `custom_api`。
> 以上配置启动后均可在「系统管理 → 系统设置」页在线切换，无需改文件重启。

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

> 使用 `LLM_PROVIDER=gateway` 或测试独立渠道诊断时另行启动 Proxy：`cd backend && .venv/bin/uvicorn app.llm_proxy:app --port 8001 --no-proxy-headers`。不要启动第二个 Agent。

### 第 5 步 · 首次访问与验证

打开 <http://localhost:5173>，点击「立即注册」——**第一个注册账号自动成为管理员**。

- [ ] `curl http://localhost:8000/api/ready` 返回 ready（不代表外部模型已可用）
- [ ] 上传一张手写笔记照片 → OCR 识别 → 切「知识入库」确认入库 → 对话页提问得到带来源引用的回答
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

## 阿里云服务器部署（本地打包推送）

开发机（Mac M 芯片/Intel）**本地构建 → 导出镜像 → 上传 → 服务器加载运行**，服务器只跑容器不做构建，适合低配 ECS。全流程由 `scripts/deploy-aliyun.sh` 自动化。

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

## 项目结构

```text
backend/app/api/         路由与权限
backend/app/services/    聊天、确认动作、知识库、网关等业务
backend/app/core/        数据库、运行所有权、模型、检索与观测
backend/tests/           正式隔离回归
frontend/src/            页面、确认卡、Pinia和SSE客户端
monitoring/             抓取、告警与Grafana装配
scripts/                部署保护、HTTPS示例、开发脚本
docker-images/          与主配置同步的部署快照
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

知识编译在入库成功后后台异步执行（几十秒到几分钟）。常见原因：① 内容过短（默认少于 80 字不编译，属正常跳过）；② 编译开关被关闭（系统设置「llm-wiki 知识编译」区块确认，可「存量补编译」一键补齐）；③ LLM 限流/失败静默降级（不影响原始资料与检索，稍后补编译）。

### Q: 上传 Word/PDF 后表格变成乱文本？

系统已内置表格 Markdown 化：PDF 经 PyMuPDF 检测表格区域转 `| 列1 | 列2 |` 格式并过滤重复行内文本；Word 按文档流提取段落与表格同样输出 Markdown。上传页「预览/编辑」区可确认解析效果并手动修正后再入库。

### Q: 如何切换 LLM 模型？

「系统管理 → 系统设置」运行时切换；修改环境文件后重新应用Compose，运行时覆盖项优先。

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
