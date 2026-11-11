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
      </section>
      <!-- ============ 能力缺口沉淀建议（需求6：系统可进化性） ============ -->
      <section class="panel" v-if="sediment && sediment.suggestions && sediment.suggestions.length">
        <div class="panel-head">
          <span class="panel-title">技能沉淀建议（频繁能力缺口 → 进化为 Skill/工具）</span>
        </div>
        <div v-for="s in sediment.suggestions" :key="s.scenario" class="sediment-item">
          <div class="sediment-body">
            <p class="sediment-msg">💡 {{ s.message }}</p>
            <p class="sediment-meta">
              建议名称：<code>{{ s.suggested_name }}</code> · 类型：{{ s.suggested_kind === 'skill' ? 'Skill' : '工具' }}
              · 最近出现：{{ formatTime(s.last_at) }} · 示例问题：{{ s.sample_query || '-' }}
            </p>
            <p class="sediment-sols">候选方案：{{ (s.solutions || []).map((x) => x.label).join('；') }}</p>
          </div>
          <button class="sediment-btn" title="同意沉淀，编辑并创建为 Skill"
                  @click="openSedimentDialog(s)">同意沉淀</button>
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
        <div class="metric-card drillable" title="高危/极高事件，点击查看明细" @click="openDrill('sec_events', '高危/极高事件')">
          <span class="metric-num" :class="{ warn: monitor.security.high + monitor.security.critical }">{{ monitor.security.high + monitor.security.critical }}</span>
          <span class="metric-label">高危/极高事件</span>
        </div>
        <div class="metric-card drillable" title="工具约束拦截，点击查看拦截明细" @click="openDrill('tool_blocked', '工具约束拦截')">
          <span class="metric-num" :class="{ warn: monitor.security.tool_blocked }">{{ monitor.security.tool_blocked }}</span>
          <span class="metric-label">工具约束拦截</span>
        </div>
      </section>
      <div class="aspect-title">业务指标（资产存量 / 活跃用户 / 上下文与 Token 用量 / 记忆）</div>
      <section class="cards" v-if="monitor">
        <div class="metric-card drillable" title="会话总数，点击查看明细" @click="openDrill('sessions', '会话总数')"><span class="metric-num">{{ monitor.business.counts.sessions || 0 }}</span><span class="metric-label">会话总数</span></div>
        <div class="metric-card drillable" title="消息总数，点击查看明细" @click="openDrill('messages', '消息总数')"><span class="metric-num">{{ monitor.business.counts.messages || 0 }}</span><span class="metric-label">消息总数</span></div>
        <div class="metric-card drillable" title="活跃用户，点击查看明细" @click="openDrill('active_users', '活跃用户')"><span class="metric-num">{{ monitor.business.counts.active_users || 0 }}</span><span class="metric-label">活跃用户</span></div>
        <div class="metric-card drillable" title="上传记录，点击查看明细" @click="openDrill('uploads', '上传记录')"><span class="metric-num">{{ monitor.business.counts.uploads || 0 }}</span><span class="metric-label">上传记录</span></div>
        <div class="metric-card drillable" title="知识库，点击查看明细" @click="openDrill('kbs', '知识库')"><span class="metric-num">{{ monitor.business.counts.knowledge_bases || 0 }}</span><span class="metric-label">知识库</span></div>
        <div class="metric-card drillable" title="百科页面，点击查看明细" @click="openDrill('wiki', '百科页面')"><span class="metric-num">{{ monitor.business.counts.wiki_pages || 0 }}</span><span class="metric-label">百科页面</span></div>
        <div class="metric-card drillable" title="记忆条数，点击查看明细" @click="openDrill('memories', '记忆条数')"><span class="metric-num">{{ monitor.business.counts.memories || 0 }}</span><span class="metric-label">记忆条数</span></div>
      </section>

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

      <!-- ============ Token 用量分析 ============ -->
      <section class="panel" v-if="metrics">
        <div class="panel-head">
          <span class="panel-title">Token 用量分析</span>
          <span v-if="metrics.overview.token_overuse" class="token-warn-badge">用量偏高</span>
        </div>

        <!-- 历史基线对比：回答“使用量是否下降/优化” -->
        <div class="token-compare">
          <div class="compare-item">
            <span class="compare-num">{{ formatNum(metrics.token_comparison.current_avg) }}</span>
            <span class="compare-label">近期平均 Token/请求</span>
          </div>
          <div class="compare-arrow" :class="metrics.token_comparison.trend">
            <template v-if="metrics.token_comparison.delta_pct != null">
              <span class="delta">{{ metrics.token_comparison.trend === 'down' ? '▼' : (metrics.token_comparison.trend === 'up' ? '▲' : '—') }} {{ Math.abs(metrics.token_comparison.delta_pct) }}%</span>
              <span class="compare-hint">近期 vs 前期（本周期内）</span>
            </template>
            <span v-else class="compare-hint">数据不足（需≥2次）</span>
          </div>
          <div class="compare-item">
            <span class="compare-num">{{ formatNum(metrics.token_comparison.previous_avg) }}</span>
            <span class="compare-label">前期平均 Token/请求</span>
          </div>
        </div>
        <p class="token-note down" v-if="metrics.token_comparison.trend === 'down'">✓ 平均用量较前期下降 {{ Math.abs(metrics.token_comparison.delta_pct) }}%，优化见效。</p>
        <p class="token-note up" v-else-if="metrics.token_comparison.trend === 'up'">↑ 平均用量较前期上升 {{ metrics.token_comparison.delta_pct }}%，建议关注下方各段构成定位增量来源。</p>
        <p class="token-note warn" v-if="metrics.overview.token_overuse">⚠ 单请求平均 Token（{{ formatNum(metrics.overview.avg_tokens_per_request) }}）已超过告警阈值 {{ formatNum(metrics.overview.token_warn_threshold) }}，可收紧历史窗口（SESSION_MAX_HISTORY）或附件上下文预算。</p>

        <!-- segment 级构成（需求3：全站累计 × 本人累计 合并为同一模块，按对话下钻见下方统一会话视图） -->
        <div class="segment-title">上下文构成 · 按语义段（全站累计 × 本人累计 合并视图）</div>
        <table v-if="mergedSegments.length" class="obs-table">
          <thead>
            <tr><th>上下文段</th><th>全站累计 Token</th><th>全站占比</th><th>本人累计</th><th>本人平均/次</th><th>本人峰值</th><th>较前期</th></tr>
          </thead>
          <tbody>
            <template v-for="seg in mergedSegments" :key="seg.segment">
              <tr class="seg-merge-row" @click="toggleSegment(seg.segment)">
                <td>
                  <span class="seg-caret" :class="{ open: expandedSegments.has(seg.segment) }">▸</span>
                  {{ seg.label }}
                </td>
                <td>{{ seg.global_tokens != null ? formatNum(seg.global_tokens) : '-' }}</td>
                <td class="share-cell">
                  <div v-if="seg.global_share != null" class="share-bar"><span :style="{ width: (seg.global_share * 100).toFixed(1) + '%' }"></span></div>
                  {{ seg.global_share != null ? (seg.global_share * 100).toFixed(1) + '%' : '-' }}
                </td>
                <td>{{ seg.my_tokens != null ? formatNum(seg.my_tokens) : '-' }}</td>
                <td>{{ seg.my_avg != null ? formatNum(seg.my_avg) : '-' }}</td>
                <td>{{ seg.my_peak != null ? formatNum(seg.my_peak) : '-' }}</td>
                <td>
                  <span v-if="seg.delta_pct != null" class="seg-delta" :class="seg.trend"
                    :title="`本人近期 ${seg.recent_avg} vs 前期 ${seg.prev_avg}`">{{ seg.trend === 'down' ? '▼' : (seg.trend === 'up' ? '▲' : '—') }}{{ Math.abs(seg.delta_pct) }}%</span>
                  <span v-else class="seg-delta flat">—</span>
                </td>
              </tr>
              <!-- 需求4：详情展开在被点击行的正下方（而非表格底部） -->
              <tr v-if="expandedSegments.has(seg.segment)" class="seg-drill-row">
                <td colspan="7">
                  <div class="segment-items">
                    <p class="seg-drill-title">「{{ seg.label }}」最近 {{ (seg.items || []).length }} 次会话（点击行查看该请求全链路追踪）</p>
                    <div
                      v-for="it in seg.items || []"
                      :key="seg.segment + it.trace_id"
                      class="segment-item"
                      :title="`点击查看该请求全链路追踪：${it.input}`"
                      @click="openTrace(it.trace_id)"
                    >
                      <span class="seg-item-time">{{ formatTime(it.started_at) }}</span>
                      <span class="seg-item-user">{{ it.username }}</span>
                      <span class="seg-item-input">{{ it.input || '-' }}</span>
                      <span class="seg-item-tok">{{ formatNum(it.tokens) }} tok</span>
                    </div>
                    <p v-if="!(seg.items || []).length" class="empty-hint">该段暂无会话明细</p>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <p v-else class="empty-hint">暂无 segment 度量数据（发起一次带知识检索的对话后即可看到）</p>

        <!-- 每日平均 token 趋势 -->
        <div class="segment-title">每日平均 Token/请求趋势</div>
        <div class="trend-chart" v-if="metrics.daily.length">
          <div v-for="row in metrics.daily" :key="'tk-' + row.day" class="trend-col"
            @mouseenter="showTip($event, tokenDayTipLines(row))"
            @mousemove="moveTip"
            @mouseleave="hideTip">
            <div class="trend-bar-wrap">
              <div class="trend-bar token-bar" :style="{ height: tokenBarHeight(row.avg_tokens) + '%' }"></div>
            </div>
            <span class="trend-label">{{ row.day.slice(5) }}</span>
          </div>
        </div>
      </section>

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

      <!-- ============ 标准化工具注册表（req1） ============ -->
      <section class="panel" v-if="toolRegistry && toolRegistry.tools && toolRegistry.tools.length">
        <div class="panel-head">
          <span class="panel-title">标准化工具注册表（命名空间 · 标签 · 场景 · 参数 Schema · 权限 · 风险 · 状态）</span>
        </div>
        <div class="registry-scroll">
        <table class="obs-table registry-table">
          <colgroup>
            <col style="width:150px" /><col style="width:100px" /><col style="width:186px" />
            <col style="width:248px" /><col style="width:104px" /><col style="width:82px" />
            <col style="width:64px" /><col style="width:206px" />
          </colgroup>
          <thead>
            <tr><th>工具</th><th>命名空间</th><th>标签</th><th>适用场景</th><th>权限</th><th>风险等级</th><th>状态</th><th>参数</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in toolRegistry.tools" :key="t.name">
              <td class="reg-name" :title="t.description">{{ t.name }}</td>
              <td><span class="reg-chip ns">{{ t.namespace }}</span></td>
              <td>
                <span class="reg-tags">
                  <em v-for="tag in (t.tags || []).slice(0, 4)" :key="tag" class="reg-chip">{{ tag }}</em>
                  <em v-if="(t.tags || []).length > 4" class="reg-chip more" :title="(t.tags || []).join('、')">+{{ (t.tags || []).length - 4 }}</em>
                  <em v-if="!(t.tags || []).length" class="reg-empty">-</em>
                </span>
              </td>
              <td><span class="reg-scene" :title="(t.scenarios || []).join('；')">{{ (t.scenarios || []).join('；') || '-' }}</span></td>
              <td><span class="reg-chip perm" :title="(t.permissions || []).join('、') || '公开'">{{ (t.permissions || []).join('、') || '公开' }}</span></td>
              <td :title="`风险等级：${riskLabel(t.risk_level)}`"><span class="status-dot" :class="riskDotClass(t.risk_level)"></span>{{ riskLabel(t.risk_level) }}</td>
              <td><span class="reg-status">{{ t.status === 'active' ? '可用' : t.status }}</span></td>
              <td>
                <span class="reg-tags" :title="schemaParamSummary(t.parameters)">
                  <em v-for="p in schemaParams(t.parameters).slice(0, 3)" :key="p" class="reg-chip param">{{ p }}</em>
                  <em v-if="schemaParams(t.parameters).length > 3" class="reg-chip more">+{{ schemaParams(t.parameters).length - 3 }}</em>
                  <em v-if="!schemaParams(t.parameters).length" class="reg-empty">无参数</em>
                </span>
              </td>
            </tr>
          </tbody>
        </table>
        </div>
        <p class="empty-hint">共 {{ toolRegistry.tools.length }} 个工具 · function-calling schema {{ (toolRegistry.function_schemas || []).length }} 份 · Schema Token 估算 {{ formatNum(toolRegistry.schema_tokens) }}</p>
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
            <span class="metric-num">{{ myContext.context_window.history_rounds }}</span>
            <span class="metric-label">历史窗口（轮）</span>
          </div>
          <div class="metric-card">
            <span class="metric-num">{{ myContext.compression.sessions_with_summary }}</span>
            <span class="metric-label">已压缩会话</span>
          </div>
        </div>
        <p class="ctx-hint">
          上下文告警水位 {{ formatNum(myContext.context_window.warn_threshold) }} Token/请求 ·
          记忆注入预算 {{ myContext.context_window.memory_ctx_chars_budget }} 字符 ·
          历史压缩触发 {{ formatNum(myContext.compression.trigger_tokens) }} Token，当前摘要占用 {{ formatNum(myContext.compression.summary_tokens) }} Token。
        </p>

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
        <p v-else class="empty-hint">还没有沉淀任何跨会话记忆。多轮对话后系统会自动抽取你的偏好与事实。</p>
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
                  <span class="span-meta">{{ formatMs(span.latency_ms) }}<template v-if="span.prompt_tokens || span.completion_tokens"> · {{ span.prompt_tokens }}+{{ span.completion_tokens }} tok</template></span>
                  <span class="span-arrow" :class="{ open: expandedSpans.has(span.id) }">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                      <polyline points="6 9 12 15 18 9" />
                    </svg>
                  </span>
                </button>
                <div v-if="expandedSpans.has(span.id)" class="span-body">
                  <p v-if="span.error" class="inline-error">{{ span.error }}</p>
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
          <p class="log-notice">按请求 ID 展示已落盘日志（含异常堆栈），常见凭据已脱敏。旧日志仅能关联带 trace ID 的记录；未记录、已清理或独立进程中未关联的日志无法补齐。</p>
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
              <span class="span-meta">{{ formatMs(span.latency_ms) }}<template v-if="span.prompt_tokens || span.completion_tokens"> · {{ span.prompt_tokens }}+{{ span.completion_tokens }} tok</template></span>
              <span class="span-arrow" :class="{ open: expandedSpans.has(span.id) }">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                  <polyline points="6 9 12 15 18 9" />
                </svg>
              </span>
            </button>
            <div v-if="expandedSpans.has(span.id)" class="span-body">
              <p v-if="span.error" class="inline-error">{{ span.error }}</p>
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

    <!-- ============ 需求1：同意沉淀 → 编辑并创建为 Skill 弹窗 ============ -->
    <div v-if="sedimentDialog" class="modal-overlay" @click.self="closeSedimentDialog">
      <div class="modal-box sediment-box" role="dialog" aria-label="沉淀为 Skill">
        <div class="trace-head">
          <h3 class="modal-title">沉淀为 Skill</h3>
          <button class="icon-btn" title="关闭" @click="closeSedimentDialog">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <p class="sediment-dialog-hint">来源场景：<code>{{ sedimentDialog.scenario }}</code> · 近 {{ sediment.days }} 天出现 {{ sedimentDialog.count }} 次</p>
        <div class="sediment-form">
          <label class="form-row">
            <span class="form-label">归属</span>
            <span class="radio-group">
              <label class="radio"><input type="radio" value="personal" v-model="sedimentDialog.visibility" /> 个人（仅自己可见可用）</label>
              <label class="radio"><input type="radio" value="public" v-model="sedimentDialog.visibility" /> 公共（全员可见可用）</label>
            </span>
          </label>
          <label class="form-row">
            <span class="form-label">技能名称</span>
            <input class="form-input" v-model="sedimentDialog.name" maxlength="60" placeholder="给这个沉淀技能取个名字" />
          </label>
          <label class="form-row">
            <span class="form-label">描述</span>
            <input class="form-input" v-model="sedimentDialog.description" maxlength="200" placeholder="一句话说明该技能用途" />
          </label>
          <label class="form-row">
            <span class="form-label">指令内容</span>
            <textarea class="form-textarea" v-model="sedimentDialog.content" rows="5"
                      placeholder="当遇到此类场景时，希望 Agent 如何处理（注入对话系统提示）"></textarea>
          </label>
          <label class="form-row">
            <span class="form-label">其他需求</span>
            <textarea class="form-textarea" v-model="sedimentDialog.extra" rows="2"
                      placeholder="可选：补充约束/语气/边界等，将合并到指令内容末尾"></textarea>
          </label>
          <label class="form-row checkbox-row">
            <input type="checkbox" v-model="sedimentDialog.enabled" /> 创建后立即启用
          </label>
        </div>

        <!-- 需求2：从主流技能市场搜寻类似 Skill，展示卡片并一键应用 -->
        <div class="market-panel">
          <div class="market-head">
            <span class="market-title">🛒 从技能市场寻找类似 Skill</span>
            <span class="market-sources">Skills.sh · ClawHub · SkillsMP 等</span>
          </div>
          <div class="market-searchrow">
            <input class="form-input" v-model="sedimentDialog.marketQuery" maxlength="120"
                   placeholder="搜索关键词，如 weather / 天气" @keyup.enter="runMarketSearch" />
            <button class="btn btn-outline sm" :disabled="sedimentDialog.marketLoading" @click="runMarketSearch">
              {{ sedimentDialog.marketLoading ? '搜索中…' : '搜索市场' }}
            </button>
          </div>
          <div v-if="sedimentDialog.marketLoading" class="mini-loading">正在从主流市场搜寻类似 Skill…</div>
          <div v-else-if="sedimentDialog.marketError" class="inline-error">{{ sedimentDialog.marketError }}</div>
          <div v-else-if="sedimentDialog.marketResults.length" class="market-grid">
            <div v-for="m in sedimentDialog.marketResults" :key="m.id" class="market-card"
                 :class="{ applied: sedimentDialog.appliedMarketId === m.id }">
              <div class="market-card-top">
                <span class="market-name" :title="m.name">{{ m.name }}</span>
                <span class="market-src">{{ m.source_name }}</span>
              </div>
              <p class="market-desc">{{ m.description || '（无描述，应用后将回源指令内容）' }}</p>
              <div class="market-card-foot">
                <span class="market-heat" v-if="m.heat" :title="'安装/下载量'">⬇ {{ formatNum(m.heat) }}</span>
                <a class="market-link" :href="m.url" target="_blank" rel="noopener">查看</a>
                <button class="btn btn-primary sm" @click="applyMarketSkill(m)">
                  {{ sedimentDialog.appliedMarketId === m.id ? '已应用 ✓' : '应用' }}
                </button>
              </div>
            </div>
          </div>
          <p v-else-if="sedimentDialog.marketSearched" class="empty-hint">未在主流市场找到类似 Skill，可直接编辑上方表单沉淀。</p>
        </div>

        <div v-if="sedimentDialog.error" class="inline-error">{{ sedimentDialog.error }}</div>
        <div class="sediment-actions">
          <button class="btn btn-ghost" :disabled="sedimentDialog.submitting" @click="closeSedimentDialog">取消</button>
          <button class="btn btn-primary" :disabled="sedimentDialog.submitting" @click="submitSediment">
            {{ sedimentDialog.submitting ? '提交中…' : '确认沉淀' }}
          </button>
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
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useAuthStore } from '@/stores/auth'
import ObsDashboard from '@/components/observability/ObsDashboard.vue'
import ObsLogs from '@/components/observability/ObsLogs.vue'
import { getMyObservabilityMetrics, getMyContextOverview, getMyContextSessions, getObservabilityMetrics, getObservabilityTraces, getObservabilityTrace, getObservabilityTraceLogs, getSkillReviews, getToolRoutingMetrics, getToolRegistry, getSessionObservability, getSedimentSuggestions, getObsMonitor, getObsToolChains, getObsDrill, createSkill, searchSkillMarket, getMyMemoryDrill } from '@/api'

