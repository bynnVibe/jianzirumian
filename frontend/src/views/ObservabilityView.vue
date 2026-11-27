<template>
  <div class="obs-view">
    <!-- 顶部标题栏 -->
    <header class="obs-header">
      <div class="header-inner">
        <div class="header-title">
          <span class="header-eyebrow">{{ canOps ? 'Observability' : '我的使用概览' }}</span>
          <h1>{{ canOps ? 'Agent 可观测性' : '可观测性' }}</h1>
          <p v-if="canOps" class="header-sub">
            在线监控 Agent 行为：日志记录每一步、指标聚合 Token/延迟/成功率、追踪回放每次请求的完整 prompt 与 response
          </p>
          <p v-else class="header-sub">了解自己的使用情况与服务完成情况，仅展示当前账号的业务统计。</p>
        </div>
        <div class="header-actions">
          <div class="days-switch">
            <button
              class="day-btn"
              :class="{ active: rangeMode === 'today' }"
              :disabled="loading"
              @click="changeRange('today')"
            >今日</button>
            <button
              v-for="d in [7, 30]"
              :key="d"
              class="day-btn"
              :class="{ active: rangeMode === 'days' && days === d }"
              :disabled="loading"
              @click="changeRange('days', d)"
            >近{{ d }}天</button>
          </div>
          <button class="btn btn-ghost" :disabled="loading" @click="loadAll(1)">
            {{ loading ? '加载中…' : '刷新' }}
          </button>
        </div>
      </div>
    </header>

    <div class="obs-body">
      <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>

      <template v-if="!canOps">
        <p v-if="loading && !businessMetrics" class="empty-hint" role="status">正在加载个人使用概览…</p>
        <template v-if="businessMetrics">
          <section class="cards business-cards">
            <div class="metric-card"><span class="metric-num">{{ businessMetrics.overview.total }}</span><span class="metric-label">使用次数</span></div>
            <div class="metric-card"><span class="metric-num">{{ businessMetrics.overview.succeeded }}</span><span class="metric-label">成功完成</span></div>
            <div class="metric-card"><span class="metric-num">{{ completionPercent(businessMetrics.overview.completion_rate) }}</span><span class="metric-label">服务完成率</span></div>
            <div class="metric-card"><span class="metric-num">{{ responseSeconds(businessMetrics.overview.avg_response_seconds) }}</span><span class="metric-label">平均等待时间</span></div>
          </section>
          <section class="panel">
            <div class="panel-head"><span class="panel-title">处理情况</span></div>
            <p class="business-summary">进行中 {{ businessMetrics.overview.processing }} 次 · 已取消 {{ businessMetrics.overview.cancelled }} 次 · 未完成 {{ businessMetrics.overview.failed }} 次</p>
            <p v-if="businessMetrics.overview.failed" class="empty-hint">遇到未完成的操作，可重新尝试；若多次失败，请联系管理员。</p>
            <p v-if="!businessMetrics.overview.total" class="empty-hint">这段时间还没有使用记录。开始一次问答或使用知识助手后，点击刷新即可查看。</p>
          </section>
          <section class="panel">
            <div class="panel-head"><span class="panel-title">每日使用趋势（{{ rangeLabel }}）</span></div>
            <div v-if="businessMetrics.overview.total" class="trend-chart business-trend">
              <div v-for="row in businessMetrics.daily" :key="row.day" class="trend-col"
                @mouseenter="showTip($event, [row.day, `使用 ${row.total} 次`, `成功 ${row.succeeded} 次`, `未完成 ${row.failed} 次`])"
                @mousemove="moveTip" @mouseleave="hideTip">
                <div class="trend-bar-wrap"><div class="trend-bar" :style="{ height: businessBarHeight(row.total) + '%' }"></div></div>
                <span class="trend-label">{{ row.day.slice(5) }}</span>
              </div>
            </div>
            <p v-else class="empty-hint">暂无使用趋势</p>
          </section>
          <section class="panel business-breakdown">
            <div class="panel-head"><span class="panel-title">业务功能使用情况</span></div>
            <table v-if="businessMetrics.by_activity.length" class="obs-table">
              <thead><tr><th>功能</th><th>使用次数</th><th>成功完成</th><th>服务完成率</th><th>平均等待</th></tr></thead>
              <tbody><tr v-for="row in businessMetrics.by_activity" :key="row.activity">
                <td>{{ row.activity }}</td><td>{{ row.total }}</td><td>{{ row.succeeded }}</td>
                <td>{{ completionPercent(row.completion_rate) }}</td><td>{{ responseSeconds(row.avg_response_seconds) }}</td>
              </tr></tbody>
            </table>
            <p v-else class="empty-hint">暂无功能使用记录</p>
          </section>
          <p class="business-note">仅统计当前账号最近 {{ businessMetrics.retention_days }} 天内保留的服务记录，不代表全站用量。一次操作可能触发问答、笔记整理等不同服务，按各项服务分别计数。完成率与平均等待时间不计入进行中和主动取消的操作；完成率不代表答案准确率。</p>
        </template>
      </template>
      <template v-else>
      <nav class="ops-tabs" aria-label="可观测性模块">
        <button class="ops-tab" :class="{ active: opsTab === 'dashboard' }" @click="opsTab = 'dashboard'">DashBoard</button>
        <button class="ops-tab" :class="{ active: opsTab === 'logs' }" @click="opsTab = 'logs'">日志记录</button>
        <button class="ops-tab" :class="{ active: opsTab === 'metrics' }" @click="opsTab = 'metrics'">指标监控</button>
        <button class="ops-tab" :class="{ active: opsTab === 'traces' }" @click="opsTab = 'traces'">链路追踪</button>
      </nav>
      <ObsDashboard
        v-if="opsTab === 'dashboard'"
        :metrics="metrics"
        :monitor="monitor"
        :loading="loading"
        :range-label="rangeLabel"
        @drill="openDrill"
      />
      <ObsLogs v-else-if="opsTab === 'logs'" @open-trace="openTrace" />
      <template v-else-if="opsTab === 'metrics'">
      <!-- 实时告警分析：质量/性能越限自动归因 + 原因分析 + 优化建议（全局铃铛深链 focus=alerts 定位到此） -->
      <ObsAlerts />
      <div class="aspect-title">性能指标（请求 / 延迟 / Token / 黄金信号）</div>
      <!-- ============ 指标总览 ============ -->
      <section class="cards" v-if="metrics">
        <div class="metric-card drillable" title="请求总数，点击查看明细" @click="openDrill('traces', '请求总数')">
          <span class="metric-num">{{ metrics.overview.total }}</span>
          <span class="metric-label">请求总数</span>
        </div>
        <div class="metric-card drillable" title="成功率，点击查看请求明细" @click="openDrill('traces', '成功率')">
          <span class="metric-num" :class="{ warn: metrics.overview.success_rate < 0.95 && metrics.overview.total }">
            {{ (metrics.overview.success_rate * 100).toFixed(1) }}%
          </span>
          <span class="metric-label">成功率</span>
        </div>
        <div class="metric-card drillable" title="失败请求，点击查看明细" @click="openDrill('traces', '失败请求')">
          <span class="metric-num">{{ metrics.overview.errors }}</span>
          <span class="metric-label">失败请求</span>
        </div>
        <div class="metric-card drillable" title="平均延迟，点击查看请求明细" @click="openDrill('traces', '平均延迟')">
          <span class="metric-num">{{ formatMs(metrics.overview.avg_latency_ms) }}</span>
          <span class="metric-label">平均延迟</span>
        </div>
        <div class="metric-card drillable" title="P95 延迟，点击查看请求明细" @click="openDrill('traces', 'P95 延迟')">
          <span class="metric-num">{{ formatMs(metrics.overview.p95_latency_ms) }}</span>
          <span class="metric-label">P95 延迟</span>
        </div>
        <div class="metric-card drillable" title="Token 消耗，点击查看每次 LLM 调用 Token 明细" @click="openDrill('llm_spans', 'Token 消耗')">
          <span class="metric-num">{{ formatNum(metrics.overview.prompt_tokens + metrics.overview.completion_tokens) }}</span>
          <span class="metric-label">Token 消耗</span>
        </div>
        <div class="metric-card drillable" title="平均 Token/请求，点击查看明细" @click="openDrill('traces', '平均 Token/请求')">
          <span class="metric-num" :class="{ warn: metrics.overview.token_overuse }">
            {{ formatNum(metrics.overview.avg_tokens_per_request) }}
          </span>
          <span class="metric-label">平均 Token/请求</span>
        </div>
        <div class="metric-card drillable" title="LLM 调用，点击查看每次 LLM 调用明细" @click="openDrill('llm_spans', 'LLM 调用')">
          <span class="metric-num">{{ metrics.overview.llm_calls }}</span>
          <span class="metric-label">LLM 调用</span>
        </div>
        <div class="metric-card drillable" title="工具调用，点击查看每次工具调用明细" @click="openDrill('tool_spans', '工具调用')">
          <span class="metric-num">{{ metrics.overview.tool_calls }}</span>
          <span class="metric-label">工具调用</span>
        </div>
      </section>

      <!-- ============ 黄金信号（需求4：对齐主流可观测性 Rate/Errors/Duration/Saturation） ============ -->
      <section class="panel" v-if="metrics && metrics.golden_signals">
        <div class="panel-head">
          <span class="panel-title">黄金信号（{{ rangeLabel }} · Google SRE 四大信号 + RED 口径）</span>
        </div>
        <div class="cards golden-cards">
          <div class="metric-card">
            <span class="metric-num">{{ metrics.golden_signals.rate_per_day }}</span>
            <span class="metric-label">流量 Rate（请求/天）</span>
          </div>
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: metrics.golden_signals.error_rate > 0.05 }">
              {{ (metrics.golden_signals.error_rate * 100).toFixed(1) }}%
            </span>
            <span class="metric-label">错误 Errors（失败/超时/中断）</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ formatMs(metrics.golden_signals.duration.avg_ms) }}</span>
            <span class="metric-label">延迟 Duration（平均）</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ formatMs(metrics.golden_signals.duration.p95_ms) }}</span>
            <span class="metric-label">延迟 P95</span>
          </div>
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: metrics.golden_signals.saturation.overuse }">
              {{ formatNum(metrics.golden_signals.saturation.avg_tokens_per_request) }}
            </span>
            <span class="metric-label">饱和度 Saturation（Token/请求 · 水位 {{ (metrics.golden_signals.saturation.utilization * 100).toFixed(0) }}%）</span>
          </div>
        </div>
      </section>

      <div class="aspect-title">质量指标（反馈 / 回归 / Skill 预审 / 沉淀建议）</div>
      <section class="cards" v-if="monitor">
        <div class="metric-card drillable" title="点赞，点击查看反馈明细" @click="openDrill('feedbacks', '点赞')">
          <span class="metric-num">{{ monitor.quality.likes }}</span>
          <span class="metric-label">点赞</span>
        </div>
        <div class="metric-card drillable" title="点踩，点击查看反馈明细" @click="openDrill('feedbacks', '点踩')">
          <span class="metric-num" :class="{ warn: monitor.quality.dislikes }">{{ monitor.quality.dislikes }}</span>
          <span class="metric-label">点踩</span>
        </div>
        <div class="metric-card drillable" title="最近回归，点击查看评测运行明细" @click="openDrill('eval_runs', '回归评测运行')">
          <span class="metric-num">{{ latestEvalText }}</span>
          <span class="metric-label">最近回归·答案相关度</span>
        </div>
        <div class="metric-card drillable" title="Skill 预审高危，点击查看预审明细" @click="openDrill('skill_reviews', 'Skill 预审（高危）')">
          <span class="metric-num" :class="{ warn: monitor.quality.skill_reviews.high }">{{ monitor.quality.skill_reviews.high }}</span>
          <span class="metric-label">Skill 预审高危</span>
        </div>
        <div class="metric-card drillable" title="在线采样评测（DeepEval 风格），点击前往评测概览·在线评测" @click="router.push({ name: 'EvalOverview', query: { tab: 'online' } })">
          <span class="metric-num" :class="{ warn: monitor.quality.online_eval?.low_count }">{{ onlineEvalText }}</span>
          <span class="metric-label">在线评测均分（{{ monitor.quality.online_eval?.samples || 0 }} 样本 · 低分 {{ monitor.quality.online_eval?.low_count || 0 }}）</span>
        </div>
      </section>
      <!-- ============ 能力缺口沉淀建议（需求6：系统可进化性） ============ -->
      <section class="panel" v-if="sediment && sediment.suggestions && sediment.suggestions.length">
        <div class="panel-head">
          <span class="panel-title">未满足能力趋势（脱敏汇总）</span>
          <button class="sediment-btn" @click="router.push('/skills')">查看我的能力建议</button>
        </div>
        <div v-for="s in sediment.suggestions" :key="`${s.scenario}:${s.missing_capability}`" class="sediment-item">
          <div class="sediment-body">
            <p class="sediment-msg">💡 {{ s.message }}</p>
            <p class="sediment-meta">
              {{ s.label }} · {{ s.owners }} 位用户 · {{ s.count }} 条独立请求
              · 最近出现：{{ formatTime(s.last_at) }}
            </p>
          </div>
        </div>
      </section>

      <!-- ============ Skill 安全预审（重点暴露拒绝行为） ============ -->
      <section class="panel" v-if="skillReviews">
        <div class="panel-head">
          <span class="panel-title">Skill 安全预审（添加/修改技能时的安全与能力可行性评审）</span>
        </div>
        <div class="cards review-cards">
          <div class="metric-card">
            <span class="metric-num">{{ skillReviews.summary.total }}</span>
            <span class="metric-label">评审次数</span>
          </div>
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: skillReviews.summary.rejected }">{{ skillReviews.summary.rejected }}</span>
            <span class="metric-label">拒绝次数</span>
          </div>
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: skillReviews.summary.high_risk }">{{ skillReviews.summary.high_risk }}</span>
            <span class="metric-label">高危安全风险</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ skillReviews.summary.unsupported }}</span>
            <span class="metric-label">能力不支持</span>
          </div>
        </div>
        <table class="obs-table" v-if="skillReviews.items.length">
          <thead>
            <tr><th>时间</th><th>用户</th><th>操作</th><th>技能</th><th>结果</th><th>风险</th><th>原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in skillReviews.items" :key="r.id" :class="{ 'row-reject': r.verdict === 'reject' }">
              <td class="cell-time">{{ formatTime(r.created_at) }}</td>
              <td :title="r.user_id">{{ r.username || r.user_id || '-' }}</td>
              <td>{{ reviewActionLabel(r.action) }}</td>
              <td :title="r.kind + ' / ' + r.scope">{{ r.skill_name || '-' }}</td>
              <td>
                <span class="status-dot" :class="r.verdict === 'reject' ? 'error' : 'ok'"></span>
                {{ r.verdict === 'reject' ? '已拒绝' : '通过' }}
              </td>
              <td>{{ reviewRiskLabel(r.risk) }}</td>
              <td class="cell-input" :title="(r.reasons || []).join('；')">{{ (r.reasons || []).join('；') || '-' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-hint">暂无技能预审记录（用户在「Skill 配置」新建/编辑技能时会自动评审并留痕）</p>
      </section>

      <div class="aspect-title">安全指标（网关安全事件 / 工具约束拦截）</div>
      <section class="cards" v-if="monitor">
        <div class="metric-card drillable" title="网关安全事件，点击查看明细" @click="openDrill('sec_events', '网关安全事件')">
          <span class="metric-num" :class="{ warn: monitor.security.events_total }">{{ monitor.security.events_total }}</span>
          <span class="metric-label">网关安全事件</span>
        </div>
        <div class="metric-card drillable" title="待处置，点击查看安全事件明细" @click="openDrill('sec_events', '安全事件待处置')">
          <span class="metric-num" :class="{ warn: monitor.security.pending }">{{ monitor.security.pending }}</span>
          <span class="metric-label">待处置</span>
        </div>
        <div class="metric-card drillable" title="未确认安全告警（全局弹窗同源），点击前往网关安全审计中心处置" @click="router.push({ name: 'Gateway', query: { tab: 'security' } })">
          <span class="metric-num" :class="{ warn: monitor.security.unacked_alerts }">{{ monitor.security.unacked_alerts || 0 }}</span>
          <span class="metric-label">待处置告警（去网关处理 →）</span>
        </div>
        <div class="metric-card drillable" title="高危/极高事件，点击查看明细" @click="openDrill('sec_events', '高危/极高事件')">
          <span class="metric-num" :class="{ warn: monitor.security.high + monitor.security.critical }">{{ monitor.security.high + monitor.security.critical }}</span>
          <span class="metric-label">高危/极高事件</span>
        </div>
        <div class="metric-card drillable" title="工具约束拦截，点击查看拦截明细" @click="openDrill('tool_blocked', '工具约束拦截')">
          <span class="metric-num" :class="{ warn: monitor.security.tool_blocked }">{{ monitor.security.tool_blocked }}</span>
          <span class="metric-label">工具约束拦截</span>
        </div>
      </section>
      <div class="aspect-title">业务指标（资产存量 / 活跃用户 / 语义缓存 / 记忆）</div>
      <section class="cards" v-if="monitor">
        <div class="metric-card drillable" title="会话总数，点击查看明细" @click="openDrill('sessions', '会话总数')"><span class="metric-num">{{ monitor.business.counts.sessions || 0 }}</span><span class="metric-label">会话总数</span></div>
        <div class="metric-card drillable" title="消息总数，点击查看明细" @click="openDrill('messages', '消息总数')"><span class="metric-num">{{ monitor.business.counts.messages || 0 }}</span><span class="metric-label">消息总数</span></div>
        <div class="metric-card drillable" title="活跃用户，点击查看明细" @click="openDrill('active_users', '活跃用户')"><span class="metric-num">{{ monitor.business.counts.active_users || 0 }}</span><span class="metric-label">活跃用户</span></div>
        <div class="metric-card drillable" title="上传记录，点击查看明细" @click="openDrill('uploads', '上传记录')"><span class="metric-num">{{ monitor.business.counts.uploads || 0 }}</span><span class="metric-label">上传记录</span></div>
        <div class="metric-card drillable" title="知识库，点击查看明细" @click="openDrill('kbs', '知识库')"><span class="metric-num">{{ monitor.business.counts.knowledge_bases || 0 }}</span><span class="metric-label">知识库</span></div>
        <div class="metric-card drillable" title="百科页面，点击查看明细" @click="openDrill('wiki', '百科页面')"><span class="metric-num">{{ monitor.business.counts.wiki_pages || 0 }}</span><span class="metric-label">百科页面</span></div>
        <div class="metric-card drillable" title="记忆条数，点击查看明细" @click="openDrill('memories', '记忆条数')"><span class="metric-num">{{ monitor.business.counts.memories || 0 }}</span><span class="metric-label">记忆条数</span></div>
      </section>

      <!-- ============ 记忆与上下文治理（静态/动态 Markdown 写路径 × 四层压缩链） ============ -->
      <div class="aspect-title">记忆与上下文治理（静态/动态 Markdown 写路径 × 四层压缩链）</div>
      <section class="cards" v-if="monitor && monitor.memory_context">
        <div class="metric-card drillable" title="记忆写路径事件，点击查看明细" @click="openDrill('memory_events', '记忆写路径事件')">
          <span class="metric-num">{{ monitor.memory_context.write_events_total }}</span>
          <span class="metric-label">记忆写路径事件</span>
        </div>
        <div class="metric-card drillable" title="记忆抽取运行（每轮对话必落一条），点击查看运行明细与无产出原因" @click="openDrill('memory_extract', '记忆抽取运行')">
          <span class="metric-num" :class="{ warn: extractZeroYield }">{{ monitor.memory_context.extract_runs?.total ?? 0 }}</span>
          <span class="metric-label">记忆抽取运行</span>
        </div>
        <div class="metric-card drillable" title="压缩链事件，点击查看明细" @click="openDrill('compact_events', '上下文压缩链事件')">
          <span class="metric-num">{{ monitor.memory_context.compact_events.total }}</span>
          <span class="metric-label">压缩链事件</span>
        </div>
        <div class="metric-card">
          <span class="metric-num">{{ formatNum(monitor.memory_context.tokens_saved) }}</span>
          <span class="metric-label">压缩节省 Token</span>
        </div>
        <div class="metric-card">
          <span class="metric-num" :class="{ warn: monitor.memory_context.breaker_sessions }">{{ monitor.memory_context.breaker_sessions }}</span>
          <span class="metric-label">熔断会话（连败≥3）</span>
        </div>
      </section>
      <section class="panel" v-if="monitor && monitor.memory_context">
        <div class="panel-head"><span class="panel-title">治理分布（{{ rangeLabel }}）</span></div>
        <div class="chain-chips">
          <span class="chain-chip">L0 落盘 {{ monitor.memory_context.compact_events.offload || 0 }} / 去重 {{ monitor.memory_context.compact_events.dedup || 0 }}</span>
          <span class="chain-chip">L1 微压缩 {{ monitor.memory_context.compact_events.micro || 0 }}（放弃 {{ monitor.memory_context.compact_events.micro_skip || 0 }}）</span>
          <span class="chain-chip">L2 Session Memory {{ monitor.memory_context.compact_events.session_memory || 0 }}</span>
          <span class="chain-chip">L2 Auto {{ monitor.memory_context.compact_events.auto_compact || 0 }}（命中 {{ monitor.memory_context.compact_events.auto_compact_hit || 0 }}）</span>
          <span class="chain-chip">L3 兜底 {{ monitor.memory_context.compact_events.full || 0 }}</span>
          <span class="chain-chip">手动压缩 {{ monitor.memory_context.compact_events.manual }}</span>
          <span class="chain-chip" :class="{ warn: monitor.memory_context.compact_events.fail }">失败 {{ monitor.memory_context.compact_events.fail }}</span>
          <span class="chain-chip" :class="{ warn: monitor.memory_context.compact_events.blocked }">熔断拦截 {{ monitor.memory_context.compact_events.blocked }}</span>
        </div>
        <div class="chain-chips">
          <span v-for="w in monitor.memory_context.write_by_layer" :key="w.layer" class="chain-chip">{{ w.label }} {{ w.count }}</span>
          <span v-for="(n, a) in monitor.memory_context.write_by_action" :key="a" class="chain-chip">{{ memActionLabel(a) }} {{ n }}</span>
        </div>
        <div class="chain-chips" v-if="monitor.memory_context.extract_runs?.total">
          <span class="chain-chip" :class="{ warn: extractZeroYield }">抽取运行 {{ monitor.memory_context.extract_runs.total }}</span>
          <span class="chain-chip" v-if="monitor.memory_context.extract_runs.last_at">最近 {{ String(monitor.memory_context.extract_runs.last_at).slice(5, 16).replace('T', ' ') }}</span>
          <span class="chain-chip" v-if="monitor.memory_context.extract_runs.last_detail" :title="monitor.memory_context.extract_runs.last_detail">{{ monitor.memory_context.extract_runs.last_detail }}</span>
        </div>
        <p class="ctx-hint">
          写路径 = 静态/动态 Markdown 双写埋点（新增/更新/删除/簇合并/超限遗忘/AutoDream 整理，含助手分域镜像）；
          抽取运行 = extractMemories 每轮对话后台扫描整轮 transcript 的事件（与写入拆口径：运行数 &gt; 0 而写入数 = 0 即「跑了但零产出」，明细含模型层无产出原因）；
          压缩链 = L0 日常约束（offload 超大工具结果落盘 + dedup 去重引用）/ L1 micro 微压缩（≥20K 进入、可释放 &lt;10K 放弃）/
          L2 session_memory 增量记录与 auto_compact ≥95K 摘要替换（summary.md 命中零调用）/ L3 full 兜底（连败 3 次或 ≥119,040）/ manual 手动 / 失败与熔断拦截。
          节省口径：替换历史层 {{ formatNum(monitor.memory_context.tokens_saved) }} tok · 微压缩释放 {{ formatNum(monitor.memory_context.micro_tokens_saved) }} tok · 日常约束节省 {{ formatNum(monitor.memory_context.daily_tokens_saved) }} tok。点击卡片查看逐条明细与口径说明。
        </p>
      </section>

      <!-- ============ 语义缓存监控（命中率 / 节省 / 空间 / 语义详情） ============ -->
      <ObsCachePanel :days="days" />

      <!-- ============ 每日趋势 ============ -->
      <section class="panel" v-if="metrics">
        <div class="panel-head"><span class="panel-title">每日请求趋势（{{ rangeLabel }}）</span></div>
        <div class="trend-chart" v-if="metrics.daily.length">
          <div
            v-for="row in metrics.daily"
            :key="row.day"
            class="trend-col"
            @mouseenter="showTip($event, dayTipLines(row))"
            @mousemove="moveTip"
            @mouseleave="hideTip"
          >
            <div class="trend-bar-wrap">
              <div class="trend-bar" :style="{ height: barHeight(row.total) + '%' }">
                <div
                  v-if="row.errors"
                  class="trend-bar-err"
                  :style="{ height: Math.min(100, (row.errors / Math.max(1, row.total)) * 100) + '%' }"
                ></div>
              </div>
            </div>
            <span class="trend-label">{{ row.day.slice(5) }}</span>
          </div>
        </div>
        <p v-else class="empty-hint">暂无数据</p>
      </section>

      <!-- ============ Token 用量分析已迁移至模型网关（与按密钥用量统计合并） ============ -->
      <p class="token-moved">
        Token 用量分析已移至
        <router-link class="token-moved-link" :to="{ name: 'Gateway', query: { tab: 'tokens' } }">模型网关 → Token 分析</router-link>
        （网关密钥用量 / 配额水位 × Agent 上下文段构成 合并呈现）
      </p>

      <!-- ============ 按 Agent / 按工具 ============ -->
      <section class="two-col" v-if="metrics">
        <div class="panel">
          <div class="panel-head"><span class="panel-title">按 Agent 统计</span></div>
          <table class="obs-table" v-if="metrics.by_agent.length">
            <thead>
              <tr><th>Agent</th><th>请求</th><th>失败</th><th>平均延迟</th><th>LLM</th><th>工具</th><th>Token</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in metrics.by_agent" :key="row.agent">
                <td>{{ row.agent }}</td>
                <td>{{ row.total }}</td>
                <td :class="{ 'cell-err': row.errors }">{{ row.errors }}</td>
                <td>{{ formatMs(row.avg_latency_ms) }}</td>
                <td>{{ row.llm_calls }}</td>
                <td>{{ row.tool_calls }}</td>
                <td>{{ formatNum(row.tokens) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-hint">暂无数据</p>
        </div>
        <div class="panel">
          <div class="panel-head"><span class="panel-title">工具调用次数</span></div>
          <table class="obs-table" v-if="metrics.by_tool.length">
            <thead>
              <tr><th>工具</th><th>调用</th><th>失败</th><th>平均延迟</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in metrics.by_tool" :key="row.tool">
                <td>{{ row.tool }}</td>
                <td>{{ row.total }}</td>
                <td :class="{ 'cell-err': row.errors }">{{ row.errors }}</td>
                <td>{{ formatMs(row.avg_latency_ms) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-hint">暂无工具调用</p>
        </div>
      </section>

      </template>
      <template v-else-if="opsTab === 'traces'">
      <nav class="trace-subtabs" role="tablist" aria-label="链路类型">
        <button class="trace-subtab" :class="{ active: traceTab === 'chat' }" @click="traceTab = 'chat'">对话型链路</button>
        <button class="trace-subtab" :class="{ active: traceTab === 'tools' }" @click="traceTab = 'tools'">工具调用链路</button>
      </nav>
      <template v-if="traceTab === 'chat'">
      <!-- ============ Trace 列表（全链路追踪） ============ -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">请求追踪（Traces）</span>
          <div class="trace-filters">
            <select v-model="filterAgent" class="filter-select" @change="loadTraces">
              <option value="">全部 Agent</option>
              <option value="chat">chat 对话</option>
              <option value="wiki_assistant">知识助手</option>
              <option value="wiki_compile">知识编译</option>
              <option value="wiki_synthesize">综合报告</option>
            </select>
            <select v-model="filterStatus" class="filter-select" @change="loadTraces">
              <option value="">全部状态</option>
              <option value="ok">成功</option>
              <option value="error">失败</option>
              <option value="cancelled">已取消</option>
              <option value="timeout">超时</option>
              <option value="interrupted">重启中断</option>
              <option value="running">进行中</option>
            </select>
          </div>
        </div>
        <div v-if="loadingTraces" class="mini-loading">加载中…</div>
        <table class="obs-table trace-table" v-else-if="traces.length">
          <thead>
            <tr><th>时间</th><th>Agent</th><th>用户</th><th>输入</th><th>状态</th><th>延迟</th><th>LLM/工具</th><th>Token</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in traces" :key="t.id" class="trace-row" @click="openTrace(t.id)">
              <td class="cell-time" :title="`开始：${t.started_at}`">{{ formatTime(t.started_at) }}</td>
              <td><span class="agent-badge" :class="'agent-' + t.agent">{{ t.agent }}</span></td>
              <td :title="t.user_id">{{ t.username || t.user_id || '-' }}</td>
              <td class="cell-input" :title="t.input">{{ t.input || '-' }}</td>
              <td :title="`状态：${statusText(t.status)}${t.error ? ' · ' + t.error : ''}`"><span class="status-dot" :class="t.status"></span>{{ statusText(t.status) }}</td>
              <td :title="`端到端耗时 ${t.latency_ms}ms`">{{ formatMs(t.latency_ms) }}</td>
              <td :title="`LLM 调用 ${t.llm_calls} 次 · 工具调用 ${t.tool_calls} 次`">{{ t.llm_calls }} / {{ t.tool_calls }}</td>
              <td :title="`prompt ${t.prompt_tokens} + completion ${t.completion_tokens}`">{{ formatNum(t.prompt_tokens + t.completion_tokens) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-hint">暂无 trace 记录（发起一次对话或知识助手提问后即可看到）</p>
        <div class="pager" v-if="total > traces.length || offset > 0">
          <button class="btn btn-ghost sm" :disabled="offset === 0" @click="page(-1)">上一页</button>
          <span class="pager-info">{{ offset + 1 }}-{{ offset + traces.length }} / {{ total }}</span>
          <button class="btn btn-ghost sm" :disabled="offset + traces.length >= total" @click="page(1)">下一页</button>
        </div>
      </section>
      </template>

      <template v-else>
      <!-- ============ Tool Routing 调用链分析（req2） ============ -->
      <section class="panel" v-if="toolRouting">
        <div class="panel-head">
          <span class="panel-title">Tool Routing 调用链分析（工具路由 · 约束钩子 · 可观测性）</span>
        </div>
        <div class="cards review-cards">
          <div class="metric-card">
            <span class="metric-num">{{ toolRouting.overview.total_calls }}</span>
            <span class="metric-label">调用总数</span>
          </div>
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: toolRouting.overview.total_calls && toolRouting.overview.param_validity_rate < 0.95 }">
              {{ (toolRouting.overview.param_validity_rate * 100).toFixed(1) }}%
            </span>
            <span class="metric-label">参数合法率</span>
          </div>
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: toolRouting.overview.allowed && toolRouting.overview.task_success_rate < 0.95 }">
              {{ (toolRouting.overview.task_success_rate * 100).toFixed(1) }}%
            </span>
            <span class="metric-label">任务成功率</span>
          </div>
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: toolRouting.overview.privilege_exposure_rate > 0 }">
              {{ (toolRouting.overview.privilege_exposure_rate * 100).toFixed(1) }}%
            </span>
            <span class="metric-label">越权暴露率</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ formatNum(toolRouting.overview.schema_tokens_total) }}</span>
            <span class="metric-label">Schema Token 总计</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ formatMs(toolRouting.overview.avg_latency_ms) }}</span>
            <span class="metric-label">平均端到端延迟</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ formatMs(toolRouting.overview.p95_latency_ms) }}</span>
            <span class="metric-label">P95 延迟</span>
          </div>
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: toolRouting.overview.blocked }">{{ toolRouting.overview.blocked }}</span>
            <span class="metric-label">约束拦截</span>
          </div>
        </div>

        <table class="obs-table" v-if="toolRouting.by_tool.length">
          <thead>
            <tr><th>工具</th><th>命名空间</th><th>调用</th><th>放行</th><th>拦截</th><th>参数合法率</th><th>任务成功率</th><th>平均延迟</th><th>Schema Token</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in toolRouting.by_tool" :key="t.tool">
              <td>{{ t.tool }}</td>
              <td>{{ t.namespace || '-' }}</td>
              <td>{{ t.total }}</td>
              <td>{{ t.allowed }}</td>
              <td :class="{ 'cell-warn': t.blocked }">{{ t.blocked }}</td>
              <td>{{ (t.param_validity_rate * 100).toFixed(0) }}%</td>
              <td>{{ t.allowed ? (t.task_success_rate * 100).toFixed(0) + '%' : '-' }}</td>
              <td>{{ formatMs(t.avg_latency_ms) }}</td>
              <td>{{ formatNum(t.schema_tokens) }}</td>
            </tr>
          </tbody>
        </table>

        <div class="panel-head" v-if="toolRouting.blocked_events.length">
          <span class="panel-title">约束拦截事件（调用前钩子链 fail-fast 短路，近 30 条）</span>
        </div>
        <table class="obs-table" v-if="toolRouting.blocked_events.length">
          <thead>
            <tr><th>时间</th><th>工具</th><th>拦截环节</th><th>风险等级</th><th>触发方</th><th>原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="(e, i) in toolRouting.blocked_events" :key="i" class="row-reject">
              <td class="cell-time">{{ formatTime(e.created_at) }}</td>
              <td>{{ e.tool_name }}</td>
              <td>{{ blockedCodeLabel(e.blocked_code, e.status) }}</td>
              <td>{{ riskLabel(e.risk_level) }}</td>
              <td>{{ e.routed_by || '-' }}</td>
              <td class="cell-input" :title="e.error">{{ e.error || e.blocked_code || '-' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="!toolRouting.overview.total_calls" class="empty-hint">暂无工具调用记录（发起对话并触发知识库检索 / 联网搜索 / CLI 工具后会自动留痕）</p>
      </section>

      <!-- ============ 工具注册表已迁至工具治理中心（/admin/tools） ============ -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">标准化工具注册表</span>
        </div>
        <p class="empty-hint">工具监控、评测、审批与注册表展示已集中到 <router-link v-if="auth.canOps" to="/admin/tools">工具治理中心</router-link><template v-else>工具治理中心（系统管理）</template>：仅标准化评测通过且启用就绪的个人工具才在注册表显示并可被聊天调用。</p>
      </section>

      <!-- ============ 按 trace 的工具调用链 ============ -->
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">按请求的工具调用链（点击行查看该请求全链路）</span>
        </div>
        <table class="obs-table" v-if="toolChains.length">
          <thead>
            <tr><th>时间</th><th>用户</th><th>Agent</th><th>工具调用序列</th><th>次数</th><th>失败</th><th>工具耗时</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in toolChains" :key="c.trace_id" class="trace-row" @click="openTrace(c.trace_id)">
              <td class="cell-time">{{ formatTime(c.started_at) }}</td>
              <td :title="c.user_id">{{ c.username || c.user_id || '-' }}</td>
              <td><span class="agent-badge" :class="'agent-' + c.agent">{{ c.agent }}</span></td>
              <td class="cell-input" :title="c.tools">{{ c.tools || '-' }}</td>
              <td>{{ c.tool_count }}</td>
              <td :class="{ 'cell-err': c.error_count }">{{ c.error_count }}</td>
              <td>{{ formatMs(c.latency_ms) }}</td>
              <td><span class="status-dot" :class="c.status"></span>{{ statusText(c.status) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-hint">近期暂无工具调用链记录（发起带工具调用的对话后即可看到）</p>
      </section>
      </template>
      </template>
      </template>

      <template v-if="!canOps || opsTab === 'metrics'">
      <!-- ============ 当前用户：上下文窗口 + 记忆模块概览（登录用户均可见） ============ -->
      <section class="panel my-context-panel" v-if="myContext">
        <div class="panel-head">
          <span class="panel-title">我的上下文窗口与记忆概览（{{ rangeLabel }}）</span>
        </div>

        <div class="cards ctx-cards">
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: myContext.context_window.overuse }">{{ formatNum(myContext.context_window.avg_prompt_tokens) }}</span>
            <span class="metric-label">平均上下文 Token/请求</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ formatNum(myContext.context_window.peak_prompt_tokens) }}</span>
            <span class="metric-label">峰值上下文 Token</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ myContext.context_window.requests }}</span>
            <span class="metric-label">统计请求数</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ fmtTok(myContext.compression.thresholds?.window || 128000) }}</span>
            <span class="metric-label">会话窗口容量（Token）</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ myContext.compression.sessions_with_summary }}</span>
            <span class="metric-label">已压缩会话</span>
          </div>
          <div class="metric-card drillable" title="点击查看四层压缩链明细" @click="openGovDrill('compact')">
            <span class="metric-num">{{ formatNum(myContext.compression.tokens_saved) }}</span>
            <span class="metric-label">压缩节省 Token（替换历史层）</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ formatNum(myContext.compression.micro_tokens_saved) }}</span>
            <span class="metric-label">微压缩释放 Token</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ formatNum(myContext.compression.daily_tokens_saved) }}</span>
            <span class="metric-label">日常约束节省 Token</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ ((myContext.compression.compress_ratio || 0) * 100).toFixed(1) }}%</span>
            <span class="metric-label">摘要压缩率（Auto/Full/手动）</span>
          </div>
          <div class="metric-card">
            <span class="metric-num" :class="{ warn: myContext.compression.breaker_sessions }">{{ myContext.compression.breaker_sessions }}</span>
            <span class="metric-label">熔断会话（连败≥3）</span>
          </div>
        </div>
        <p class="ctx-hint">
          上下文告警水位 {{ formatNum(myContext.context_window.warn_threshold) }} Token/请求 ·
          记忆注入预算 {{ myContext.context_window.memory_ctx_chars_budget }} 字符 ·
          Session Memory 首次触发 {{ formatNum(myContext.compression.trigger_tokens) }} Token，当前摘要占用 {{ formatNum(myContext.compression.summary_tokens) }} Token ·
          单轮输出上限 {{ fmtTok(myContext.compression.thresholds?.output_cap || 4000) }}。
        </p>

        <div class="ctx-sub-title">四层压缩链（L0 日常约束 → L1 微压缩 → L2 系统压缩 → L3 兜底）</div>
        <div class="chain-chips">
          <span class="chain-chip">L0 落盘 {{ myContext.compression.chain_events?.offload || 0 }} / 去重 {{ myContext.compression.chain_events?.dedup || 0 }}</span>
          <span class="chain-chip">L1 微压缩 {{ myContext.compression.chain_events?.micro || 0 }}（放弃 {{ myContext.compression.chain_events?.micro_skip || 0 }}）</span>
          <span class="chain-chip">L2 Session Memory {{ myContext.compression.chain_events?.session_memory || 0 }}</span>
          <span class="chain-chip">L2 Auto {{ myContext.compression.chain_events?.auto_compact || 0 }}（命中 {{ myContext.compression.chain_events?.auto_compact_hit || 0 }}）</span>
          <span class="chain-chip">L3 兜底 {{ (myContext.compression.chain_events?.full || 0) + (myContext.compression.chain_events?.full_fallback || 0) }}</span>
          <span class="chain-chip">手动压缩 {{ myContext.compression.chain_events?.manual || 0 }}</span>
          <span class="chain-chip" :class="{ warn: myContext.compression.chain_events?.auto_fail }">失败 {{ myContext.compression.chain_events?.auto_fail || 0 }}</span>
          <span class="chain-chip" :class="{ warn: myContext.compression.chain_events?.auto_blocked }">熔断拦截 {{ myContext.compression.chain_events?.auto_blocked || 0 }}</span>
          <span class="chain-chip">边界标记 session_memory {{ myContext.compression.kind_counts?.session_memory || 0 }} / auto_compact {{ myContext.compression.kind_counts?.auto_compact || 0 }} / full {{ myContext.compression.kind_counts?.full || 0 }} / manual {{ myContext.compression.kind_counts?.manual || 0 }} / note {{ myContext.compression.kind_counts?.note || 0 }}</span>
        </div>
        <p class="ctx-hint">
          L0：单轮输出 ≤{{ fmtTok(myContext.compression.thresholds?.output_cap || 4000) }}，超大工具结果落盘只留约 2KB 预览 + 路径（read_tool_result 按需回读），重复内容替换为引用；
          L1：工具结果合计 ≥{{ fmtTok(myContext.compression.thresholds?.micro_trigger || 20000) }} 进入微压缩，可释放 &lt;10K 则放弃（micro_skip），占位符保留调用/结果配对，本轮与最近 3 个结果不裁剪；
          L2：Session Memory 首次 {{ formatNum(myContext.compression.trigger_tokens) }} Token 起增量记录（只记不替换）；上下文 ≥{{ fmtTok(myContext.compression.thresholds?.auto || 95000) }} 触发 Auto Compaction，summary.md 命中零调用直接替换，未命中 fork 九段式摘要（保留 ≥10K 与最近 3 轮）；
          L3：Auto 连败 3 次或 ≥{{ fmtTok(myContext.compression.thresholds?.full || 119040) }}（窗口 93%）兜底全量压缩，上游上下文超长时被动触发并重试一次（当前最大连败 {{ myContext.compression.fail_streak_max }}）；
          摘要持久于短期记忆 summary.md，数据库缺失时按文件恢复。
        </p>
        <div class="gov-actions">
          <button class="btn btn-ghost sm" @click="openGovDrill('compact')">压缩链明细</button>
          <button class="btn btn-ghost sm" @click="openGovDrill('memory')">记忆写路径明细</button>
        </div>

        <div class="ctx-sub-title">按对话查看上下文与记忆</div>
        <p class="ctx-hint">
          上下文与记忆按累计总量统计意义有限：请在下方「对话维度统一视图」选择具体对话，
          按天查看每次请求的上下文段构成、请求追踪与调用链，以及该对话/该用户的记忆注入情况。
        </p>

        <div class="ctx-sub-title">记忆模块概览</div>
        <div class="cards mem-cards">
          <div class="metric-card drillable" title="点击查看记忆明细" @click="openMemoryDrill('all', '记忆条数')"><span class="metric-num">{{ myContext.memory.total }}</span><span class="metric-label">记忆条数 / 上限 {{ myContext.memory.storage_limit }}</span></div>
          <div class="metric-card drillable" title="点击查看画像卡候选明细" @click="openMemoryDrill('profile', '画像卡候选')"><span class="metric-num">{{ myContext.memory.profile_count }}</span><span class="metric-label">画像卡候选</span></div>
          <div class="metric-card drillable" title="点击查看被检索命中明细" @click="openMemoryDrill('retrieved', '被检索命中')"><span class="metric-num">{{ myContext.memory.retrieved_count }}</span><span class="metric-label">被检索命中</span></div>
          <div class="metric-card drillable" title="点击查看从未使用明细" @click="openMemoryDrill('never_used', '从未使用')"><span class="metric-num">{{ myContext.memory.never_used_count }}</span><span class="metric-label">从未使用</span></div>
          <div class="metric-card drillable" title="点击查看重要度明细" @click="openMemoryDrill('importance', '平均重要度')"><span class="metric-num">{{ myContext.memory.avg_importance }}</span><span class="metric-label">平均重要度</span></div>
          <div class="metric-card drillable" title="点击查看 Markdown 写路径事件" @click="openGovDrill('memory')"><span class="metric-num">{{ myContext.memory.write_events?.total || 0 }}</span><span class="metric-label">写路径事件（{{ rangeLabel }}）</span></div>
        </div>
        <div v-if="myContext.memory.total" class="mem-types">
          <div v-for="t in myContext.memory.by_type" :key="t.type" class="mem-type-row drillable" :title="'点击查看「' + t.label + '」类记忆明细'" @click="openMemoryDrill('type:' + t.type, t.label + '类记忆')">
            <span class="mem-type-label">{{ t.label }}</span>
            <div class="mem-type-bar"><span :style="{ width: memTypeWidth(t.count) }"></span></div>
            <span class="mem-type-count">{{ t.count }}</span>
          </div>
          <p class="ctx-hint">
            记忆字符占用 {{ formatNum(myContext.memory.total_chars) }} ·
            画像卡预算 {{ myContext.memory.profile_chars_budget }} 字符、两层合计 {{ myContext.memory.ctx_chars_budget }} 字符 ·
            存储水位 {{ (myContext.memory.storage_usage * 100).toFixed(1) }}%。
          </p>
        </div>
        <p v-else class="empty-hint">还没有沉淀任何跨会话长期记忆。多轮对话后 extractMemories 会自动抽取你的用户信息与反馈纠正。</p>

        <div class="ctx-sub-title">静态/动态 Markdown 存储（参考 Claude Code：静态三层 + 短期四模块 + 长期四类型）</div>
        <div class="layer-chips">
          <span v-for="l in myContext.memory.layers || []" :key="l.layer" class="layer-chip" :class="{ on: l.exists }"
                :title="l.path ? '文件：memory/' + l.path : '尚未落盘'">{{ l.label }} · {{ l.chars }} 字</span>
        </div>
        <p class="ctx-hint">
          写路径按动作：<template v-for="(n, a) in myContext.memory.write_events?.by_action || {}" :key="a">{{ memActionLabel(a) }} {{ n }} · </template>
          双写 SQLite 查询镜像与 markdown 文件（长期记忆每条独立 .md + MEMORY.md 索引；短期记忆按会话落 transcript.jsonl 黑匣子/summary 摘要，task_state 会话期内存对象、结束落盘）。可在侧栏「我的记忆 → 记忆文件」查看与编辑。
        </p>
      </section>

      <!-- ============ 对话维度统一视图（需求2+3：上下文构成 × 请求追踪 × 调用链 合并） ============ -->
      <section class="panel" v-if="auth.isAuthenticated && !auth.isGuest">
        <div class="panel-head">
          <span class="panel-title">对话维度统一视图（上下文构成 · 请求追踪 · 调用链 · 记忆）</span>
        </div>
        <div class="sess-select-row">
          <select v-model="selSessionId" class="filter-select sess-select" @change="onSessionChange">
            <option value="">选择对话…</option>
            <option v-for="s in mySessions" :key="s.session_id" :value="s.session_id">
              {{ s.title }}（{{ s.requests }} 次请求）
            </option>
          </select>
          <div v-if="sessObs && sessObs.days.length" class="day-chips">
            <button class="day-chip" :class="{ active: !sessDay }" @click="selectDay('')">全部</button>
            <button v-for="d in sessObs.days" :key="d" class="day-chip" :class="{ active: sessDay === d }"
                    @click="selectDay(d)">{{ d.slice(5) }}</button>
          </div>
        </div>

        <p v-if="!selSessionId" class="empty-hint">选择一个对话后，可按天查看该对话的上下文构成、请求追踪与调用链（与聊天页上下文缩略图同口径）。</p>
        <p v-else-if="sessLoading" class="mini-loading">加载中…</p>
        <p v-else-if="sessError" class="inline-error">{{ sessError }}</p>
        <template v-else-if="sessObs">
          <div class="sess-obs-meta">
            <span>对话：{{ sessObs.title }}</span>
            <span>用户：{{ sessObs.username || sessObs.user_id || '-' }}</span>
            <span>请求 {{ sessObs.traces.length }} 次</span>
            <span v-if="sessObs.selected_trace_id">选中请求：{{ formatTime(sessObs.selected_at) }} · {{ formatNum(sessObs.total_tokens) }} tok</span>
          </div>
          <div class="sess-tabs">
            <button class="sess-tab" :class="{ active: sessTab === 'context' }" @click="sessTab = 'context'">上下文构成</button>
            <button v-if="canOps" class="sess-tab" :class="{ active: sessTab === 'traces' }" @click="sessTab = 'traces'">请求追踪</button>
            <button v-if="canOps" class="sess-tab" :class="{ active: sessTab === 'spans' }" @click="sessTab = 'spans'">调用链</button>
            <button class="sess-tab" :class="{ active: sessTab === 'memory' }" @click="sessTab = 'memory'">记忆</button>
          </div>

          <!-- Tab1：选中请求的上下文段构成 -->
          <div v-show="sessTab === 'context'">
            <table v-if="sessObs.segments.length" class="obs-table">
              <thead><tr><th>上下文段</th><th>Token</th><th>占比</th></tr></thead>
              <tbody>
                <tr v-for="seg in sessObs.segments" :key="seg.segment">
                  <td>{{ seg.label }}</td>
                  <td>{{ formatNum(seg.tokens) }}</td>
                  <td class="share-cell">
                    <div class="share-bar"><span :style="{ width: (seg.share * 100).toFixed(1) + '%' }"></span></div>
                    {{ (seg.share * 100).toFixed(1) }}%
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-else class="empty-hint">该请求暂无段构成记录。</p>
          </div>

          <!-- Tab2：该对话的全部请求追踪（可按天筛选，点击行切换选中请求） -->
          <div v-show="sessTab === 'traces'">
            <table v-if="sessObs.traces.length" class="obs-table trace-table">
              <thead><tr><th>时间</th><th>Agent</th><th>输入</th><th>状态</th><th>延迟</th><th>Prompt Token</th></tr></thead>
              <tbody>
                <tr v-for="t in sessObs.traces" :key="t.trace_id"
                    class="trace-row" :class="{ 'row-selected': t.trace_id === sessObs.selected_trace_id }"
                    @click="selectSessTrace(t.trace_id)">
                  <td class="cell-time">{{ formatTime(t.started_at) }}</td>
                  <td><span class="agent-badge" :class="'agent-' + t.agent">{{ t.agent }}</span></td>
                  <td class="cell-input" :title="t.input">{{ t.input || '-' }}</td>
                  <td><span class="status-dot" :class="t.status"></span>{{ statusText(t.status) }}</td>
                  <td>{{ formatMs(t.latency_ms) }}</td>
                  <td>{{ formatNum(t.prompt_tokens) }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="empty-hint">该天暂无请求记录。</p>
            <p class="ctx-hint">点击某行即切换上方「上下文构成 / 调用链」对应的请求；也可点「全链路弹窗」查看完整 prompt/response。</p>
          </div>

          <!-- Tab3：选中请求的调用链（span 序列，点击展开入参/结果，可进全链路弹窗） -->
          <div v-show="sessTab === 'spans'">
            <div v-if="sessObs.spans.length" class="span-list sess-span-list">
              <div v-for="span in sessObs.spans" :key="span.id" class="span-item" :class="'kind-' + span.kind">
                <button class="span-head" @click="toggleSpan(span.id)">
                  <span class="span-seq">#{{ span.seq }}</span>
                  <span class="span-kind-badge" :class="span.kind">{{ span.kind }}</span>
                  <span class="span-name">{{ span.name }}</span>
                  <span class="status-dot" :class="span.status"></span>
                  <span class="span-meta">{{ formatMs(span.latency_ms) }}<template v-if="spanTokenText(span)"> · {{ spanTokenText(span) }}</template></span>
                  <span class="span-arrow" :class="{ open: expandedSpans.has(span.id) }">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                      <polyline points="6 9 12 15 18 9" />
                    </svg>
                  </span>
                </button>
                <div v-if="expandedSpans.has(span.id)" class="span-body">
                  <p v-if="span.error" class="inline-error">{{ span.error }}</p>
                  <p v-if="spanMetaText(span)" class="span-ctx-meta">{{ spanMetaText(span) }}</p>
                  <p v-if="span.ctx_tokens" class="span-ctx-meta">窗口水位 {{ fmtTok(span.ctx_tokens) }} / {{ fmtTok(128000) }}</p>
                  <template v-if="span.input">
                    <span class="span-io-label">PROMPT / 入参</span>
                    <pre class="span-text">{{ span.input }}</pre>
                  </template>
                  <template v-if="span.output">
                    <span class="span-io-label">RESPONSE / 结果</span>
                    <pre class="span-text">{{ span.output }}</pre>
                  </template>
                  <p v-if="!span.input && !span.output && !span.error" class="empty-hint">（无附加内容）</p>
                </div>
              </div>
            </div>
            <p v-else class="empty-hint">该请求暂无调用链 span。</p>
            <button v-if="canOps && sessObs.selected_trace_id" class="btn btn-ghost sm" @click="openTrace(sessObs.selected_trace_id)">
              查看全链路弹窗（含详细日志）
            </button>
          </div>

          <!-- Tab4：该对话的记忆注入 + 该用户的记忆总览（需求3：按对话/按用户展现） -->
          <div v-show="sessTab === 'memory'">
            <div class="ctx-sub-title">该对话的记忆注入（memory 系段跨请求累计）</div>
            <table v-if="sessObs.memory && sessObs.memory.segments.length" class="obs-table">
              <thead><tr><th>记忆段</th><th>累计 Token</th><th>注入次数</th></tr></thead>
              <tbody>
                <tr v-for="seg in sessObs.memory.segments" :key="seg.segment">
                  <td>{{ seg.label }}</td>
                  <td>{{ formatNum(seg.tokens) }}</td>
                  <td>{{ seg.count }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="empty-hint">该对话暂未注入过记忆。</p>
            <template v-if="sessObs.user_memory">
              <div class="ctx-sub-title">该用户的记忆总览（{{ sessObs.username || sessObs.user_id }}）</div>
              <div class="cards mem-cards">
                <div class="metric-card"><span class="metric-num">{{ sessObs.user_memory.total }}</span><span class="metric-label">记忆条数 / 上限 {{ sessObs.user_memory.storage_limit }}</span></div>
                <div class="metric-card"><span class="metric-num">{{ sessObs.user_memory.retrieved_count }}</span><span class="metric-label">被检索命中</span></div>
                <div class="metric-card"><span class="metric-num">{{ sessObs.user_memory.never_used_count }}</span><span class="metric-label">从未使用</span></div>
                <div class="metric-card"><span class="metric-num">{{ sessObs.user_memory.avg_importance }}</span><span class="metric-label">平均重要度</span></div>
              </div>
            </template>
          </div>
        </template>
      </section>
      </template>

      <!-- ============ 个人工具管理已迁至工具治理中心（/admin/tools） ============ -->
      <section v-if="auth.isAuthenticated && !auth.isGuest" class="panel">
        <p class="empty-hint">个人工具的添加确认、监控、评测、审批与注册展示已集中到 <router-link v-if="auth.canOps" to="/admin/tools">工具治理中心</router-link><template v-else>工具治理中心（系统管理，仅管理员/运维可见）</template>。</p>
      </section>
    </div>

    <!-- ============ 图表 hover tooltip ============ -->
    <div v-if="tooltip.show" class="chart-tooltip" :style="{ left: tooltip.x + 14 + 'px', top: tooltip.y + 14 + 'px' }" role="tooltip">
      <div v-for="(line, i) in tooltip.lines" :key="i" :class="['tip-line', i === 0 ? 'tip-title' : '']">{{ line }}</div>
    </div>

    <!-- ============ Trace 详情弹窗（span 时间线） ============ -->
    <div v-if="canOps && traceDetail" class="modal-overlay" @click.self="closeTrace">
      <div class="modal-box trace-box">
        <div class="trace-head">
          <h3 class="modal-title">
            全链路追踪 · <span class="agent-badge" :class="'agent-' + traceDetail.agent">{{ traceDetail.agent }}</span>
          </h3>
          <button class="icon-btn" title="关闭" @click="closeTrace">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div class="trace-meta">
          <span>状态：<b :class="{ 'cell-err': traceDetail.status === 'error' }">{{ statusText(traceDetail.status) }}</b></span>
          <span>延迟：{{ formatMs(traceDetail.latency_ms) }}</span>
          <span>Token：{{ traceDetail.prompt_tokens }} + {{ traceDetail.completion_tokens }}</span>
          <span title="LLM 真实 usage 累计 / 非 LLM 环节估算累计 / 本次请求窗口水位峰值">
            LLM {{ formatNum(traceTokenSummary.llm) }} · 工具结果 ≈{{ formatNum(traceTokenSummary.tool) }} · 水位 {{ fmtTok(traceTokenSummary.ctx) }}/{{ fmtTok(traceTokenSummary.window) }}
          </span>
          <span :title="traceDetail.user_id">用户：{{ traceDetail.username || traceDetail.user_id || '-' }}</span>
          <span>开始：{{ formatTime(traceDetail.started_at) }}</span>
        </div>
        <div class="trace-input">
          <span class="span-kind-badge step">输入</span>
          <pre class="span-text">{{ traceDetail.input || '-' }}</pre>
        </div>
        <div v-if="traceFinalAnswer" class="trace-answer">
          <div class="trace-answer-head">
            <span class="span-io-label">AI回复内容</span>
            <button class="btn btn-ghost sm" @click="copyTraceAnswer">复制</button>
          </div>
          <pre class="span-text answer-text">{{ traceFinalAnswer }}</pre>
        </div>
        <p v-if="traceDetail.error" class="inline-error">错误：{{ traceDetail.error }}</p>

        <div class="trace-log-actions">
          <button class="btn btn-ghost" :aria-expanded="showTraceLogs" @click="toggleTraceLogs">
            {{ showTraceLogs ? '返回步骤追踪' : '查看详细日志' }}
          </button>
          <span class="trace-id">请求 ID：{{ traceDetail.id }}</span>
        </div>
        <section v-if="showTraceLogs" class="trace-logs" aria-label="本次请求详细日志" :aria-busy="logsLoading">
          <div class="trace-log-actions">
            <span>已加载 {{ traceLogs.length }} 条记录</span>
            <button class="btn btn-ghost sm" :disabled="logsLoading" @click="loadTraceLogs(false)">刷新日志</button>
          </div>
          <p class="log-notice">按请求 ID 展示已落盘日志（含异常堆栈），凭据与个人信息（手机号/证件号/银行卡/邮箱）已脱敏，原始文件不改写。旧日志仅能关联带 trace ID 的记录；未记录、已清理或独立进程中未关联的日志无法补齐。</p>
          <p v-for="warning in logWarnings" :key="warning" class="log-notice">{{ warning }}</p>
          <p v-if="logsError" class="inline-error" role="alert">{{ logsError }}</p>
          <div v-if="traceLogs.length" class="log-entries">
            <div v-for="entry in traceLogs" :key="entry.id" class="log-entry">
              <span class="span-io-label">{{ entry.source }}<template v-if="entry.truncated"> · 本条过长，已截断</template></span>
              <pre class="span-text trace-log-text">{{ entry.text }}</pre>
            </div>
          </div>
          <p v-else-if="!logsLoading && !logsError" class="empty-hint">暂未找到可关联日志。可返回步骤追踪查看已保存的 prompt、response 与错误信息。</p>
          <p v-if="logsLoading" class="mini-loading" role="status">日志加载中…</p>
          <button v-if="logsCursor" class="btn btn-ghost" :disabled="logsLoading" @click="loadTraceLogs(true)">继续加载日志</button>
        </section>
        <div v-else class="span-list">
          <div v-for="span in traceDetail.spans" :key="span.id" class="span-item" :class="'kind-' + span.kind">
            <button class="span-head" @click="toggleSpan(span.id)">
              <span class="span-seq">#{{ span.seq }}</span>
              <span class="span-kind-badge" :class="span.kind">{{ span.kind }}</span>
              <span class="span-name">{{ span.name }}</span>
              <span class="span-bar" :title="formatMs(span.latency_ms)"><i :class="span.kind" :style="{ width: spanBarWidth(span) }"></i></span>
              <span class="status-dot" :class="span.status"></span>
              <span class="span-meta">{{ formatMs(span.latency_ms) }}<template v-if="spanTokenText(span)"> · {{ spanTokenText(span) }}</template></span>
              <span class="span-arrow" :class="{ open: expandedSpans.has(span.id) }">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                  <polyline points="6 9 12 15 18 9" />
                </svg>
              </span>
            </button>
            <div v-if="expandedSpans.has(span.id)" class="span-body">
              <p v-if="span.error" class="inline-error">{{ span.error }}</p>
              <p v-if="spanMetaText(span)" class="span-ctx-meta">{{ spanMetaText(span) }}</p>
              <p v-if="span.ctx_tokens" class="span-ctx-meta">窗口水位 {{ fmtTok(span.ctx_tokens) }} / {{ fmtTok(128000) }}</p>
              <template v-if="span.input">
                <span class="span-io-label">PROMPT / 入参</span>
                <pre class="span-text">{{ span.input }}</pre>
              </template>
              <template v-if="span.output">
                <span class="span-io-label">RESPONSE / 结果</span>
                <pre class="span-text">{{ span.output }}</pre>
              </template>
              <p v-if="!span.input && !span.output && !span.error" class="empty-hint">（无附加内容）</p>
            </div>
          </div>
          <p v-if="!traceDetail.spans.length" class="empty-hint">该 trace 暂无 span</p>
        </div>
      </div>
    </div>

    <!-- ============ 指标钻取明细弹窗（数字点击 → 明细列表） ============ -->
    <div v-if="drill" class="modal-overlay" @click.self="drill = null">
      <div class="modal-box drill-box" role="dialog" :aria-label="drill.label + ' 明细'">
        <div class="trace-head">
          <h3 class="modal-title">{{ drill.label }} · 明细（{{ rangeLabel }} · 共 {{ drill.total }} 条）</h3>
          <button class="icon-btn" title="关闭" @click="drill = null">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div v-if="drill.loading" class="mini-loading">加载明细…</div>
        <div v-else-if="drill.error" class="inline-error">{{ drill.error }}</div>
        <div v-else-if="!drill.rows.length" class="empty-hint">暂无明细数据。</div>
        <div v-else class="drill-scroll">
          <table class="obs-table drill-table">
            <thead>
              <tr><th v-for="col in drill.columns" :key="col.key">{{ col.label }}</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in drill.rows" :key="i">
                <td v-for="col in drill.columns" :key="col.key" :title="String(row[col.key] ?? '')">{{ row[col.key] ?? '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <!-- 需求2：分页控件（单页不呈现过多数据） -->
        <div v-if="drill.total > 0" class="drill-pager">
          <button class="btn btn-ghost sm" :disabled="drill.loading || drill.page <= 1" @click="goDrillPage(-1)">上一页</button>
          <span class="pager-info">第 {{ drill.page }} / {{ drillTotalPages }} 页 · 共 {{ drill.total }} 条</span>
          <button class="btn btn-ghost sm" :disabled="drill.loading || drill.page >= drillTotalPages" @click="goDrillPage(1)">下一页</button>
        </div>
        <!-- 需求2：口径计算说明（呈现在表格下面） -->
        <div v-if="drill.explanation" class="drill-explain">
          <span class="drill-explain-title">📐 口径说明</span>
          <p>{{ drill.explanation }}</p>
        </div>
      </div>
    </div>

    <!-- 轻量 toast 反馈 -->
    <transition name="fade">
      <div v-if="toast" class="obs-toast" :class="{ error: toast.error }">{{ toast.message }}</div>
    </transition>

    <!-- 需求1：记忆概览钻取弹窗（点击记忆指标/类型条查看明细 + 口径说明） -->
    <div v-if="memDrill" class="modal-overlay" @click.self="closeMemoryDrill">
      <div class="modal-box mem-drill-box" role="dialog" aria-label="记忆明细">
        <div class="trace-head">
          <h3 class="modal-title">记忆明细 · {{ memDrill.label }}</h3>
          <button class="icon-btn" title="关闭" @click="closeMemoryDrill">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <p class="mem-drill-count">共 {{ memDrill.total }} 条</p>
        <div v-if="memDrill.loading" class="mini-loading">加载中…</div>
        <div v-else-if="memDrill.error" class="inline-error">{{ memDrill.error }}</div>
        <template v-else>
          <table class="obs-table mem-drill-table" v-if="memDrill.items.length">
            <thead>
              <tr><th>类型</th><th>内容</th><th>重要度</th><th>画像卡</th><th>关键词</th><th>最近使用</th><th>更新时间</th></tr>
            </thead>
            <tbody>
              <tr v-for="m in memDrill.items" :key="m.id">
                <td>{{ m.type_label }}</td>
                <td class="cell-input" :title="m.content">{{ m.content }}</td>
                <td>{{ m.importance }}</td>
                <td>{{ m.is_profile ? '✓' : '—' }}</td>
                <td class="cell-input" :title="(m.keywords || []).join('、')">{{ (m.keywords || []).join('、') || '-' }}</td>
                <td>{{ m.last_used_at ? formatTime(m.last_used_at) : '从未' }}</td>
                <td>{{ formatTime(m.updated_at) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty-hint">该口径下暂无记忆明细。</p>
          <div class="drill-explain">📐 口径说明：{{ memDrill.explanation }}</div>
        </template>
      </div>
    </div>

    <!-- 记忆/上下文治理钻取弹窗（Compaction 压缩链事件流 + 摘要边界标记 / 静态动态写路径事件） -->
    <div v-if="govDrill" class="modal-overlay" @click.self="govDrill = null">
      <div class="modal-box drill-box" role="dialog" :aria-label="govDrill.label">
        <div class="trace-head">
          <h3 class="modal-title">{{ govDrill.label }}</h3>
          <button class="icon-btn" title="关闭" @click="govDrill = null">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div v-if="govDrill.loading" class="mini-loading">加载中…</div>
        <div v-else-if="govDrill.error" class="inline-error">{{ govDrill.error }}</div>
        <template v-else>
          <template v-if="govDrill.kind === 'compact'">
            <div class="ctx-sub-title">压缩链事件流（最近 {{ govDrill.events.length }} 条）</div>
            <table v-if="govDrill.events.length" class="obs-table">
              <thead><tr><th>时间</th><th>类型</th><th>压缩前 Token</th><th>压缩后 Token</th><th>明细</th></tr></thead>
              <tbody>
                <tr v-for="(e, i) in govDrill.events" :key="i">
                  <td>{{ e.created_at }}</td>
                  <td>{{ e.label }}</td>
                  <td>{{ e.before_tokens || '-' }}</td>
                  <td>{{ e.after_tokens || '-' }}</td>
                  <td class="cell-input" :title="e.detail">{{ e.detail || '-' }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="empty-hint">暂无压缩链事件。发起多轮对话或手动压缩后即可看到。</p>
            <div class="ctx-sub-title">会话摘要边界标记（compact_kind / 压缩边界与覆盖点 / 熔断计数）</div>
            <table v-if="govDrill.summaries.length" class="obs-table">
              <thead><tr><th>会话</th><th>类型</th><th>压缩前 Token</th><th>摘要 Token</th><th>边界 / 覆盖点</th><th>连败</th><th>用户编辑</th><th>更新时间</th></tr></thead>
              <tbody>
                <tr v-for="s in govDrill.summaries" :key="s.session_id">
                  <td class="cell-input" :title="s.session_id">{{ (s.session_id || '').slice(0, 8) }}</td>
                  <td>{{ compactKindLabel(s.compact_kind) }}</td>
                  <td>{{ s.before_tokens || '-' }}</td>
                  <td>{{ s.token_estimate }}</td>
                  <td class="cell-input" :title="'压缩边界 ' + (s.boundary_message_id || '-') + ' / 摘要覆盖点 ' + (s.covered_until_message_id || '-')">{{ (s.boundary_message_id || '-').slice(0, 8) }} / {{ (s.covered_until_message_id || '-').slice(0, 8) }}</td>
                  <td :class="{ 'cell-err': s.fail_streak >= 3 }">{{ s.fail_streak }}</td>
                  <td>{{ Number(s.edited_by_user) ? '✓' : '—' }}</td>
                  <td>{{ s.updated_at }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="empty-hint">暂无会话摘要。</p>
          </template>
          <template v-else>
            <table v-if="govDrill.events.length" class="obs-table">
              <thead><tr><th>时间</th><th>动作</th><th>记忆层</th><th>类型</th><th>明细</th></tr></thead>
              <tbody>
                <tr v-for="(e, i) in govDrill.events" :key="i">
                  <td>{{ e.created_at }}</td>
                  <td>{{ e.action_label }}</td>
                  <td>{{ e.layer_label }}</td>
                  <td>{{ e.memory_type || '-' }}</td>
                  <td class="cell-input" :title="e.detail">{{ e.detail || '-' }}</td>
                </tr>
              </tbody>
            </table>
            <p v-else class="empty-hint">暂无写路径事件。记忆增删改与静态层编辑都会在此留痕。</p>
          </template>
          <div class="drill-explain">
            <span class="drill-explain-title">📐 口径说明</span>
            <p>{{ govDrill.explanation }}</p>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, watch, onMounted, onActivated } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useQueryCacheStore, TTL_MONITOR } from '@/stores/queryCache'
import ObsDashboard from '@/components/observability/ObsDashboard.vue'
import ObsLogs from '@/components/observability/ObsLogs.vue'
import ObsAlerts from '@/components/observability/ObsAlerts.vue'
import ObsCachePanel from '@/components/observability/ObsCachePanel.vue'
import { getMyObservabilityMetrics, getMyContextOverview, getMyContextSessions, getObservabilityMetrics, getObservabilityTraces, getObservabilityTrace, getObservabilityTraceLogs, getSkillReviews, getToolRoutingMetrics, getSessionObservability, getSedimentSuggestions, getObsMonitor, getObsToolChains, getObsDrill, getMyMemoryDrill, getMyCompactDrill, getMyMemoryEvents } from '@/api'
import { fmtTok, spanTokenText, spanMetaText, traceTokenSummary as traceTokenSummaryOf } from '@/utils/contextTokens'

const auth = useAuthStore()
const queryCache = useQueryCacheStore()
const route = useRoute()
const router = useRouter()
const canOps = computed(() => auth.canOps)
// 在线评测均分（GEval 归一化 0-1；无样本显示 —）
const onlineEvalText = computed(() => {
  const v = monitor.value?.quality?.online_eval?.avg_geval
  return v != null ? Number(v).toFixed(2) : '—'
})
const businessMetrics = ref(null)
const myContext = ref(null)
const mySessions = ref([])
const openSession = ref('')

const days = ref(7)
const rangeMode = ref('days') // 'days' | 'today'（需求1：今日统计）
const rangeLabel = computed(() => (rangeMode.value === 'today' ? '今日' : `近${days.value}天`))
const loading = ref(false)
const loadingTraces = ref(false)
const errorMsg = ref('')
const metrics = ref(null)
const skillReviews = ref(null)
const toolRouting = ref(null)
const sediment = ref(null)
// 需求1：记忆概览钻取弹窗（点击记忆指标/类型条查看明细）
const memDrill = ref(null)
// 轻量 toast（与 Skill 配置页一致的反馈方式）
const toast = ref(null)
let toastTimer = null
function showToast(message, error = false) {
  toast.value = { message, error }
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = null }, 2600)
}
// 四大模块页签（需求：DashBoard / 日志记录 / 指标监控 / 链路追踪）；支持 ?tab= 深链（全局告警铃铛跳转）
const opsTab = ref(['dashboard', 'logs', 'metrics', 'traces'].includes(route.query.tab) ? route.query.tab : 'dashboard')
// 深链定位：?tab=metrics&focus=alerts → 切页签并滚动到告警区
function applyRouteQuery() {
  const tab = route.query.tab
  if (['dashboard', 'logs', 'metrics', 'traces'].includes(tab)) opsTab.value = tab
  if (route.query.focus === 'alerts') {
    nextTick(() => {
      setTimeout(() => {
        document.getElementById('obs-alerts')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      }, 300)
    })
  }
}
watch(() => route.query, applyRouteQuery)
// 链路追踪子页签：对话型链路 / 工具调用链路
const traceTab = ref('chat')
const monitor = ref(null)
const toolChains = ref([])
// 对话维度统一视图（需求2）
const selSessionId = ref('')
const sessDay = ref('')
const sessObs = ref(null)
const sessLoading = ref(false)
const sessError = ref('')
const sessTab = ref('context')
let sessRequestId = 0
const traces = ref([])
const total = ref(0)
const offset = ref(0)
const filterAgent = ref('')
const filterStatus = ref('')
const traceDetail = ref(null)
const expandedSpans = ref(new Set())
const showTraceLogs = ref(false)
const traceLogs = ref([])
const logsLoading = ref(false)
const logsLoaded = ref(false)
const logsCursor = ref(null)
const logsError = ref('')
const logWarnings = ref([])
let traceRequestId = 0
let logsRequestId = 0

onMounted(() => { loadAll(0); applyRouteQuery() })

// 按当前 Tab 按需加载（不再全量 Promise.all）：未加载/版本落后的 Tab 才发请求，
// 热切换不重复拉相同 GET；KeepAlive 回页时按资源版本判断是否重验
const loadedTabs = new Set()
const tabVersions = {}
function versionSig() {
  const v = queryCache.versions
  return `${v.usage || 0}|${v.alerts || 0}|${v.skills || 0}|${v.sessions || 0}`
}
const TAB_LOADERS = {
  dashboard: (r) => Promise.all([loadMetrics(r), loadMonitor(r)]),
  logs: () => Promise.resolve(), // ObsLogs 组件自行加载
  metrics: (r) => Promise.all([loadMetrics(r), loadMonitor(r), loadSkillReviews(), loadToolRouting(r), loadSediment(), loadMyContext()]),
  traces: (r) => Promise.all([loadTraces(), loadToolChains(r)]),
}
async function loadTab(tab, refresh = 0) {
  const loader = TAB_LOADERS[tab]
  if (!loader) return
  await loader(refresh)
  loadedTabs.add(tab)
  tabVersions[tab] = versionSig()
}
watch(opsTab, (tab) => {
  if (!canOps.value) return
  if (!loadedTabs.has(tab) || tabVersions[tab] !== versionSig()) {
    loadTab(tab).catch(() => {})
  }
})
onActivated(() => {
  if (canOps.value && loadedTabs.has(opsTab.value) && tabVersions[opsTab.value] !== versionSig()) {
    loadTab(opsTab.value).catch(() => {})
  }
})

async function loadAll(refresh = 0) {
  loading.value = true
  errorMsg.value = ''
  try {
    if (canOps.value) {
      if (refresh) loadedTabs.clear() // 手动刷新/改时间范围：其余 Tab 切到时重拉
      await loadTab(opsTab.value, refresh)
    } else {
      businessMetrics.value = null
      const data = await queryCache.cachedGet('sessions',
        { kind: 'my-metrics', days: days.value, range: rangeMode.value },
        async () => (await getMyObservabilityMetrics(days.value, rangeMode.value)).data,
        { ttl: TTL_MONITOR, force: refresh === 1, onRevalidate: (d) => { businessMetrics.value = d } })
      businessMetrics.value = data
      await loadMyContext()
    }
    // 时间范围切换后同步刷新已选对话的统一视图
    if (selSessionId.value) loadSessObs()
  } catch (err) {
    errorMsg.value = canOps.value ? (err.response?.data?.detail || err.message || '加载失败') : '个人使用概览加载失败，请稍后刷新重试。'
  } finally {
    loading.value = false
  }
}

// 当前用户上下文窗口 + 记忆模块概览：仅登录用户可见，加载失败不阻断其余面板
async function loadMyContext() {
  if (!auth.isAuthenticated || auth.isGuest) { myContext.value = null; mySessions.value = []; return }
  try {
    const res = await getMyContextOverview(days.value, rangeMode.value)
    myContext.value = res.data
  } catch {
    myContext.value = null
  }
  // 会话列表：统一会话视图的选择器数据源，拉取失败不阻断主面板
  try {
    const res = await getMyContextSessions(days.value, 20, rangeMode.value)
    mySessions.value = res.data.sessions || []
  } catch {
    mySessions.value = []
  }
}

async function loadMetrics(refresh = 0) {
  if (!canOps.value) return
  const data = await queryCache.cachedGet('usage',
    { kind: 'metrics', days: days.value, range: rangeMode.value },
    async () => (await getObservabilityMetrics(days.value, rangeMode.value, refresh)).data,
    { ttl: TTL_MONITOR, force: refresh === 1, onRevalidate: (d) => { metrics.value = d } })
  metrics.value = data
}

async function loadSkillReviews() {
  if (!canOps.value) return
  try {
    const data = await queryCache.cachedGet('skills', { kind: 'reviews', limit: 50 },
      async () => (await getSkillReviews(50)).data,
      { ttl: TTL_MONITOR, onRevalidate: (d) => { skillReviews.value = d } })
    skillReviews.value = data
  } catch {
    skillReviews.value = null  // 预审面板加载失败不阻断其余指标
  }
}

function reviewActionLabel(action) {
  return { create: '新建', update: '编辑', import: '导入' }[action] || action || '-'
}

function reviewRiskLabel(risk) {
  return { high: '高危安全', unsupported: '能力不支持' }[risk] || '无'
}

// Tool Routing 调用链分析（req2）：加载失败不阻断其余面板
async function loadToolRouting(refresh = 0) {
  if (!canOps.value) return
  try {
    const data = await queryCache.cachedGet('usage',
      { kind: 'tool-routing', days: days.value, range: rangeMode.value },
      async () => (await getToolRoutingMetrics(days.value, rangeMode.value, refresh)).data,
      { ttl: TTL_MONITOR, force: refresh === 1, onRevalidate: (d) => { toolRouting.value = d } })
    toolRouting.value = data
  } catch {
    toolRouting.value = null
  }
}

// 指标监控四方面聚合（质量/安全/业务）：加载失败不阻断其余面板
async function loadMonitor(refresh = 0) {
  if (!canOps.value) return
  try {
    const data = await queryCache.cachedGet('usage',
      { kind: 'monitor', days: days.value, range: rangeMode.value },
      async () => (await getObsMonitor(days.value, rangeMode.value, refresh)).data,
      { ttl: TTL_MONITOR, force: refresh === 1, onRevalidate: (d) => { monitor.value = d } })
    monitor.value = data
  } catch {
    monitor.value = null
  }
}

// 按 trace 聚合的工具调用链：加载失败不阻断其余面板
async function loadToolChains(refresh = 0) {
  if (!canOps.value) return
  try {
    const data = await queryCache.cachedGet('usage',
      { kind: 'tool-chains', days: days.value, range: rangeMode.value, limit: 50 },
      async () => (await getObsToolChains(days.value, rangeMode.value, 50, refresh)).data,
      { ttl: TTL_MONITOR, force: refresh === 1, onRevalidate: (d) => { toolChains.value = d.items || [] } })
    toolChains.value = data.items || []
  } catch {
    toolChains.value = []
  }
}

// 质量指标卡：最近一次回归运行的答案相关度均分
const latestEvalText = computed(() => {
  const run = monitor.value?.quality?.eval_runs?.[0]
  if (!run || !run.avg || !run.avg.total) return '-'
  return (run.avg.answer_relevance * 100).toFixed(0) + '%'
})

// 能力缺口沉淀建议（需求6）：加载失败不阻断其余面板
async function loadSediment() {
  if (!canOps.value) return
  try {
    const data = await queryCache.cachedGet('usage', { kind: 'sediment', days: 30, min: 3 },
      async () => (await getSedimentSuggestions(30, 3)).data,
      { ttl: TTL_MONITOR, onRevalidate: (d) => { sediment.value = d } })
    sediment.value = data
  } catch {
    sediment.value = null
  }
}

async function openMemoryDrill(kind, label) {
  memDrill.value = { kind, label, loading: true, error: '', items: [], total: 0, explanation: '' }
  try {
    const res = await getMyMemoryDrill(kind)
    const d = res.data
    memDrill.value = { kind, label: d.label || label, loading: false, error: '', items: d.items || [], total: d.total || 0, explanation: d.explanation || '' }
  } catch {
    memDrill.value = { kind, label, loading: false, error: '加载记忆明细失败', items: [], total: 0, explanation: '' }
  }
}

function closeMemoryDrill() {
  memDrill.value = null
}

// ---- 记忆/上下文治理钻取（Compaction 压缩链 / 静态动态写路径，仅本人数据）----
const govDrill = ref(null)

function memActionLabel(a) {
  return { add: '新增', update: '更新', delete: '删除', merge: '簇合并', forget: '超限遗忘', extract: '抽取运行', dream: 'AutoDream 整理' }[a] || a || '-'
}

// 抽取跑了但零写入：写路径事件为 0 而运行数 > 0，是「记忆面板永远为空」的直接信号
const extractZeroYield = computed(() => {
  const mc = monitor.value?.memory_context
  return !!mc && (mc.extract_runs?.total ?? 0) > 0 && (mc.write_events_total ?? 0) === 0
})

function compactKindLabel(k) {
  return {
    session_memory: 'L2·Session Memory', auto_compact: 'L2·Auto 摘要',
    auto_compact_hit: 'L2·Auto 命中', full: 'L3·兜底压缩', full_fallback: 'L3·兜底降级',
    manual: '手动压缩', note: '会话笔记',
  }[k] || k || '-'
}

async function openGovDrill(kind) {
  const label = kind === 'compact' ? '四层压缩链明细' : '记忆写路径明细'
  govDrill.value = { kind, label, loading: true, error: '', events: [], summaries: [], explanation: '' }
  try {
    const res = kind === 'compact' ? await getMyCompactDrill(50) : await getMyMemoryEvents(50)
    const d = res.data
    govDrill.value = {
      kind, label, loading: false, error: '',
      events: d.events || [], summaries: d.summaries || [], explanation: d.explanation || '',
    }
  } catch (e) {
    govDrill.value = { kind, label, loading: false, error: e.response?.data?.detail || '加载治理明细失败', events: [], summaries: [], explanation: '' }
  }
}

// ---- 对话维度统一视图（需求2：上下文构成 × 请求追踪 × 调用链 合并）----
function onSessionChange() {
  sessDay.value = ''
  sessTab.value = 'context'
  loadSessObs()
}

function selectDay(d) {
  sessDay.value = d
  loadSessObs()
}

function selectSessTrace(traceId) {
  loadSessObs(traceId)
}

async function loadSessObs(traceId = '') {
  if (!selSessionId.value) { sessObs.value = null; return }
  const requestId = ++sessRequestId
  sessLoading.value = true
  sessError.value = ''
  try {
    const res = await getSessionObservability(selSessionId.value, {
      day: sessDay.value,
      trace_id: traceId || sessObs.value?.selected_trace_id || '',
    })
    if (requestId !== sessRequestId) return
    sessObs.value = res.data
  } catch (err) {
    if (requestId === sessRequestId) {
      sessError.value = err.response?.data?.detail || err.message || '对话可观测数据加载失败'
      sessObs.value = null
    }
  } finally {
    if (requestId === sessRequestId) sessLoading.value = false
  }
}

// 拦截环节编码 → 中文（对应 hooks.py 钩子链）
function blockedCodeLabel(code, status) {
  if (status === 'error' && !code) return '执行异常'
  return {
    schema_invalid: 'Schema 校验',
    permission_denied: '权限闸',
    risk_blocked: '风险确认',
    rate_limited: '限流闸',
    tool_disabled: '状态闸',
    unknown_tool: '未知工具',
  }[code] || code || status || '-'
}

// 风险等级 → 中文 + 状态点颜色
function riskLabel(risk) {
  return { low: '低', medium: '中', high: '高', critical: '极高' }[risk] || risk || '-'
}
function riskDotClass(risk) {
  return { low: 'ok', medium: 'ok', high: 'running', critical: 'error' }[risk] || 'ok'
}

// ---- 指标钻取：数字点击 → 明细弹窗 ----
const DRILL_PAGE_SIZE = 20 // 需求2：单页不呈现过多数据
const drill = ref(null) // { type, label, columns, rows, total, page, pageSize, explanation, loading, error }

async function openDrill(type, label, page = 1) {
  drill.value = { type, label, columns: [], rows: [], total: 0, page, pageSize: DRILL_PAGE_SIZE, explanation: '', loading: true, error: '' }
  try {
    // 需求2/3：钻取明细带上当前时间窗（今日/近N天）+ 服务端分页，与 DashBoard 卡片同口径；
    // 传入指标 label（metric）使口径说明按具体指标给出并代入真实数值
    const res = await getObsDrill(type, 200, days.value, rangeMode.value, page, DRILL_PAGE_SIZE, label)
    drill.value = {
      type, label, columns: res.data.columns, rows: res.data.rows, total: res.data.total,
      page: res.data.page || page, pageSize: res.data.page_size || DRILL_PAGE_SIZE,
      explanation: res.data.explanation || '', loading: false, error: '',
    }
  } catch (err) {
    drill.value = { type, label, columns: [], rows: [], total: 0, page, pageSize: DRILL_PAGE_SIZE, explanation: '', loading: false, error: err.response?.data?.detail || err.message || '加载明细失败' }
  }
}

// 需求2：明细表格分页切换
const drillTotalPages = computed(() => {
  if (!drill.value || !drill.value.pageSize) return 1
  return Math.max(1, Math.ceil((drill.value.total || 0) / drill.value.pageSize))
})
function goDrillPage(dir) {
  if (!drill.value) return
  const next = Math.min(drillTotalPages.value, Math.max(1, drill.value.page + dir))
  if (next !== drill.value.page) openDrill(drill.value.type, drill.value.label, next)
}

async function loadTraces() {
  if (!canOps.value) return
  loadingTraces.value = true
  try {
    const params = {
      agent: filterAgent.value,
      status: filterStatus.value,
      limit: 30,
      offset: offset.value,
    }
    const data = await queryCache.cachedGet('usage', { kind: 'traces', ...params },
      async () => (await getObservabilityTraces(params)).data,
      { ttl: TTL_MONITOR, onRevalidate: (d) => { traces.value = d.items || []; total.value = d.total || 0 } })
    traces.value = data.items || []
    total.value = data.total || 0
  } finally {
    loadingTraces.value = false
  }
}

function changeRange(mode, d) {
  if (loading.value) return
  rangeMode.value = mode
  if (mode === 'days' && d) days.value = d
  loadAll(1)
}

function page(dir) {
  offset.value = Math.max(0, offset.value + dir * 30)
  loadTraces()
}

async function openTrace(id) {
  if (!canOps.value) return
  const requestId = ++traceRequestId
  resetTraceLogs()
  errorMsg.value = ''
  try {
    const res = await getObservabilityTrace(id)
    if (requestId !== traceRequestId) return
    if (!res.data.success) throw new Error(res.data.detail || 'trace 不存在')
    traceDetail.value = res.data.trace
    expandedSpans.value = new Set()
  } catch (err) {
    if (requestId === traceRequestId) errorMsg.value = err.response?.data?.detail || err.message || '加载 trace 详情失败'
  }
}

function resetTraceLogs() {
  ++logsRequestId
  showTraceLogs.value = false
  traceLogs.value = []
  logsLoading.value = false
  logsLoaded.value = false
  logsCursor.value = null
  logsError.value = ''
  logWarnings.value = []
}

function closeTrace() {
  ++traceRequestId
  traceDetail.value = null
  resetTraceLogs()
}

function toggleTraceLogs() {
  showTraceLogs.value = !showTraceLogs.value
  if (showTraceLogs.value && !logsLoaded.value) loadTraceLogs()
}

async function loadTraceLogs(append = false) {
  if (!canOps.value || !traceDetail.value || logsLoading.value) return
  const id = traceDetail.value.id
  const requestId = ++logsRequestId
  const cursor = append ? logsCursor.value : ''
  logsLoading.value = true
  logsError.value = ''
  if (!append) {
    traceLogs.value = []
    logsCursor.value = null
    logWarnings.value = []
  }
  try {
    const res = await getObservabilityTraceLogs(id, { cursor: cursor || '', limit: 100 })
    if (requestId !== logsRequestId || traceDetail.value?.id !== id) return
    if (!res.data.success) throw new Error(res.data.detail || '日志读取失败')
    traceLogs.value = append ? [...traceLogs.value, ...res.data.entries] : res.data.entries
    logsCursor.value = res.data.next_cursor
    logWarnings.value = [...new Set([...logWarnings.value, ...(res.data.warnings || [])])]
    logsLoaded.value = true
  } catch (err) {
    if (requestId === logsRequestId) logsError.value = err.response?.data?.detail || err.message || '日志读取失败'
  } finally {
    if (requestId === logsRequestId) logsLoading.value = false
  }
}

function toggleSpan(id) {
  const next = new Set(expandedSpans.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedSpans.value = next
}

// 图4：Trace 详情的最终 AI 回复——只认真实最终回复 span（name="answer"，含直调模板答案）；
// 不再倒序取任意 llm span（意图改写/证据校验等中间输出曾被误当「AI回复内容」）
const traceFinalAnswer = computed(() => {
  const spans = traceDetail.value?.spans || []
  for (let i = spans.length - 1; i >= 0; i--) {
    const s = spans[i]
    if (s.kind === 'llm' && s.name === 'answer' && s.output) return s.output
  }
  return ''
})

async function copyTraceAnswer() {
  try {
    await navigator.clipboard.writeText(traceFinalAnswer.value)
  } catch {
    /* 剪贴板不可用时静默降级 */
  }
}

// 图4：每步耗时条宽度（相对整条 trace 延迟）
function spanBarWidth(span) {
  const total = traceDetail.value?.latency_ms || 0
  if (!total || !span.latency_ms) return '2%'
  return Math.max(2, Math.min(100, (span.latency_ms / total) * 100)) + '%'
}

// 需求8：每个 span 都展示 Token —— LLM 为真实 usage（入+出，缓存命中另注），
// TOOL/STEP 为估算口径（带 ≈），压缩动作 span 展示「压缩前→压缩后」。
// 口径与文案统一收敛在 utils/contextTokens（与上下文用量徽章同源，便于单测）。
const traceTokenSummary = computed(() => traceTokenSummaryOf(traceDetail.value?.spans))

function completionPercent(value) {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

function responseSeconds(value) {
  return value == null ? '—' : `${Number(value).toFixed(1)} 秒`
}

function businessBarHeight(count) {
  const max = Math.max(1, ...(businessMetrics.value?.daily || []).map((row) => row.total))
  return count ? Math.max(6, Math.round(count / max * 100)) : 0
}

// ---- 展示辅助 ----
function formatMs(ms) {
  const n = Number(ms || 0)
  if (n < 1000) return `${n}ms`
  return `${(n / 1000).toFixed(1)}s`
}

function formatNum(n) {
  const v = Number(n || 0)
  if (v >= 10000) return `${(v / 1000).toFixed(1)}k`
  return String(v)
}

// 记忆类型条形宽度：相对该用户各类型中的最大条数归一化
function memTypeWidth(count) {
  const types = myContext.value?.memory?.by_type || []
  const max = Math.max(1, ...types.map((t) => Number(t.count || 0)))
  return ((Number(count || 0) / max) * 100).toFixed(1) + '%'
}

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (x) => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function statusText(status) {
  return { ok: '成功', error: '失败', running: '进行中', cancelled: '已取消', timeout: '超时', interrupted: '重启中断', skipped: '已跳过', blocked: '被阻止', partial: '部分完成', unverified: '未验证', hitl: '待用户确认' }[status] || status
}

function barHeight(count) {
  const max = Math.max(1, ...(metrics.value?.daily || []).map((r) => r.total))
  if (!count) return 3
  return Math.max(6, Math.round((count / max) * 100))
}

// ---- 图表 hover tooltip（鼠标悬停即时显示对应数字）----
const tooltip = ref({ show: false, x: 0, y: 0, lines: [] })

function showTip(event, lines) {
  tooltip.value = { show: true, x: event.clientX, y: event.clientY, lines }
}

function moveTip(event) {
  if (!tooltip.value.show) return
  tooltip.value.x = event.clientX
  tooltip.value.y = event.clientY
}

function hideTip() {
  tooltip.value.show = false
}

function dayTipLines(row) {
  return [
    row.day,
    `请求 ${row.total} · 失败 ${row.errors}`,
    `Token 总量 ${formatNum(row.tokens)}`,
    `平均 ${row.avg_tokens ?? 0} token/请求`,
    `平均延迟 ${formatMs(row.avg_latency_ms)}`,
  ]
}
</script>

<style scoped>
/* 四大模块页签与方面标题（DashBoard / 日志记录 / 指标监控 / 链路追踪） */
.ops-tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 0 0 14px;
  padding: 4px;
  background: var(--color-bg-soft, rgba(0, 0, 0, 0.03));
  border: 1px solid var(--color-border, #e5ddd0);
  border-radius: 10px;
  width: fit-content;
}
.ops-tab {
  border: none;
  background: transparent;
  color: var(--color-text-soft, #6b5d4f);
  font-size: 13px;
  font-weight: 500;
  padding: 7px 16px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.ops-tab:hover { background: rgba(0, 0, 0, 0.04); }
.ops-tab.active {
  background: var(--color-text, #3d3428);
  color: #fff;
}
.trace-subtabs {
  display: flex;
  gap: 4px;
  margin: 0 0 14px;
  border-bottom: 1px solid var(--color-border, #e5ddd0);
  padding-bottom: 0;
}
.trace-subtab {
  border: none;
  background: transparent;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-soft, #6b5d4f);
  padding: 8px 16px;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  transition: color 0.15s;
}
.trace-subtab:hover { color: var(--color-accent, #c98a4b); }
.trace-subtab.active {
  color: var(--color-accent, #c98a4b);
  border-bottom-color: var(--color-accent, #c98a4b);
}
.aspect-title {
  margin: 18px 0 10px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--color-text-soft, #6b5d4f);
  border-left: 3px solid var(--color-accent, #c98a4b);
  padding-left: 8px;
}
.obs-view {
  /* 父容器 .main-content 为 100vh + overflow:hidden，页面需自身承担滚动 */
  height: 100%;
  overflow-y: auto;
  background: var(--color-bg);
}

.obs-header {
  background: linear-gradient(135deg, #faf8f5, #f3efe9);
  border-bottom: 1px solid var(--color-border);
  padding: 28px 32px 22px;
}

.header-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.header-eyebrow {
  font-size: 11px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: var(--color-primary);
  font-weight: 700;
}

.header-title h1 {
  margin: 2px 0 6px;
  font-size: 24px;
  color: var(--color-text);
}

.header-sub {
  margin: 0;
  font-size: 13px;
  color: var(--color-text-secondary);
  max-width: 640px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.days-switch {
  display: flex;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  overflow: hidden;
}

.day-btn {
  padding: 6px 12px;
  font-size: 12px;
  border: none;
  background: #fff;
  color: var(--color-text-secondary);
  cursor: pointer;
}

.day-btn.active {
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-weight: 600;
}

.obs-body {
  max-width: 1200px;
  margin: 0 auto;
  padding: 20px 32px 48px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.cards.business-cards { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.business-summary { margin: 0; line-height: 1.8; }
.business-note { margin: 0; font-size: 12px; line-height: 1.8; color: var(--color-text-secondary); }
.business-breakdown { overflow-x: auto; }
.business-trend { overflow-x: auto; }
.business-trend .trend-col { min-width: 26px; }

/* ---- 指标卡片 ---- */
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(128px, 1fr));
  gap: 10px;
}

.metric-card {
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* 需求4：可点击钻取明细的指标卡（视觉反馈） */
.metric-card.drillable {
  cursor: pointer;
  transition: box-shadow 0.15s ease, border-color 0.15s ease, transform 0.1s ease;
}
.metric-card.drillable:hover {
  border-color: var(--color-primary);
  box-shadow: 0 4px 14px rgba(138, 90, 43, 0.14);
  transform: translateY(-1px);
}
.metric-card.drillable:active {
  transform: translateY(0);
}

.metric-num {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-primary);
}

.metric-num.warn {
  color: #c62828;
}

.metric-label {
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* ---- 面板 ---- */
.panel {
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 14px 16px 16px;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.panel-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
}

.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

@media (max-width: 900px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}

/* ---- 趋势图 ---- */
.trend-chart {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: 150px;
  padding: 8px 4px 0;
}

.trend-col {
  flex: 1;
  min-width: 14px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  height: 100%;
}

.trend-bar-wrap {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}

.trend-bar {
  width: 60%;
  max-width: 26px;
  background: linear-gradient(180deg, #d9a76a, #c98a4b);
  border-radius: 4px 4px 0 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  transition: height 0.3s;
}

.trend-bar-err {
  width: 100%;
  background: #c62828;
}

/* ---- Token 用量分析已迁移至模型网关（原位留跳转链接） ---- */
.token-moved {
  margin: 0 0 18px;
  font-size: 12.5px;
  color: var(--color-text-secondary);
  background: #faf6f0;
  border: 1px dashed var(--color-border);
  border-radius: 10px;
  padding: 12px 16px;
  line-height: 1.7;
}

.token-moved-link {
  color: var(--color-primary);
  font-weight: 700;
  text-decoration: none;
}

.token-moved-link:hover { text-decoration: underline; }

/* ---- 图表 hover tooltip ---- */
.chart-tooltip {
  position: fixed;
  z-index: 200;
  pointer-events: none;
  background: rgba(43, 36, 28, 0.94);
  color: #fff;
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 11px;
  line-height: 1.6;
  max-width: 280px;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.25);
}

.tip-line {
  white-space: nowrap;
}

.tip-title {
  font-weight: 700;
  margin-bottom: 2px;
  color: #f0d9b8;
}

.trend-label {
  font-size: 10px;
  color: var(--color-text-secondary);
}

/* ---- 表格 ---- */
.obs-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.obs-table th,
.obs-table td {
  padding: 7px 8px;
  text-align: left;
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text);
  white-space: nowrap;
}

.obs-table th {
  color: var(--color-text-secondary);
  font-weight: 600;
}

.trace-table .trace-row {
  cursor: pointer;
  transition: background 0.15s;
}

.trace-table .trace-row:hover {
  background: var(--color-primary-light);
}

.cell-input {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---- 指标钻取明细弹窗 ---- */
.drill-box { width: min(860px, 94vw); }
.drill-scroll { max-height: 58vh; overflow: auto; border: 1px solid var(--color-border); border-radius: 10px; }
.drill-table { min-width: 640px; }
.drill-table td { white-space: normal; word-break: break-word; max-width: 260px; overflow: hidden; text-overflow: ellipsis; }

/* 需求2：明细分页 + 口径说明 */
.drill-pager {
  display: flex; align-items: center; justify-content: center; gap: 12px;
  margin-top: 10px;
}
.drill-pager .pager-info { font-size: 12px; color: var(--color-text-secondary); }
.drill-explain {
  margin-top: 10px; padding: 10px 12px;
  background: rgba(138, 90, 43, 0.06); border: 1px dashed var(--color-border);
  border-radius: 8px;
}
.drill-explain-title { font-size: 12px; font-weight: 600; color: var(--color-primary); }
.drill-explain p { margin: 4px 0 0; font-size: 12px; line-height: 1.6; color: var(--color-text-secondary); }

.cell-time {
  color: var(--color-text-secondary);
}

.cell-err {
  color: #c62828;
  font-weight: 600;
}

.agent-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11px;
  background: #ede6dc;
  color: #6b5b45;
}

.agent-badge.agent-chat {
  background: #e3f0e6;
  color: #2e6b3e;
}

.agent-badge.agent-wiki_assistant {
  background: #e8ecf7;
  color: #3b4f8f;
}

.agent-badge.agent-wiki_compile,
.agent-badge.agent-wiki_synthesize {
  background: #f7ece0;
  color: #96602a;
}

.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  background: #9e9e9e;
}

.status-dot.cancelled, .status-dot.interrupted { background: #637080; }
.status-dot.timeout { background: #c28725; }

/* Skill 安全预审面板 */
.review-cards { margin-bottom: 14px; }
.row-reject td { background: #fdf3ef; }
.row-reject .cell-input { color: #b4553d; }
.cell-warn { color: #c62828; font-weight: 600; }
.status-dot.ok {
  background: #43a047;
}

.status-dot.error {
  background: #c62828;
}

.status-dot.running {
  background: #f9a825;
}

.filter-select {
  padding: 5px 8px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: #fff;
  color: var(--color-text);
  outline: none;
}

.trace-filters {
  display: flex;
  gap: 8px;
}

.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 12px;
}

.pager-info {
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* ---- 通用小组件 ---- */
.btn {
  padding: 7px 14px;
  font-size: 13px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid transparent;
}

.btn.sm {
  padding: 4px 10px;
  font-size: 12px;
}

.btn-ghost {
  background: #fff;
  border-color: var(--color-border);
  color: var(--color-text);
}

.btn-ghost:hover:not(:disabled) {
  background: var(--color-primary-light);
}

.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.btn-primary { background: var(--color-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { background: #b87a3f; }

.icon-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  color: var(--color-text-secondary);
}

.icon-btn:hover {
  background: #ede6dc;
}

.mini-loading,
.empty-hint {
  padding: 18px 0;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.inline-error {
  color: #c62828;
  font-size: 12px;
  margin: 0 0 8px;
}

/* ---- Trace 详情弹窗 ---- */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal-box {
  background: var(--color-bg, #fff);
  border-radius: 14px;
  padding: 20px 24px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}

.trace-box {
  width: 860px;
  max-width: 94vw;
  max-height: 88vh;
  overflow-y: auto;
}

.trace-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.trace-head .modal-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}

.trace-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin: 10px 0 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.trace-meta b {
  color: var(--color-text);
}

.trace-input {
  background: #faf8f5;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 8px 10px;
  margin-bottom: 12px;
}

.trace-answer {
  background: #fff;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 8px 10px;
  margin-bottom: 12px;
}
.trace-answer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}
.answer-text { max-height: 180px; overflow-y: auto; }

.trace-log-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
  font-size: 12px;
}

.trace-id, .log-notice {
  font-size: 12px;
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}

.log-notice {
  line-height: 1.6;
  margin: 0 0 10px;
}

.log-entries {
  max-height: 50vh;
  overflow-y: auto;
  margin-bottom: 12px;
}

.log-entry .trace-log-text {
  max-height: none;
  overflow-y: visible;
}

.span-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.span-item {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  overflow: hidden;
}

.span-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: none;
  background: #faf8f5;
  cursor: pointer;
  font-size: 12px;
  color: var(--color-text);
}

.span-head:hover {
  background: var(--color-primary-light);
}

.span-seq {
  color: var(--color-text-secondary);
  font-weight: 600;
}

.span-kind-badge {
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
}

.span-kind-badge.llm {
  background: #e8ecf7;
  color: #3b4f8f;
}

.span-kind-badge.tool {
  background: #e3f0e6;
  color: #2e6b3e;
}

.span-kind-badge.step {
  background: #ede6dc;
  color: #6b5b45;
}

.span-name {
  font-weight: 600;
  flex: 1;
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.span-meta {
  color: var(--color-text-secondary);
  white-space: nowrap;
}

/* 图4：每步耗时条（宽度按占整条 trace 延迟比例） */
.span-bar {
  flex: 1;
  min-width: 40px;
  height: 6px;
  background: rgba(0, 0, 0, 0.06);
  border-radius: 3px;
  overflow: hidden;
}
.span-bar i { display: block; height: 100%; border-radius: 3px; background: #9ca3af; }
.span-bar i.llm { background: #3b82f6; }
.span-bar i.tool { background: #8b5cf6; }
.span-bar i.step { background: #9ca3af; }

.span-arrow {
  display: flex;
  color: var(--color-text-secondary);
  transition: transform 0.2s;
}

.span-arrow.open {
  transform: rotate(180deg);
}

.span-body {
  padding: 10px 12px;
  border-top: 1px solid var(--color-border);
  background: #fff;
}

.span-io-label {
  display: block;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--color-text-secondary);
  margin: 6px 0 4px;
}

/* span 的压缩标记与窗口水位（落盘路径 / 去重 / 清除 / 原始大小） */
.span-ctx-meta {
  margin: 4px 0;
  font-size: 11.5px;
  color: var(--color-text-secondary);
  background: #f4f1ec;
  border-left: 2px solid #c98a4b;
  border-radius: 0 6px 6px 0;
  padding: 4px 8px;
  word-break: break-all;
}

.span-text {
  margin: 0;
  padding: 8px 10px;
  background: #faf8f5;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 280px;
  overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

/* ---- 当前用户：上下文窗口 + 记忆概览 ---- */
.my-context-panel { margin-top: 16px; }
.ctx-cards, .mem-cards { margin: 4px 0 8px; }
/* 治理指标 chips（压缩链事件分布 / 静态动态存储概览）与钻取入口 */
.chain-chips, .layer-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 4px 0 8px;
}
.chain-chip, .layer-chip {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--color-border, rgba(0, 0, 0, 0.12));
  color: var(--color-text-secondary);
  background: var(--color-bg-soft, rgba(0, 0, 0, 0.02));
}
.chain-chip.warn {
  color: var(--color-danger, #b3402e);
  border-color: var(--color-danger, #b3402e);
}
.layer-chip { border-style: dashed; background: transparent; }
.layer-chip.on {
  border-style: solid;
  color: var(--color-text);
  background: var(--color-bg-soft, rgba(0, 0, 0, 0.03));
}
.gov-actions { display: flex; gap: 8px; margin: 0 0 10px; }
.ctx-hint {
  margin: 2px 0 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--color-text-secondary);
}
.ctx-sub-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--color-text);
  margin: 14px 0 8px;
  padding-left: 8px;
  border-left: 3px solid var(--color-primary);
}
.share-cell { min-width: 130px; }
.share-bar {
  display: inline-block;
  vertical-align: middle;
  width: 70px;
  height: 6px;
  margin-right: 8px;
  background: #f0ebe3;
  border-radius: 3px;
  overflow: hidden;
}
.share-bar span { display: block; height: 100%; background: var(--color-primary); border-radius: 3px; }
.mem-types { display: flex; flex-direction: column; gap: 6px; }
.mem-type-row { display: flex; align-items: center; gap: 10px; }
.mem-type-row.drillable { cursor: pointer; border-radius: 6px; padding: 2px 4px; transition: background .15s; }
.mem-type-row.drillable:hover { background: rgba(138, 90, 43, 0.08); }
/* 需求1：记忆钻取弹窗 */
.mem-drill-box { width: min(880px, 94vw); max-height: 88vh; overflow-y: auto; }
.mem-drill-count { font-size: 12px; color: var(--color-text-secondary); margin: 4px 0 10px; }
.mem-drill-table td.cell-input { max-width: 260px; }
.mem-type-label { width: 40px; font-size: 12px; color: var(--color-text-secondary); }
.mem-type-bar {
  flex: 1;
  height: 8px;
  background: #f0ebe3;
  border-radius: 4px;
  overflow: hidden;
}
.mem-type-bar span { display: block; height: 100%; background: var(--color-primary); border-radius: 4px; }
.mem-type-count { width: 32px; text-align: right; font-size: 12px; font-weight: 600; color: var(--color-text); }

/* 按对话窗口下钻列表 */
.sess-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
}
.sess-item {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  overflow: hidden;
}
.sess-head {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  border: none;
  background: #fdfbf7;
  cursor: pointer;
  padding: 9px 12px;
  font-size: 13px;
  color: var(--color-text);
  text-align: left;
}
.sess-head:hover { background: var(--color-primary-light); }
.sess-caret { color: var(--color-primary); font-size: 11px; flex: none; }
.sess-title {
  flex: 1;
  min-width: 0;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sess-meta { font-size: 12px; color: var(--color-text-secondary); flex: none; }
.sess-time { font-size: 12px; color: var(--color-text-secondary); flex: none; font-variant-numeric: tabular-nums; }
.sess-seg-table { margin: 0; border: none; border-top: 1px dashed var(--color-border); }

/* ---- 需求4：黄金信号卡片 ---- */
.golden-cards { grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }

/* ---- 需求6：技能沉淀建议 ---- */
.sediment-item {
  border: 1px solid #eed9bb;
  border-radius: 10px;
  background: #fffdf9;
  padding: 10px 12px;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.sediment-body { flex: 1; min-width: 0; }
.sediment-msg { margin: 0 0 4px; font-size: 13px; font-weight: 600; color: #7c5a2e; }
.sediment-meta { margin: 0 0 3px; font-size: 12px; color: var(--color-text-secondary); }
.sediment-meta code { background: #f7ecd9; border-radius: 4px; padding: 1px 5px; }
.sediment-sols { margin: 0; font-size: 12px; color: var(--color-text-secondary); line-height: 1.6; }
/* 需求1：侧边「同意沉淀」按钮 */
.sediment-btn {
  flex: none;
  border: 1px solid #d9a86c;
  background: #f6e3c5;
  color: #7c4a12;
  border-radius: 8px;
  padding: 7px 14px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s ease;
}
.sediment-btn:hover { background: #eecf9f; }
/* 需求1：沉淀编辑弹窗 */
.sediment-box { width: min(640px, 94vw); max-height: 88vh; overflow-y: auto; }
.sediment-dialog-hint { margin: 4px 0 12px; font-size: 12px; color: var(--color-text-secondary); }
.sediment-dialog-hint code { background: #f7ecd9; border-radius: 4px; padding: 1px 5px; }
.sediment-form { display: flex; flex-direction: column; gap: 12px; }
.form-row { display: flex; flex-direction: column; gap: 5px; }
.form-label { font-size: 12px; font-weight: 600; color: var(--color-text-secondary); }
.form-input, .form-textarea {
  border: 1px solid #e2d3ba;
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 13px;
  font-family: inherit;
  color: var(--color-text);
  background: #fffdf9;
  width: 100%;
  box-sizing: border-box;
}
.form-textarea { resize: vertical; line-height: 1.6; }
.form-input:focus, .form-textarea:focus { outline: none; border-color: var(--color-primary); }
.radio-group { display: flex; gap: 18px; flex-wrap: wrap; }
.radio { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--color-text); cursor: pointer; }
.checkbox-row { flex-direction: row; align-items: center; gap: 8px; font-size: 13px; color: var(--color-text); cursor: pointer; }
.sediment-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; }
/* 需求2：技能市场相似 Skill 搜寻面板 */
.market-panel { margin-top: 16px; border: 1px dashed #d8c6a8; border-radius: 10px; padding: 12px; background: #fdfaf4; }
.market-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.market-title { font-size: 13px; font-weight: 700; color: var(--color-text); }
.market-sources { font-size: 11px; color: var(--color-text-secondary); }
.market-searchrow { display: flex; gap: 8px; }
.market-searchrow .form-input { flex: 1; }
.market-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; margin-top: 10px; }
.market-card { border: 1px solid #e2d3ba; border-radius: 10px; background: #fff; padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; transition: box-shadow .15s, border-color .15s; }
.market-card:hover { box-shadow: 0 3px 10px rgba(0,0,0,.08); }
.market-card.applied { border-color: #4a7c59; box-shadow: 0 0 0 2px rgba(74,124,89,.15); }
.market-card-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.market-name { font-weight: 700; font-size: 13px; color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.market-src { flex: none; font-size: 10.5px; background: #f6f1e8; color: #8a6d3b; border-radius: 4px; padding: 1px 6px; }
.market-desc { font-size: 12px; color: var(--color-text-secondary); line-height: 1.55; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; min-height: 2.6em; }
.market-card-foot { display: flex; align-items: center; gap: 8px; margin-top: auto; }
.market-heat { font-size: 11px; color: var(--color-text-secondary); }
.market-link { font-size: 11.5px; color: var(--color-primary); text-decoration: underline; margin-left: auto; }
.market-card-foot .btn { flex: none; }
/* 轻量 toast */
.obs-toast {
  position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%);
  background: #4a7c59; color: #fff; padding: 9px 18px; border-radius: 20px;
  font-size: 13px; box-shadow: 0 4px 14px rgba(0,0,0,.18); z-index: 1200;
}
.obs-toast.error { background: #b4553d; }
.fade-enter-active, .fade-leave-active { transition: opacity .25s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* ---- 需求2：对话维度统一视图 ---- */
.sess-select-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.sess-select { max-width: 340px; }
.day-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.day-chip {
  padding: 3px 10px;
  font-size: 12px;
  color: #6b5a48;
  background: #fff;
  border: 1px solid #e0cdb2;
  border-radius: 999px;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.day-chip:hover { background: #fbf3e8; border-color: #c98a4b; }
.day-chip.active { color: #fff; background: var(--color-primary); border-color: var(--color-primary); }
.sess-obs-meta {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 8px;
}
.sess-tabs {
  display: flex;
  gap: 6px;
  border-bottom: 1px solid var(--color-border);
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.sess-tab {
  padding: 6px 14px;
  font-size: 12.5px;
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
}
.sess-tab:hover { color: var(--color-primary); }
.sess-tab.active {
  color: var(--color-primary);
  font-weight: 600;
  border-bottom-color: var(--color-primary);
}
.row-selected > td { background: var(--color-primary-light); }
.sess-span-list { max-height: 420px; overflow-y: auto; }
</style>