const auth = useAuthStore()
const canOps = computed(() => auth.canOps)
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
const toolRegistry = ref(null)
const sediment = ref(null)
// 需求1：同意沉淀编辑弹窗（公共/个人归属 + 名称/描述/指令内容/其他需求）
const sedimentDialog = ref(null)
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
// 四大模块页签（需求：DashBoard / 日志记录 / 指标监控 / 链路追踪）
const opsTab = ref('dashboard')
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

onMounted(() => loadAll(0))

async function loadAll(refresh = 0) {
  loading.value = true
  errorMsg.value = ''
  try {
    if (canOps.value) {
      await Promise.all([loadMetrics(refresh), loadTraces(), loadSkillReviews(), loadToolRouting(refresh), loadToolRegistry(), loadSediment(), loadMyContext(), loadMonitor(refresh), loadToolChains(refresh)])
    } else {
      businessMetrics.value = null
      const res = await getMyObservabilityMetrics(days.value, rangeMode.value)
      businessMetrics.value = res.data
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
  const res = await getObservabilityMetrics(days.value, rangeMode.value, refresh)
  metrics.value = res.data
}

async function loadSkillReviews() {
  if (!canOps.value) return
  try {
    const res = await getSkillReviews(50)
    skillReviews.value = res.data
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
    const res = await getToolRoutingMetrics(days.value, rangeMode.value, refresh)
    toolRouting.value = res.data
  } catch {
    toolRouting.value = null
  }
}

// 指标监控四方面聚合（质量/安全/业务）：加载失败不阻断其余面板
async function loadMonitor(refresh = 0) {
  if (!canOps.value) return
  try {
    const res = await getObsMonitor(days.value, rangeMode.value, refresh)
    monitor.value = res.data
  } catch {
    monitor.value = null
  }
}

// 按 trace 聚合的工具调用链：加载失败不阻断其余面板
async function loadToolChains(refresh = 0) {
  if (!canOps.value) return
  try {
    const res = await getObsToolChains(days.value, rangeMode.value, 50, refresh)
    toolChains.value = res.data.items || []
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
    const res = await getSedimentSuggestions(30, 3)
    sediment.value = res.data
  } catch {
    sediment.value = null
  }
}

// ---- 需求1：同意沉淀 → 编辑弹窗 → 创建为 Skill ----
function openSedimentDialog(s) {
  const sols = (s.solutions || []).map((x) => x.label).filter(Boolean)
  const label = s.label || s.scenario
  const defaultContent = `当识别到「${label}」类需求时，`
    + (sols.length ? `按以下方案处理：${sols.join('；')}。` : '主动说明当前能力边界并给出可行的替代建议。')
  sedimentDialog.value = {
    scenario: s.scenario,
    label,
    count: s.count,
    visibility: 'personal',
    name: String(s.suggested_name || ('skill_' + s.scenario)).slice(0, 60),
    description: String(s.message || '').slice(0, 200),
    content: defaultContent,
    extra: '',
    enabled: true,
    submitting: false,
    error: '',
    // 需求2：技能市场相似 Skill 搜寻
    marketQuery: String(s.scenario || '').replace(/_/g, ' ').trim() || label,
    marketLoading: false,
    marketResults: [],
    marketError: '',
    marketSearched: false,
    appliedMarketId: '',
  }
  // 打开即自动搜寻一次类似 Skill（best-effort，不阻塞编辑）
  runMarketSearch()
}

async function runMarketSearch() {
  const d = sedimentDialog.value
  if (!d || d.marketLoading) return
  const q = (d.marketQuery || '').trim()
  if (!q) { d.marketError = '请输入搜索关键词'; return }
  d.marketLoading = true
  d.marketError = ''
  try {
    const res = await searchSkillMarket(q, 6)
    d.marketResults = res.data.results || []
    d.marketSearched = true
  } catch {
    d.marketResults = []
    d.marketSearched = true
    d.marketError = '市场搜索失败，请稍后重试'
  } finally {
    d.marketLoading = false
  }
}

function applyMarketSkill(m) {
  const d = sedimentDialog.value
  if (!d) return
  d.name = String(m.name || d.name).slice(0, 60)
  if (m.description) d.description = String(m.description).slice(0, 200)
  const body = (m.content || '').trim()
  if (body) {
    d.content = body
  } else if (m.description) {
    d.content = `当识别到「${m.name}」类需求时，参考以下能力说明处理：${m.description}`
  }
  d.appliedMarketId = m.id
  showToast(`已应用市场 Skill「${m.name}」，可继续编辑后确认沉淀`)
}

function closeSedimentDialog() {
  if (sedimentDialog.value && sedimentDialog.value.submitting) return
  sedimentDialog.value = null
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

async function submitSediment() {
  const d = sedimentDialog.value
  if (!d || d.submitting) return
  const name = (d.name || '').trim()
  if (!name) { d.error = '技能名称必填'; return }
  let content = (d.content || '').trim()
  const extra = (d.extra || '').trim()
  if (extra) content = content ? `${content}\n\n补充要求：${extra}` : `补充要求：${extra}`
  if (!content) { d.error = '指令内容不能为空'; return }
  d.error = ''
  d.submitting = true
  try {
    await createSkill({
      name, kind: 'prompt', scope: 'chat', content,
      description: (d.description || '').trim(),
      enabled: !!d.enabled, visibility: d.visibility,
    })
    sedimentDialog.value = null
    showToast(d.visibility === 'public' ? '已沉淀为公共 Skill' : '已沉淀为个人 Skill')
  } catch (e) {
    d.error = e?.response?.data?.detail || e?.message || '沉淀失败'
  } finally {
    if (sedimentDialog.value) sedimentDialog.value.submitting = false
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

// 标准化工具注册表快照（req1）
async function loadToolRegistry() {
  if (!canOps.value) return
  try {
    const res = await getToolRegistry()
    toolRegistry.value = res.data
  } catch {
    toolRegistry.value = null
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

// 参数 Schema 摘要：列出属性名（必填加 *），供注册表「参数」列速览
function schemaParamSummary(parameters) {
  const props = parameters && parameters.properties
  if (!props || !Object.keys(props).length) return '无参数'
  const required = new Set((parameters && parameters.required) || [])
  return Object.keys(props).map((k) => (required.has(k) ? k + '*' : k)).join('、')
}

/** 注册表参数 chip 列表（必填带 *） */
function schemaParams(parameters) {
  const props = parameters && parameters.properties
  if (!props || !Object.keys(props).length) return []
  const required = new Set((parameters && parameters.required) || [])
  return Object.keys(props).map((k) => (required.has(k) ? k + '*' : k))
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
    const res = await getObservabilityTraces({
      agent: filterAgent.value,
      status: filterStatus.value,
      limit: 30,
      offset: offset.value,
    })
    traces.value = res.data.items || []
    total.value = res.data.total || 0
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

// 图4：Trace 详情的最终 AI 回复（取最后一个有输出的 llm span，回退任意有输出 span）
const traceFinalAnswer = computed(() => {
  const spans = traceDetail.value?.spans || []
  for (let i = spans.length - 1; i >= 0; i--) {
    if (spans[i].kind === 'llm' && spans[i].output) return spans[i].output
  }
  for (let i = spans.length - 1; i >= 0; i--) {
    if (spans[i].output) return spans[i].output
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
  return { ok: '成功', error: '失败', running: '进行中', cancelled: '已取消', timeout: '超时', interrupted: '重启中断' }[status] || status
}

function barHeight(count) {
  const max = Math.max(1, ...(metrics.value?.daily || []).map((r) => r.total))
  if (!count) return 3
  return Math.max(6, Math.round((count / max) * 100))
}

function tokenBarHeight(avg) {
  const max = Math.max(1, ...(metrics.value?.daily || []).map((r) => r.avg_tokens || 0))
  if (!avg) return 3
  return Math.max(6, Math.round((avg / max) * 100))
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

function tokenDayTipLines(row) {
  return [
    row.day,
    `平均 ${row.avg_tokens ?? 0} token/请求`,
    `Token 总量 ${formatNum(row.tokens)}`,
    `请求 ${row.total}`,
  ]
}

function segmentTipLines(seg) {
  const lines = [
    seg.label,
    `总 ${formatNum(seg.tokens)} token · 占比 ${(seg.share * 100).toFixed(1)}%`,
    `平均 ${seg.avg_tokens} token/次 · ${seg.count} 次`,
  ]
  if (seg.delta_pct != null) {
    lines.push(`近期 ${seg.recent_avg} vs 前期 ${seg.prev_avg}：${seg.delta_pct > 0 ? '+' : ''}${seg.delta_pct}%（${seg.trend === 'down' ? '下降=优化' : seg.trend === 'up' ? '上升' : '持平'}）`)
  } else {
    lines.push('数据不足（需≥2次）')
  }
  lines.push('点击可展开查看对应会话')
  return lines
}

// 需求3：全站 segment_breakdown（ops）× 本人 context_window.segments 合并为同一模块，
// 按 segment 键对齐；任一侧缺失时对应列显示 '-'。按对话下钻见统一会话视图。
const mergedSegments = computed(() => {
  const globalSegs = metrics.value?.segment_breakdown || []
  const mySegs = myContext.value?.context_window?.segments || []
  const myByKey = new Map(mySegs.map((s) => [s.segment, s]))
  const globalByKey = new Map(globalSegs.map((s) => [s.segment, s]))
  const keys = [...new Set([...globalSegs.map((s) => s.segment), ...mySegs.map((s) => s.segment)])]
  return keys.map((k) => {
    const g = globalByKey.get(k)
    const m = myByKey.get(k)
    return {
      segment: k,
      label: (g || m).label,
      global_tokens: g ? g.tokens : null,
      global_share: g ? g.share : null,
      my_tokens: m ? m.tokens : null,
      my_avg: m ? m.avg_tokens : null,
      my_peak: m ? m.peak_tokens : null,
      delta_pct: g && g.delta_pct != null ? g.delta_pct : null,
      trend: g ? g.trend : '',
      recent_avg: g ? g.recent_avg : null,
      prev_avg: g ? g.prev_avg : null,
      items: g ? g.items || [] : [],
    }
  }).sort((a, b) => (b.global_tokens || b.my_tokens || 0) - (a.global_tokens || a.my_tokens || 0))
})

// ---- segment drill-down：展开查看该段对应哪几次会话 ----
const expandedSegments = ref(new Set())

function toggleSegment(seg) {
  const next = new Set(expandedSegments.value)
  if (next.has(seg)) next.delete(seg)
  else next.add(seg)
  expandedSegments.value = next
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

/* ---- Token 用量分析 ---- */
.token-bar {
  background: linear-gradient(180deg, #7f9bd1, #4f6bab);
}

.token-warn-badge {
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 700;
  background: #fdecea;
  color: #c62828;
}

.token-compare {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  background: #faf8f5;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  margin-bottom: 10px;
}

.compare-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.compare-num {
  font-size: 20px;
  font-weight: 700;
  color: var(--color-text);
}

.compare-label {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.compare-arrow {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.compare-arrow .delta {
  font-size: 15px;
  font-weight: 700;
}

.compare-arrow.down .delta { color: #2e7d32; }
.compare-arrow.up .delta { color: #c62828; }
.compare-arrow.flat .delta { color: var(--color-text-secondary); }

.compare-hint {
  font-size: 10px;
  color: var(--color-text-secondary);
}

.token-note {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--color-text-secondary);
}

.token-note.down { color: #2e7d32; }
.token-note.up { color: #b26a00; }
.token-note.warn { color: #c62828; }

.segment-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--color-text);
  margin: 14px 0 8px;
}

.segment-bars {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.segment-group {
  display: flex;
  flex-direction: column;
}

.segment-row {
  width: 100%;
  display: grid;
  grid-template-columns: 88px 1fr 168px 18px;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
  background: none;
  border: none;
  cursor: pointer;
  text-align: left;
}

.segment-row:hover .segment-label {
  color: var(--color-primary);
}

.segment-label {
  font-size: 12px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.segment-bar-wrap {
  height: 14px;
  background: #f0ebe3;
  border-radius: 7px;
  overflow: hidden;
}

.segment-bar {
  height: 100%;
  background: linear-gradient(90deg, #d9a76a, #c98a4b);
  border-radius: 7px;
  min-width: 2px;
  transition: width 0.3s;
}

.segment-val {
  font-size: 11px;
  color: var(--color-text-secondary);
  text-align: right;
  white-space: nowrap;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
}

.seg-delta {
  font-weight: 700;
}

.seg-delta.down { color: #2e7d32; }
.seg-delta.up { color: #c62828; }
.seg-delta.flat { color: var(--color-text-secondary); }

.segment-arrow {
  color: var(--color-text-secondary);
  font-size: 10px;
  transition: transform 0.2s;
  text-align: center;
}

.segment-arrow.open {
  transform: rotate(180deg);
}

.segment-items {
  margin: 4px 0 8px 98px;
  border-left: 2px solid var(--color-border);
  padding-left: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.segment-item {
  display: grid;
  grid-template-columns: 92px 72px 1fr 76px;
  gap: 10px;
  align-items: center;
  font-size: 11px;
  color: var(--color-text);
  padding: 3px 6px;
  border-radius: 6px;
  cursor: pointer;
}

.segment-item:hover {
  background: var(--color-primary-light);
}

.seg-item-time,
.seg-item-user {
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.seg-item-input {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.seg-item-tok {
  text-align: right;
  white-space: nowrap;
  font-weight: 600;
  color: var(--color-primary);
}

.seg-items-hint {
  margin: 4px 0 0;
  font-size: 10px;
  color: var(--color-text-secondary);
}

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

/* ---- 标准化工具注册表（防穿格：固定列宽 + 换行 + chip） ---- */
.registry-scroll { overflow-x: auto; }
.registry-table { table-layout: fixed; min-width: 1140px; }
.registry-table th, .registry-table td {
  white-space: normal;
  word-break: break-word;
  vertical-align: top;
  line-height: 1.5;
}
.registry-table th { white-space: nowrap; }
.reg-name { font-weight: 600; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.reg-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.reg-chip {
  display: inline-block; font-style: normal; font-size: 10.5px; font-weight: 600;
  background: #f0ece6; color: var(--color-text-secondary);
  border-radius: 5px; padding: 1px 7px; line-height: 1.6; white-space: nowrap;
  max-width: 100%; overflow: hidden; text-overflow: ellipsis;
}
.reg-chip.ns { background: #e3edfd; color: #1d4ed8; }
.reg-chip.perm { background: #efe6fd; color: #7c3aed; }
.reg-chip.param { background: #fdf0d8; color: #b5822a; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.reg-chip.more { background: #ede6dc; }
.reg-empty { font-style: normal; color: var(--color-text-secondary); font-size: 11px; }
.reg-scene {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  font-size: 11.5px; color: var(--color-text-secondary); line-height: 1.5;
}
.reg-status { font-size: 11.5px; font-weight: 600; color: #2e7d46; }

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

/* ---- 需求3：合并后的段构成表 ---- */
.seg-merge-row { cursor: pointer; }
.seg-merge-row:hover { background: var(--color-primary-light); }
.seg-caret {
  display: inline-block;
  color: var(--color-primary);
  font-size: 11px;
  margin-right: 5px;
  transition: transform 0.15s ease;
}
.seg-caret.open { transform: rotate(90deg); }
.seg-drill-row > td { background: #fbf7ef; padding: 6px 10px; }
.seg-drill-title { margin: 4px 0; font-size: 12px; font-weight: 600; color: var(--color-text-secondary); }

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
