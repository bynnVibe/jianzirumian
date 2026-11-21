<template>
  <div class="eval-overview">
    <!-- ============ 顶部：标题 + 分段页签 + 工具条（图1 式） ============ -->
    <header class="eval-header">
      <div class="header-inner">
        <div class="title-row">
          <h1 class="page-title">评测</h1>
          <nav class="domain-tabs" role="tablist" aria-label="评测域">
            <button class="domain-tab" :class="{ active: domain === 'rag' }" role="tab" :aria-selected="domain === 'rag'" @click="switchDomain('rag')">
              RAG 评测
            </button>
            <button class="domain-tab" :class="{ active: domain === 'agent' }" role="tab" :aria-selected="domain === 'agent'" @click="switchDomain('agent')">
              Agent 评测
            </button>
            <button class="domain-tab" :class="{ active: domain === 'tool' }" role="tab" :aria-selected="domain === 'tool'" @click="switchDomain('tool')">
              工具评测
            </button>
          </nav>
        </div>
        <div v-if="domain !== 'agent'" class="toolbar">
          <input
            v-if="tab !== 'online' && tab !== 'badcases'"
            v-model.trim="keyword"
            class="search-input"
            type="search"
            :placeholder="tab === 'datasets' ? '搜索评测集名称…' : tab === 'evaluators' ? '搜索评估器名称…' : '搜索评测集 / 发起人…'"
          />
          <button class="tool-btn" title="刷新" :disabled="loadingDatasets || loadingEvals" @click="reloadAll">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
              <polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
            </svg>
            刷新
          </button>
          <button v-if="domain === 'rag' && tab === 'datasets'" class="tool-btn" @click="openCandidates(null)">候选审核</button>
          <button v-if="domain === 'rag' && tab === 'datasets'" class="btn-primary-btn" @click="openCreateDataset">＋ 新建评测集</button>
          <button v-else-if="domain === 'rag' && tab === 'evaluators'" class="btn-primary-btn" @click="openCreateEvaluator">＋ 新建评估器</button>
          <button v-else-if="domain === 'rag' && tab === 'experiments'" class="btn-primary-btn" @click="openCreateRun">＋ 新建实验</button>
        </div>
      </div>
      <div class="subtab-row">
        <nav class="seg-tabs" role="tablist" aria-label="评测分类">
          <button
            v-for="t in currentTabs" :key="t.key"
            class="seg-tab" :class="{ active: tab === t.key }" role="tab" :aria-selected="tab === t.key"
            @click="switchTab(t.key)"
          >
            {{ t.label }}
          </button>
        </nav>
      </div>
      <p class="header-sub">{{ headerSub }}</p>
    </header>

    <div class="overview-body">
      <p v-if="errorMsg" class="inline-error">{{ errorMsg }}</p>

      <!-- ============ RAG 评测域 ============ -->
      <template v-if="domain === 'rag'">
      <!-- ============ 评测集卡片格 ============ -->
      <template v-if="tab === 'datasets'">
        <div v-if="loadingDatasets" class="mini-loading">加载中…</div>
        <div v-else-if="!filteredDatasets.length" class="empty-block">
          <p>{{ ragDatasets.length ? '没有匹配「' + keyword + '」的评测集' : '还没有评测集，创建第一个开始问答回归' }}</p>
          <button class="outline-btn" @click="openCreateDataset">创建评测集</button>
        </div>
        <div v-else class="card-grid">
          <div v-for="d in filteredDatasets" :key="d.id" class="entry-card" :class="{ focused: d.id === focusedId }" @click="goDataset(d.id)">
            <div class="entry-card-head">
              <span class="entry-icon ds">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                  <path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
                </svg>
              </span>
              <span class="entry-name">{{ d.name }}</span>
              <span class="card-actions">
                <button class="icon-btn danger" title="删除评测集" @click.stop="removeDataset(d)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  </svg>
                </button>
              </span>
            </div>
            <p class="entry-desc">{{ d.description || '暂无描述' }}</p>
            <div class="entry-tags">
              <span class="tag-chip">{{ d.case_count }} 用例</span>
              <span class="tag-chip" :class="runChipCls(d.last_run)">{{ runStatusText(d.last_run) }}</span>
            </div>
            <div class="entry-foot">
              <button class="outline-btn sm" @click.stop="openBuild(d)">构建</button>
              <button class="outline-btn sm" @click.stop="openCandidates(d)">候选审核</button>
            </div>
          </div>
        </div>
      </template>

      <!-- ============ 评估器（基于评测集的评测工作流） ============ -->
      <template v-else-if="tab === 'evaluators'">
        <!-- ---- 页内运行监控（点击运行后在本页查看进度与结果） ---- -->
        <section v-if="activeRun" class="run-monitor">
          <div class="monitor-head">
            <span class="monitor-title">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              {{ activeRun.evaluator.name }}
            </span>
            <span class="tag-chip muted">{{ activeRun.evaluator.dataset_name }} · {{ activeRun.evaluator.case_count }} 用例</span>
            <span v-if="runDetail" class="status-pill" :class="runStatusCls(runDetail.run.status)">{{ statusLabel(runDetail.run.status) }}</span>
            <span class="monitor-progress-text">{{ progressText }}</span>
            <span class="monitor-actions">
              <button v-if="!runDetail || runDetail.run.status !== 'running'" class="run-btn sm" :disabled="startingRun" @click="runEvaluator(activeRun.evaluator)">
                {{ startingRun ? '发起中…' : '重新运行' }}
              </button>
              <button class="icon-btn" title="收起监控" @click="closeRun">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </span>
          </div>
          <div class="progress-track"><div class="progress-fill" :class="{ done: runDetail && runDetail.run.status === 'finished' }" :style="{ width: progressPct }"></div></div>
          <div v-if="runSummaryCards.length" class="monitor-metrics">
            <div v-for="m in runSummaryCards" :key="m.label" class="monitor-metric">
              <span class="mm-value" :class="m.cls">{{ m.value }}</span>
              <span class="mm-label">{{ m.label }}</span>
            </div>
          </div>
          <div v-if="loadingRun && !runDetail" class="mini-loading">加载运行详情…</div>
          <table v-else-if="runDetail && runDetail.results.length" class="monitor-table">
            <thead>
              <tr><th style="width:26%">问题</th><th>忠实</th><th>相关</th><th>精确</th><th>召回</th><th title="Hit@K：top K 内是否命中期望来源/关键词">Hit@K</th><th title="MRR：首个相关来源排名的倒数">MRR</th><th title="幻觉率：越低越好">幻觉↓</th><th>GEval</th><th>延迟</th><th>状态</th></tr>
            </thead>
            <tbody>
              <template v-for="row in runDetail.results" :key="row.id">
                <tr class="result-row" @click="toggleExpand(row.id)">
                  <td class="q-cell"><span class="exp-caret" :class="{ open: expanded.has(row.id) }">▸</span>{{ row.question }}</td>
                  <td><span class="score" :class="scoreCls(row.faithfulness)">{{ pct(row.faithfulness) }}</span></td>
                  <td><span class="score" :class="scoreCls(row.answer_relevance)">{{ pct(row.answer_relevance) }}</span></td>
                  <td><span class="score" :class="scoreCls(row.context_precision)">{{ pct(row.context_precision) }}</span></td>
                  <td><span class="score" :class="scoreCls(row.context_recall)">{{ pct(row.context_recall) }}</span></td>
                  <td><span class="score" :class="row.hit_at_k ? 'good' : 'na'">{{ row.hit_at_k ? '命中' : '未中' }}</span></td>
                  <td><span class="score" :class="row.mrr ? scoreCls(row.mrr) : 'na'">{{ mrrText(row.mrr) }}</span></td>
                  <td><span class="score" :class="hallucCls(row.hallucination)">{{ pct(row.hallucination) }}</span></td>
                  <td><span class="score" :class="scoreCls(row.geval_score)">{{ pct(row.geval_score) }}</span></td>
                  <td>{{ row.latency_ms }}ms</td>
                  <td><span class="status-pill sm" :class="row.status === 'ok' ? 'finished' : 'failed'">{{ row.status === 'ok' ? '成功' : '错误' }}</span></td>
                </tr>
                <tr v-if="expanded.has(row.id)" class="expand-row">
                  <td colspan="11">
                    <div class="expand-inner">
                      <div v-if="row.error" class="err-line">错误：{{ row.error }}</div>
                      <div class="expand-block">
                        <span class="expand-title">模型答案</span>
                        <div class="answer-box">{{ row.answer || '(空)' }}</div>
                      </div>
                      <div v-if="row.judge_reason" class="expand-block">
                        <span class="expand-title">评分理由</span>
                        <div class="answer-box">{{ row.judge_reason }}</div>
                      </div>
                      <div v-if="row.geval_reason" class="expand-block">
                        <span class="expand-title">GEval 准则评分理由</span>
                        <div class="answer-box">{{ row.geval_reason }}</div>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
          <p v-else-if="runDetail" class="empty-hint">暂无用例结果，运行产出结果后将自动刷新。</p>
          <p v-else class="empty-hint">该评估器还没有运行记录，点击「重新运行」在页内发起评测。</p>
        </section>

        <!-- ---- 评估器卡片格 ---- -->
        <div v-if="loadingEvals" class="mini-loading">加载中…</div>
        <div v-else-if="!filteredEvals.length" class="empty-block">
          <p>{{ evaluators.length ? '没有匹配「' + keyword + '」的评估器' : '还没有评估器，基于已有评测集构建评测工作流' }}</p>
          <button class="outline-btn" @click="openCreateEvaluator">新建评估器</button>
        </div>
        <div v-else class="card-grid">
          <div v-for="ev in filteredEvals" :key="ev.id" class="entry-card" @click="openRun(ev)">
            <div class="entry-card-head">
              <span class="entry-icon ev">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                  <circle cx="5" cy="6" r="2.2" /><circle cx="19" cy="6" r="2.2" /><circle cx="12" cy="18" r="2.2" />
                  <path d="M7 7.5l3.5 8M17 7.5l-3.5 8M7.2 6h9.6" />
                </svg>
              </span>
              <span class="entry-name">{{ ev.name }}</span>
              <span class="card-actions">
                <button class="icon-btn" title="编辑评估器" @click.stop="openEditEvaluator(ev)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <path d="M17 3a2.83 2.83 0 0 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                  </svg>
                </button>
                <button class="icon-btn danger" title="删除评估器" @click.stop="removeEvaluator(ev)">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                    <polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  </svg>
                </button>
              </span>
            </div>
            <p class="entry-desc">{{ ev.description || `基于评测集「${ev.dataset_name}」的评测工作流` }}</p>
            <div class="entry-tags">
              <span class="tag-chip ds-chip">{{ ev.dataset_name }}</span>
              <span class="tag-chip">{{ ev.case_count }} 用例</span>
              <span class="tag-chip muted">{{ ev.run_count }} 次运行</span>
              <span class="tag-chip" :class="runChipCls(ev.last_run)">{{ runStatusText(ev.last_run) }}</span>
            </div>
            <div class="entry-tags">
              <span v-for="m in evaluatorMetricLabels(ev)" :key="m" class="tag-chip metric-chip">{{ m }}</span>
            </div>
            <div class="entry-foot">
              <button class="run-btn" :disabled="startingRun || !ev.dataset_exists" @click.stop="runEvaluator(ev)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13">
                  <polygon points="5 3 19 12 5 21 5 3" />
                </svg>
                运行
              </button>
              <button v-if="ev.last_run" class="outline-btn sm" @click.stop="openRun(ev)">查看结果</button>
            </div>
          </div>
        </div>

        <!-- Prompt / Skill 专项评测工作台已移至「Agent 评测」域 -->
      </template>

      <!-- ============ 在线采样评测（DeepEval 风格 · 质量自动回流） ============ -->
      <template v-else-if="tab === 'online'">
        <div class="online-toolbar">
          <select v-model.number="onlineDays" class="input online-days" @change="loadOnline(true)">
            <option :value="7">近 7 天</option>
            <option :value="14">近 14 天</option>
            <option :value="30">近 30 天</option>
          </select>
          <span class="online-note">后台按 10% 采样率对线上回答自动评测（单轮上限 5 条）；无检索上下文时仅跑 GEval。共 {{ onlineTotal }} 条样本。</span>
        </div>
        <div v-if="onlineBusiness" class="biz-wrap">
          <div class="monitor-metrics online-metrics">
            <div class="monitor-metric"><span class="mm-value" :class="rateCls(onlineBusiness.dislike_rate, 0.1)">{{ ratePct(onlineBusiness.dislike_rate) }}</span><span class="mm-label">点踩率</span></div>
            <div class="monitor-metric"><span class="mm-value">{{ ratePct(onlineBusiness.followup_rate) }}</span><span class="mm-label">追问率</span></div>
            <div class="monitor-metric"><span class="mm-value" :class="rateCls(onlineBusiness.empty_answer_rate, 0.1)">{{ ratePct(onlineBusiness.empty_answer_rate) }}</span><span class="mm-label">空回答率</span></div>
          </div>
          <p class="biz-note">业务三率基于近 {{ onlineDays }} 天 {{ onlineBusiness.samples }} 条助手回答（纯 SQL 确定性口径，零 LLM 成本）；追问窗口 {{ onlineBusiness.followup_window_seconds }}s。点踩率 / 空回答率越低越好，超阈值参与质量告警。</p>
        </div>
        <div v-if="onlineAgg" class="monitor-metrics online-metrics">
          <div class="monitor-metric"><span class="mm-value">{{ onlineAgg.samples }}</span><span class="mm-label">窗口样本数</span></div>
          <div class="monitor-metric"><span class="mm-value" :class="scoreCls(onlineAgg.avg_faithfulness)">{{ pct(onlineAgg.avg_faithfulness) }}</span><span class="mm-label">平均忠实度</span></div>
          <div class="monitor-metric"><span class="mm-value" :class="scoreCls(onlineAgg.avg_geval)">{{ pct(onlineAgg.avg_geval) }}</span><span class="mm-label">平均 GEval 分</span></div>
          <div class="monitor-metric"><span class="mm-value" :class="onlineAgg.low_count ? 'bad' : ''">{{ onlineAgg.low_count }}</span><span class="mm-label">低分样本（&lt;60%）</span></div>
        </div>
        <div v-if="loadingOnline && !onlineItems.length" class="mini-loading">加载在线评测中…</div>
        <div v-else-if="!onlineItems.length" class="empty-block">
          <p>暂无在线评测样本。发起对话后后台会自动采样评测，稍后刷新即可查看。</p>
        </div>
        <table v-else class="exp-table online-table">
          <thead>
            <tr><th>时间</th><th style="width:38%">问题（脱敏摘要）</th><th>忠实度</th><th>GEval</th><th>上下文</th><th>状态</th></tr>
          </thead>
          <tbody>
            <template v-for="row in onlineItems" :key="row.id">
              <tr class="exp-row" @click="toggleOnlineExpand(row.id)">
                <td class="cell-time"><span class="exp-caret" :class="{ open: onlineExpanded.has(row.id) }">▸</span>{{ row.created_at }}</td>
                <td class="online-q">{{ row.question }}</td>
                <td><span class="score" :class="scoreCls(row.faithfulness)">{{ pct(row.faithfulness) }}</span></td>
                <td><span class="score" :class="scoreCls(row.geval_score)">{{ pct(row.geval_score) }}</span></td>
                <td>{{ row.context_chars ? formatNum(row.context_chars) + ' 字符' : '无检索' }}</td>
                <td><span class="status-pill sm" :class="row.status === 'ok' ? 'finished' : 'failed'">{{ row.status === 'ok' ? '成功' : '跳过/错误' }}</span></td>
              </tr>
              <tr v-if="onlineExpanded.has(row.id)" class="expand-row">
                <td colspan="6">
                  <div class="expand-inner">
                    <div class="expand-block">
                      <span class="expand-title">回答摘要（脱敏）</span>
                      <div class="answer-box">{{ row.answer || '(空)' }}</div>
                    </div>
                    <div v-if="row.geval_reason" class="expand-block">
                      <span class="expand-title">GEval 评分理由</span>
                      <div class="answer-box">{{ row.geval_reason }}</div>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-if="onlineTotal > onlineItems.length || onlineOffset > 0" class="online-pager">
          <button class="outline-btn sm" :disabled="onlineOffset <= 0 || loadingOnline" @click="onlinePage(-1)">上一页</button>
          <span class="online-page-text">{{ onlineOffset + 1 }}–{{ Math.min(onlineOffset + onlineItems.length, onlineTotal) }} / {{ onlineTotal }}</span>
          <button class="outline-btn sm" :disabled="onlineOffset + onlineItems.length >= onlineTotal || loadingOnline" @click="onlinePage(1)">下一页</button>
        </div>
      </template>

      <!-- ============ BadCase 分析（多源收集 / 归因 / 转用例闭环） ============ -->
      <template v-else-if="tab === 'badcases'">
        <EvalBadCases />
      </template>

      <!-- ============ 实验（跨评测集的运行历史） ============ -->
      <template v-else>
        <div v-if="loadingRuns" class="mini-loading">加载中…</div>
        <div v-else-if="!filteredRuns.length" class="empty-block">
          <p>{{ allRuns.length ? '没有匹配「' + keyword + '」的实验' : '还没有实验，选择评测集发起第一次运行' }}</p>
          <button class="outline-btn" @click="openCreateRun">新建实验</button>
        </div>
        <table v-else class="exp-table">
          <thead>
            <tr>
              <th>开始时间</th><th>评测集</th><th>状态</th><th>进度</th>
              <th>忠实</th><th>相关</th><th>精确</th><th>召回</th><th>延迟</th><th>发起人</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in filteredRuns" :key="r.id" class="exp-row" @click="goRun(r)">
              <td class="cell-time">{{ fmtTime(r.started_at) }}</td>
              <td class="exp-ds">{{ r.dataset_name }}</td>
              <td><span class="tag-chip" :class="runChipCls(r)">{{ statusLabel(r.status) }}</span></td>
              <td>{{ r.completed }}/{{ r.total }}</td>
              <td><span class="score" :class="scoreCls(r.summary?.avg_faithfulness)">{{ pct(r.summary?.avg_faithfulness) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary?.avg_answer_relevance)">{{ pct(r.summary?.avg_answer_relevance) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary?.avg_context_precision)">{{ pct(r.summary?.avg_context_precision) }}</span></td>
              <td><span class="score" :class="scoreCls(r.summary?.avg_context_recall)">{{ pct(r.summary?.avg_context_recall) }}</span></td>
              <td>{{ r.summary?.avg_latency_ms != null ? Math.round(r.summary.avg_latency_ms) + 'ms' : '-' }}</td>
              <td>{{ r.created_by || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </template>
      </template>

      <!-- ============ 工具评测域（@tool 样例测试通过后自动派生评测集 → 评测通过进入标准化工具注册表） ============ -->
      <template v-else-if="domain === 'tool'">
        <div v-if="loadingDatasets" class="mini-loading">加载中…</div>
        <div v-else-if="!filteredDatasets.length" class="empty-block">
          <p>{{ datasets.some((d) => d.kind === 'tool') ? '没有匹配「' + keyword + '」的工具评测集' : '还没有工具评测集。管理员在聊天中 @tool 添加工具并通过只读样例测试后，会自动派生评测集并发起评测。' }}</p>
        </div>
        <div v-else class="card-grid">
          <div v-for="d in filteredDatasets" :key="d.id" class="entry-card" :class="{ focused: d.id === focusedId }" @click="goDataset(d.id)">
            <div class="entry-card-head">
              <span class="entry-icon ds tool-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                  <path d="M14.7 6.3a4.5 4.5 0 0 0-6 6L3 18l3 3 5.7-5.7a4.5 4.5 0 0 0 6-6L14 13l-3-3 3.7-3.7Z" />
                </svg>
              </span>
              <span class="entry-name">{{ d.name }}</span>
            </div>
            <p class="entry-desc">{{ d.description || '由工具版本声明自动派生的评测集' }}</p>
            <div class="entry-tags">
              <span class="tag-chip">{{ d.case_count }} 用例</span>
              <span class="tag-chip" :class="runChipCls(d.last_run)">{{ runStatusText(d.last_run) }}</span>
              <span v-if="d.last_run && d.last_run.summary && d.last_run.summary.pass_rate != null" class="tag-chip" :class="d.last_run.summary.pass_rate >= 1 ? 'ok' : 'bad'">通过率 {{ pct(d.last_run.summary.pass_rate) }}</span>
            </div>
            <div class="entry-foot">
              <button class="run-btn" :disabled="startingRun || (d.last_run && d.last_run.status === 'running')" @click.stop="runToolEval(d)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13">
                  <polygon points="5 3 19 12 5 21 5 3" />
                </svg>
                {{ d.last_run && d.last_run.status === 'running' ? '评测中…' : '重新评测' }}
              </button>
              <button class="outline-btn sm" @click.stop="goDataset(d.id)">查看用例与结果</button>
            </div>
          </div>
        </div>
        <p class="empty-hint">工具评测集由工具版本声明自动派生，不支持手动增删用例；全部用例通过后工具进入「工具注册表」（工具治理中心 /tools 查看）。</p>
      </template>

      <!-- ============ Agent 评测域 ============ -->
      <template v-else>
        <AgentEvalPanel v-if="tab === 'trajectory'" />
        <AgentOnlinePanel v-else-if="tab === 'online'" />
        <section v-else class="workbench-section agent-pse">
          <div class="workbench-head">
            <div>
              <span class="workbench-title">Prompt / Skill 专项评测工作台</span>
              <span class="workbench-sub">静态断言 + Rubric 多维打分 + A/B 行为对比 · Agent 组件级评测，已整合在本页，无需跳转</span>
            </div>
            <button class="tool-btn" @click="goPromptSkill">独立页面打开</button>
          </div>
          <PromptSkillEval />
        </section>
      </template>
    </div>

    <!-- 候选审核抽屉（pending 列表 / 编辑后通过 / 拒绝 / 批量通过） -->
    <EvalCandidates
      v-if="candidatesTarget"
      :dataset-id="candidatesTarget.id"
      :dataset-name="candidatesTarget.name"
      @close="candidatesTarget = null"
      @changed="loadDatasets"
    />

    <!-- 构建测试集弹窗（LLM 基于知识库生成 / 日志挖掘 → 候选待审池） -->
    <div v-if="buildTarget" class="modal-overlay" @click.self="buildTarget = null">
      <div class="modal-box">
        <h3 class="modal-title">构建测试集 · {{ buildTarget.name }}</h3>
        <div class="build-tabs">
          <button class="build-tab" :class="{ active: buildMode === 'kb' }" @click="buildMode = 'kb'">LLM 基于知识库生成</button>
          <button class="build-tab" :class="{ active: buildMode === 'logs' }" @click="buildMode = 'logs'">日志挖掘</button>
        </div>
        <template v-if="buildMode === 'kb'">
          <label class="field">
            <span class="field-label">知识库 <em>*</em></span>
            <select class="input" v-model="buildForm.kbId">
              <option value="" disabled>选择知识库</option>
              <option v-for="kb in knowledgeBases" :key="kb.id" :value="kb.id">{{ kb.name }}</option>
            </select>
          </label>
          <label class="field">
            <span class="field-label">生成数量</span>
            <input class="input" type="number" min="1" max="20" v-model.number="buildForm.count" />
          </label>
          <p class="modal-sub">LLM 基于知识库内容生成问答候选（含期望来源），写入待审池，审核通过后入集，不直接污染评测集。</p>
          <div class="modal-actions">
            <button class="outline-btn" @click="buildTarget = null">关闭</button>
            <button class="btn-primary-btn" :disabled="building || !buildForm.kbId" @click="doGenerate">{{ building ? '生成中…' : '生成候选' }}</button>
          </div>
        </template>
        <template v-else>
          <p class="modal-sub">从线上对话日志挖掘真实问答对（use_knowledge 且回答非空），新增配对 ≥ 20 才触发；LLM 转为候选写入待审池（origin=log_mining）。</p>
          <div class="modal-actions">
            <button class="outline-btn" @click="buildTarget = null">关闭</button>
            <button class="btn-primary-btn" :disabled="building" @click="doMine">{{ building ? '挖掘中…' : '立即挖掘' }}</button>
          </div>
        </template>
        <p v-if="buildMsg" class="build-msg" :class="{ ok: buildOk }">{{ buildMsg }}</p>
      </div>
    </div>

    <!-- 新建评测集弹窗 -->
    <div v-if="showCreateDataset" class="modal-overlay" @click.self="showCreateDataset = false">
      <div class="modal-box">
        <h3 class="modal-title">新建评测集</h3>
        <label class="field">
          <span class="field-label">名称 <em>*</em></span>
          <input class="input" v-model.trim="datasetForm.name" placeholder="如：中医养生核心问答集" />
        </label>
        <label class="field">
          <span class="field-label">描述</span>
          <textarea class="input textarea" v-model.trim="datasetForm.description" rows="3" placeholder="评测集用途说明（选填）"></textarea>
        </label>
        <div class="modal-actions">
          <button class="outline-btn" @click="showCreateDataset = false">取消</button>
          <button class="btn-primary-btn" :disabled="creatingDataset || !datasetForm.name" @click="createDataset">
            {{ creatingDataset ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 新建/编辑评估器弹窗：基于已有评测集构建工作流 -->
    <div v-if="showCreateEvaluator" class="modal-overlay" @click.self="showCreateEvaluator = false">
      <div class="modal-box">
        <h3 class="modal-title">{{ editingEvaluatorId ? '编辑评估器' : '新建评估器' }}</h3>
        <p v-if="!editingEvaluatorId" class="modal-sub">选择一个已有评测集作为数据源构建评测工作流；创建后可在页内一键运行并查看结果。</p>
        <p v-if="!editingEvaluatorId && !ragDatasets.length" class="modal-sub warn">还没有评测集，请先在「评测集」页签创建评测集并添加用例。</p>
        <label class="field">
          <span class="field-label">评估器名称 <em>*</em></span>
          <input class="input" v-model.trim="evaluatorForm.name" placeholder="如：问答主链路回归工作流" />
        </label>
        <label class="field">
          <span class="field-label">绑定评测集 <em>*</em></span>
          <select class="input" v-model="evaluatorForm.dataset_id" :disabled="!!editingEvaluatorId">
            <option value="" disabled>选择评测集</option>
            <option v-for="d in ragDatasets" :key="d.id" :value="d.id">{{ d.name }}（{{ d.case_count }} 用例）</option>
          </select>
        </label>
        <label class="field">
          <span class="field-label">描述</span>
          <textarea class="input textarea" v-model.trim="evaluatorForm.description" rows="2" placeholder="工作流用途说明（选填）"></textarea>
        </label>
        <div class="field">
          <span class="field-label">评测指标（DeepEval 风格 · 不选=默认 Ragas 四指标）</span>
          <div class="metric-checks">
            <label v-for="m in METRIC_OPTIONS" :key="m.key" class="metric-check">
              <input type="checkbox" :value="m.key" v-model="evaluatorForm.metrics" />
              <span>{{ m.label }}<em v-if="m.hint">{{ m.hint }}</em></span>
            </label>
          </div>
        </div>
        <label class="field" v-if="evaluatorForm.metrics.includes('geval')">
          <span class="field-label">GEval 评估准则（自然语言描述，模型按准则链式评估打 1-10 分）</span>
          <textarea class="input textarea" v-model.trim="evaluatorForm.geval_criteria" rows="3" placeholder="如：回答准确、有用、与问题相关，且给出可操作的建议"></textarea>
        </label>
        <label class="field">
          <span class="field-label">Hit@K 的 K（检索排名指标：top K 内命中期望来源/关键词即算 Hit@K，MRR=首命中排名倒数；默认 5）</span>
          <input class="input" type="number" min="1" max="50" v-model.number="evaluatorForm.hit_k" />
        </label>
        <div class="modal-actions">
          <button class="outline-btn" @click="showCreateEvaluator = false">取消</button>
          <button class="btn-primary-btn" :disabled="savingEvaluator || !evaluatorForm.name || !evaluatorForm.dataset_id" @click="saveEvaluator">
            {{ savingEvaluator ? '保存中…' : editingEvaluatorId ? '保存' : '创建' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 新建实验弹窗：选择评测集发起一次运行 -->
    <div v-if="showCreateRun" class="modal-overlay" @click.self="showCreateRun = false">
      <div class="modal-box">
        <h3 class="modal-title">新建实验</h3>
        <p class="modal-sub">选择评测集发起一次评测运行；发起后在「实验」页签查看进度，点击行进入逐用例结果。</p>
        <p v-if="!ragDatasets.length" class="modal-sub warn">还没有评测集，请先在「评测集」页签创建评测集并添加用例。</p>
        <label class="field">
          <span class="field-label">评测集 <em>*</em></span>
          <select class="input" v-model="runFormDatasetId">
            <option value="" disabled>选择评测集</option>
            <option v-for="d in ragDatasets" :key="d.id" :value="d.id">{{ d.name }}（{{ d.case_count }} 用例）</option>
          </select>
        </label>
        <div class="modal-actions">
          <button class="outline-btn" @click="showCreateRun = false">取消</button>
          <button class="btn-primary-btn" :disabled="creatingRun || !runFormDatasetId" @click="createRun">
            {{ creatingRun ? '发起中…' : '发起实验' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import PromptSkillEval from '@/components/PromptSkillEval.vue'
import EvalBadCases from '@/components/eval/EvalBadCases.vue'
import EvalCandidates from '@/components/eval/EvalCandidates.vue'
import AgentEvalPanel from '@/components/eval/AgentEvalPanel.vue'
import AgentOnlinePanel from '@/components/eval/AgentOnlinePanel.vue'
import {
  listEvalDatasets, createEvalDataset, deleteEvalDataset,
  listEvalEvaluators, createEvalEvaluator, updateEvalEvaluator, deleteEvalEvaluator,
  startEvalRun, getEvalRun, listAllEvalRuns, getObsOnlineEval,
  generateEvalCandidates, mineEvalCandidates, listKnowledgeBases,
} from '@/api'

const router = useRouter()
const route = useRoute()

// ---- 顶级域：RAG 评测 | Agent 评测（?domain=rag|agent 深链，默认 rag；存量 ?tab= 兼容映射到 RAG） ----
const RAG_TABS = [
  { key: 'datasets', label: '评测集' },
  { key: 'evaluators', label: '评估器' },
  { key: 'experiments', label: '实验' },
  { key: 'online', label: '在线评测' },
  { key: 'badcases', label: 'BadCase 分析' },
]
const AGENT_TABS = [
  { key: 'trajectory', label: '轨迹评测' },
  { key: 'online', label: '在线轨迹' },
  { key: 'prompt-skill', label: 'Prompt · Skill' },
]
const TOOL_TABS = [
  { key: 'datasets', label: '评测集' },
]
const RAG_KEYS = RAG_TABS.map((t) => t.key)
const AGENT_KEYS = AGENT_TABS.map((t) => t.key)
const TOOL_KEYS = TOOL_TABS.map((t) => t.key)

function resolveDomain(q) { return q === 'agent' || q === 'tool' ? q : 'rag' }
function resolveTab(dom, q) {
  const keys = dom === 'agent' ? AGENT_KEYS : dom === 'tool' ? TOOL_KEYS : RAG_KEYS
  const def = dom === 'agent' ? 'trajectory' : 'datasets'
  return keys.includes(q) ? q : def
}

const domain = ref(resolveDomain(route.query.domain))
const tab = ref(resolveTab(domain.value, route.query.tab))
const currentTabs = computed(() => (domain.value === 'agent' ? AGENT_TABS : domain.value === 'tool' ? TOOL_TABS : RAG_TABS))
const keyword = ref('')
const focusedId = ref('')

// 深链同步：?domain=&tab= 变化时校正本地状态（存量仅 ?tab= 默认 RAG 域）
watch(() => [route.query.domain, route.query.tab], ([d, t]) => {
  const dom = resolveDomain(d)
  const changed = dom !== domain.value
  domain.value = dom
  tab.value = resolveTab(dom, t)
  if (changed && dom === 'tool') loadDatasets()
})

function syncQuery() {
  router.replace({ query: { ...route.query, domain: domain.value, tab: tab.value } })
}
function switchDomain(d) {
  if (domain.value === d) return
  domain.value = d
  tab.value = d === 'agent' ? 'trajectory' : 'datasets'
  keyword.value = ''
  syncQuery()
  if (d === 'tool') loadDatasets()
}
function switchTab(t) {
  if (tab.value === t) return
  tab.value = t
  syncQuery()
}

const headerSub = computed(() => {
  if (domain.value === 'tool') {
    return '工具评测：管理员在聊天中 @tool 添加工具并通过只读样例测试后，自动按工具版本声明派生评测集并发起评测（真实调用 + 断言校验）。全部用例通过后工具进入「标准化工具注册表」；评测集不支持手动修改，可在详情页查看逐用例结果，或在此重新评测。'
  }
  if (domain.value === 'agent') {
    return tab.value === 'trajectory'
      ? 'Agent 轨迹评测（离线）：对 kind=agent 评测集逐用例真实运行 Agent，按 AgentEvals 轨迹匹配（严格 / 无序 / 子集 / 超集）与 DeepEval 六指标（任务完成 / 步骤效率 / 工具正确 / 参数正确 / 规划质量 / 规划遵循）+ 整体轨迹评分量化。'
      : tab.value === 'online'
        ? 'Agent 在线轨迹：后台按采样率对线上已结束的 chat Agent 轨迹自动评测（无参考六指标），质量均分回流可观测性并参与告警；支持手动评测指定 trace。文本已脱敏。'
        : 'Prompt / Skill 组件级评测：静态断言 + Rubric 多维打分 + A/B 行为对比，据结果一键优化并上线，作为 Agent 域的动作 / 推理组件评测补充。'
  }
  return tab.value === 'datasets'
    ? '问答回归 · Ragas：按评测集对问答主链路跑离线回归，量化忠实度 / 回答相关性 / 上下文精确率 / 召回率与 Hit@K / MRR 检索排名指标。支持 LLM 基于知识库生成、日志挖掘构建测试集（候选审核入集）。点击卡片进入详情。'
    : tab.value === 'evaluators'
      ? '评估器：基于已有评测集构建评测工作流，可配置 DeepEval 风格指标（幻觉率 / GEval 准则评分）与 Hit@K 的 K，一键运行并在页内实时查看评测进度与逐用例结果。'
      : tab.value === 'online'
        ? '在线评测：后台按采样率对线上真实回答自动评测（忠实度 + GEval），并统计点踩率 / 追问率 / 空回答率业务三率；质量均分回流可观测性并参与质量告警。文本已脱敏。'
        : tab.value === 'badcases'
          ? 'BadCase 分析：多源自动收集低分 / 失败用例（离线 / 在线评测、点踩、空回答、Agent 低分），规则化归因 + 可选 LLM 增强，转评测用例跑回归形成闭环。文本已脱敏。'
          : '实验：一次评测运行即一个实验。选择评测集发起实验，点击行进入该运行的逐用例结果与环比分析。'
})

const datasets = ref([])
const loadingDatasets = ref(false)
const evaluators = ref([])
const loadingEvals = ref(false)
const errorMsg = ref('')

onMounted(reloadAll)
onUnmounted(() => {
  stopPolling()
  stopRunsPolling()
  stopToolPolling()
})

function errMsg(e, fallback) {
  return e?.response?.data?.message || e?.response?.data?.detail || e?.message || fallback
}

function reloadAll() {
  if (domain.value === 'tool') { loadDatasets(); return }
  if (domain.value !== 'rag') return
  loadDatasets()
  loadEvaluators()
  if (tab.value === 'experiments') loadRunsAll()
  if (tab.value === 'online') loadOnline(true)
}

async function loadDatasets() {
  loadingDatasets.value = true
  try {
    const res = await listEvalDatasets()
    datasets.value = res.data.datasets || []
    if (domain.value === 'tool') manageToolPolling()
  } catch (e) {
    errorMsg.value = errMsg(e, '加载评测集失败')
  } finally {
    loadingDatasets.value = false
  }
}

async function loadEvaluators() {
  loadingEvals.value = true
  try {
    const res = await listEvalEvaluators()
    evaluators.value = res.data.evaluators || []
    // 运行结束后同步监控面板中的评估器快照（用例数/最近运行）
    if (activeRun.value) {
      const fresh = evaluators.value.find((x) => x.id === activeRun.value.evaluator.id)
      if (fresh) activeRun.value = { ...activeRun.value, evaluator: fresh }
    }
  } catch (e) {
    errorMsg.value = errMsg(e, '加载评估器失败')
  } finally {
    loadingEvals.value = false
  }
}

const filteredDatasets = computed(() => {
  const base = domain.value === 'tool'
    ? datasets.value.filter((d) => d.kind === 'tool')
    : datasets.value.filter((d) => d.kind !== 'tool')
  if (!keyword.value) return base
  return base.filter((d) => (d.name || '').toLowerCase().includes(keyword.value.toLowerCase()))
})
const filteredEvals = computed(() => {
  if (!keyword.value) return evaluators.value
  const kw = keyword.value.toLowerCase()
  return evaluators.value.filter((e) =>
    (e.name || '').toLowerCase().includes(kw) || (e.dataset_name || '').toLowerCase().includes(kw))
})
// 工具评测集不参与 RAG 评估器 / 实验绑定
const ragDatasets = computed(() => datasets.value.filter((d) => d.kind !== 'tool'))

function goDataset(id) {
  focusedId.value = id
  router.push({ name: 'EvalDataset', params: { datasetId: id } })
}

function goPromptSkill() {
  router.push({ name: 'EvalPromptSkill' })
}

// ---- 工具评测（kind=tool 评测集自动派生；重新评测与实验同一入口，后台真实调用 + 断言） ----
let toolPollTimer = null

async function runToolEval(d) {
  startingRun.value = true
  errorMsg.value = ''
  try {
    await startEvalRun(d.id)
    await loadDatasets()
  } catch (e) {
    errorMsg.value = e?.response?.status === 409
      ? (e.response.data?.message || '已有评测任务在执行中，请等待其结束后再试')
      : errMsg(e, '发起工具评测失败')
  } finally {
    startingRun.value = false
  }
}

function manageToolPolling() {
  const running = datasets.value.some((d) => d.kind === 'tool' && d.last_run?.status === 'running')
  if (running && !toolPollTimer) {
    toolPollTimer = setInterval(async () => { await loadDatasets() }, 3000)
  } else if (!running && toolPollTimer) {
    stopToolPolling()
  }
}

function stopToolPolling() {
  if (toolPollTimer) {
    clearInterval(toolPollTimer)
    toolPollTimer = null
  }
}

// ---- 测试集构建（LLM 基于知识库生成 / 日志挖掘 → 候选待审池）+ 候选审核抽屉 ----
const buildTarget = ref(null)        // 目标评测集对象
const buildMode = ref('kb')          // kb | logs
const buildForm = reactive({ kbId: '', count: 5 })
const building = ref(false)
const buildMsg = ref('')
const buildOk = ref(false)
const knowledgeBases = ref([])
const candidatesTarget = ref(null)   // { id, name }；id='' 表示全部评测集

function openBuild(d) {
  buildTarget.value = d
  buildMode.value = 'kb'
  buildForm.kbId = ''
  buildForm.count = 5
  buildMsg.value = ''
  buildOk.value = false
  if (!knowledgeBases.value.length) loadKnowledgeBases()
}

async function loadKnowledgeBases() {
  try {
    const res = await listKnowledgeBases()
    knowledgeBases.value = res.data.bases || []
  } catch (e) { /* KB 列表仅用于构建下拉，失败不阻断 */ }
}

async function doGenerate() {
  building.value = true
  buildMsg.value = ''
  buildOk.value = false
  try {
    const res = await generateEvalCandidates({
      datasetId: buildTarget.value.id, kbId: buildForm.kbId, count: buildForm.count || 5,
    })
    buildOk.value = true
    buildMsg.value = `已生成 ${res.data.created ?? 0} 条候选，请在「候选审核」中筛选通过后入集。`
  } catch (e) {
    buildMsg.value = errMsg(e, '生成候选失败')
  } finally {
    building.value = false
  }
}

async function doMine() {
  building.value = true
  buildMsg.value = ''
  buildOk.value = false
  try {
    const res = await mineEvalCandidates(buildTarget.value.id)
    if (res.data.triggered) {
      buildOk.value = true
      buildMsg.value = `日志挖掘触发：新增配对 ${res.data.pairs ?? 0} 条，生成候选 ${res.data.created ?? 0} 条，请在「候选审核」中查看。`
    } else {
      buildMsg.value = `新增配对不足（${res.data.pairs ?? 0} 条，阈值 ${res.data.min_pairs ?? 20}），本轮未触发挖掘。`
    }
  } catch (e) {
    buildMsg.value = errMsg(e, '日志挖掘失败')
  } finally {
    building.value = false
  }
}

function openCandidates(d) {
  candidatesTarget.value = d ? { id: d.id, name: d.name } : { id: '', name: '' }
}

// ---- 新建评测集 ----
const showCreateDataset = ref(false)
const creatingDataset = ref(false)
const datasetForm = reactive({ name: '', description: '' })

function openCreateDataset() {
  datasetForm.name = ''
  datasetForm.description = ''
  showCreateDataset.value = true
}

async function createDataset() {
  creatingDataset.value = true
  try {
    const res = await createEvalDataset({ name: datasetForm.name, description: datasetForm.description })
    showCreateDataset.value = false
    goDataset(res.data.dataset.id)
  } catch (e) {
    errorMsg.value = errMsg(e, '创建评测集失败')
  } finally {
    creatingDataset.value = false
  }
}

async function removeDataset(d) {
  if (!window.confirm(`确认删除评测集「${d.name}」？其用例与运行历史将一并删除。`)) return
  try {
    await deleteEvalDataset(d.id)
    datasets.value = datasets.value.filter((x) => x.id !== d.id)
  } catch (e) {
    errorMsg.value = errMsg(e, '删除评测集失败')
  }
}

// ---- 新建/编辑评估器 ----
// DeepEval 风格指标选项（与后端 deepeval_metrics.ALL_METRICS 对齐；不选=默认 Ragas 四指标）
const METRIC_OPTIONS = [
  { key: 'faithfulness', label: '忠实度' },
  { key: 'answer_relevancy', label: '回答相关性' },
  { key: 'contextual_precision', label: '上下文精确率' },
  { key: 'contextual_recall', label: '上下文召回率' },
  { key: 'hallucination', label: '幻觉率', hint: '越低越好' },
  { key: 'geval', label: 'GEval 准则评分', hint: '自定义准则' },
]
const METRIC_LABEL_MAP = Object.fromEntries(METRIC_OPTIONS.map((m) => [m.key, m.label]))

function evaluatorMetricLabels(ev) {
  const names = ev?.metric_config?.metrics || []
  if (!names.length) return ['Ragas 默认四指标']
  return names.map((m) => METRIC_LABEL_MAP[m] || m)
}

const showCreateEvaluator = ref(false)
const savingEvaluator = ref(false)
const editingEvaluatorId = ref('')
const evaluatorForm = reactive({ name: '', dataset_id: '', description: '', metrics: [], geval_criteria: '', hit_k: 5 })

function openCreateEvaluator() {
  editingEvaluatorId.value = ''
  evaluatorForm.name = ''
  evaluatorForm.dataset_id = ''
  evaluatorForm.description = ''
  evaluatorForm.metrics = []
  evaluatorForm.geval_criteria = ''
  evaluatorForm.hit_k = 5
  if (!datasets.value.length) loadDatasets()
  showCreateEvaluator.value = true
}

function openEditEvaluator(ev) {
  editingEvaluatorId.value = ev.id
  evaluatorForm.name = ev.name
  evaluatorForm.dataset_id = ev.dataset_id
  evaluatorForm.description = ev.description || ''
  evaluatorForm.metrics = [...(ev.metric_config?.metrics || [])]
  evaluatorForm.geval_criteria = ev.metric_config?.geval_criteria || ''
  evaluatorForm.hit_k = ev.hit_k || 5
  showCreateEvaluator.value = true
}

async function saveEvaluator() {
  savingEvaluator.value = true
  try {
    if (editingEvaluatorId.value) {
      await updateEvalEvaluator(editingEvaluatorId.value, {
        name: evaluatorForm.name,
        description: evaluatorForm.description,
        metrics: evaluatorForm.metrics,
        gevalCriteria: evaluatorForm.geval_criteria,
        hitK: evaluatorForm.hit_k,
      })
    } else {
      await createEvalEvaluator({
        name: evaluatorForm.name,
        datasetId: evaluatorForm.dataset_id,
        description: evaluatorForm.description,
        metrics: evaluatorForm.metrics,
        gevalCriteria: evaluatorForm.geval_criteria,
        hitK: evaluatorForm.hit_k,
      })
    }
    showCreateEvaluator.value = false
    await loadEvaluators()
  } catch (e) {
    errorMsg.value = errMsg(e, editingEvaluatorId.value ? '保存评估器失败' : '创建评估器失败')
  } finally {
    savingEvaluator.value = false
  }
}

async function removeEvaluator(ev) {
  if (!window.confirm(`确认删除评估器「${ev.name}」？评测集与运行历史不受影响。`)) return
  try {
    await deleteEvalEvaluator(ev.id)
    evaluators.value = evaluators.value.filter((x) => x.id !== ev.id)
    if (activeRun.value?.evaluator.id === ev.id) closeRun()
  } catch (e) {
    errorMsg.value = errMsg(e, '删除评估器失败')
  }
}

// ---- 页内运行监控 ----
const activeRun = ref(null)      // { evaluator, runId }
const runDetail = ref(null)      // getEvalRun 响应
const loadingRun = ref(false)
const startingRun = ref(false)
const expanded = ref(new Set())
let pollTimer = null

async function runEvaluator(ev) {
  if (!ev.dataset_exists) {
    errorMsg.value = '该评估器绑定的评测集已删除，无法运行'
    return
  }
  startingRun.value = true
  errorMsg.value = ''
  try {
    const res = await startEvalRun(ev.dataset_id, ev.id)
    activeRun.value = { evaluator: ev, runId: res.data.run_id }
    runDetail.value = null
    expanded.value = new Set()
    await loadRunDetail(true)
    startPolling()
  } catch (e) {
    if (e?.response?.status === 409) {
      errorMsg.value = e.response.data?.message || '已有评测任务在执行中，请等待其结束后再试'
    } else {
      errorMsg.value = errMsg(e, '发起评测失败')
    }
  } finally {
    startingRun.value = false
  }
}

function openRun(ev) {
  if (!ev.last_run) {
    runEvaluator(ev)
    return
  }
  activeRun.value = { evaluator: ev, runId: ev.last_run.id }
  expanded.value = new Set()
  loadRunDetail()
  if (ev.last_run.status === 'running') startPolling()
}

function closeRun() {
  stopPolling()
  activeRun.value = null
  runDetail.value = null
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => loadRunDetail(true), 2500)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function loadRunDetail(silent = false) {
  if (!activeRun.value?.runId) return
  if (!silent) loadingRun.value = true
  try {
    const res = await getEvalRun(activeRun.value.runId)
    runDetail.value = res.data
    if (res.data.run.status !== 'running') {
      stopPolling()
      loadEvaluators() // 刷新卡片上的最近运行状态
    }
  } catch (e) {
    if (!silent) errorMsg.value = errMsg(e, '加载运行详情失败')
  } finally {
    if (!silent) loadingRun.value = false
  }
}

function toggleExpand(id) {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

const progressText = computed(() => {
  if (!runDetail.value) return ''
  const done = runDetail.value.results?.length ?? 0
  const total = activeRun.value?.evaluator?.case_count ?? 0
  return total ? `${done} / ${total} 用例` : `${done} 用例`
})
const progressPct = computed(() => {
  const total = activeRun.value?.evaluator?.case_count ?? 0
  const done = runDetail.value?.results?.length ?? 0
  if (!total) return runDetail.value?.run.status === 'finished' ? '100%' : '0%'
  return Math.min(100, Math.round((done / total) * 100)) + '%'
})
const runSummaryCards = computed(() => {
  const s = runDetail.value?.run?.summary || {}
  if (s.total == null) return []
  const cards = [
    { label: '用例', value: s.total },
    { label: '成功', value: s.ok ?? 0 },
    { label: '失败', value: s.error ?? 0, cls: s.error ? 'bad' : '' },
    { label: '忠实度', value: pct(s.avg_faithfulness) },
    { label: '答案相关', value: pct(s.avg_answer_relevance) },
    { label: '上下文精确', value: pct(s.avg_context_precision) },
    { label: '上下文召回', value: pct(s.avg_context_recall) },
    { label: 'Hit@K', value: pct(s.avg_hit_at_k) },
    { label: 'MRR', value: s.avg_mrr != null ? Number(s.avg_mrr).toFixed(2) : '—' },
  ]
  // DeepEval 风格扩展指标：仅在评估器配置了对应指标时展示
  if (s.avg_hallucination != null) cards.push({ label: '幻觉率↓', value: pct(s.avg_hallucination) })
  if (s.avg_geval_score != null) cards.push({ label: 'GEval', value: pct(s.avg_geval_score) })
  cards.push({ label: '平均延迟', value: `${Math.round(s.avg_latency_ms || 0)}ms` })
  return cards
})

// ---- 在线采样评测（DeepEval 风格 · 后台自动采样，质量回流可观测性） ----
const onlineDays = ref(7)
const onlineAgg = ref(null)
const onlineBusiness = ref(null)
const onlineItems = ref([])
const onlineTotal = ref(0)
const onlineOffset = ref(0)
const loadingOnline = ref(false)
const onlineExpanded = ref(new Set())
const ONLINE_PAGE_SIZE = 50

async function loadOnline(reset = false) {
  if (reset) {
    onlineOffset.value = 0
    onlineExpanded.value = new Set()
  }
  loadingOnline.value = true
  try {
    const res = await getObsOnlineEval(onlineDays.value, ONLINE_PAGE_SIZE, onlineOffset.value)
    onlineAgg.value = res.data.aggregate || null
    onlineBusiness.value = res.data.business || null
    onlineItems.value = res.data.items || []
    onlineTotal.value = res.data.total || 0
  } catch (e) {
    errorMsg.value = errMsg(e, '加载在线评测失败')
  } finally {
    loadingOnline.value = false
  }
}

function onlinePage(dir) {
  const next = onlineOffset.value + dir * ONLINE_PAGE_SIZE
  if (next < 0 || next >= onlineTotal.value) return
  onlineOffset.value = next
  loadOnline()
}

function toggleOnlineExpand(id) {
  const next = new Set(onlineExpanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  onlineExpanded.value = next
}

// ---- 实验（跨评测集运行历史） ----
const allRuns = ref([])
const loadingRuns = ref(false)
const showCreateRun = ref(false)
const runFormDatasetId = ref('')
const creatingRun = ref(false)
let runsPollTimer = null

const filteredRuns = computed(() => {
  if (!keyword.value) return allRuns.value
  const kw = keyword.value.toLowerCase()
  return allRuns.value.filter((r) =>
    (r.dataset_name || '').toLowerCase().includes(kw) || (r.created_by || '').toLowerCase().includes(kw))
})

watch(tab, (t) => {
  if (domain.value !== 'rag') { stopRunsPolling(); return }
  if (t === 'experiments') loadRunsAll()
  else stopRunsPolling()
  if (t === 'online') loadOnline(true)
})

async function loadRunsAll() {
  if (!allRuns.value.length) loadingRuns.value = true
  try {
    const res = await listAllEvalRuns()
    allRuns.value = res.data.runs || []
    manageRunsPolling()
  } catch (e) {
    errorMsg.value = errMsg(e, '加载实验失败')
  } finally {
    loadingRuns.value = false
  }
}

function manageRunsPolling() {
  const running = allRuns.value.some((r) => r.status === 'running')
  if (running && !runsPollTimer) runsPollTimer = setInterval(loadRunsAll, 3000)
  else if (!running && runsPollTimer) stopRunsPolling()
}

function stopRunsPolling() {
  if (runsPollTimer) {
    clearInterval(runsPollTimer)
    runsPollTimer = null
  }
}

function openCreateRun() {
  runFormDatasetId.value = ''
  if (!datasets.value.length) loadDatasets()
  showCreateRun.value = true
}

async function createRun() {
  creatingRun.value = true
  errorMsg.value = ''
  try {
    await startEvalRun(runFormDatasetId.value)
    showCreateRun.value = false
    tab.value = 'experiments'
    await loadRunsAll()
  } catch (e) {
    errorMsg.value = e?.response?.status === 409
      ? (e.response.data?.message || '已有评测任务在执行中，请等待其结束后再试')
      : errMsg(e, '发起实验失败')
  } finally {
    creatingRun.value = false
  }
}

function goRun(r) {
  router.push({ name: 'EvalDataset', params: { datasetId: r.dataset_id }, query: { run: r.id } })
}

// ---- 格式化 ----
function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
function pct(v) {
  return v == null ? '-' : (v * 100).toFixed(0) + '%'
}
function scoreCls(v) {
  if (v == null) return 'na'
  if (v >= 0.8) return 'good'
  if (v >= 0.5) return 'mid'
  return 'low'
}
// 幻觉率反向指标：越低越好
function hallucCls(v) {
  if (v == null) return 'na'
  if (v <= 0.2) return 'good'
  if (v <= 0.5) return 'mid'
  return 'low'
}
function formatNum(n) {
  if (n == null || Number.isNaN(Number(n))) return '-'
  return Number(n).toLocaleString('en-US')
}
// MRR：首命中排名倒数，0/空显示「—」（无命中或无期望信号）
function mrrText(v) {
  return v ? Number(v).toFixed(2) : '—'
}
// 业务三率：无样本（null）显示「—」，否则百分比一位小数
function ratePct(v) {
  return v == null ? '—' : (v * 100).toFixed(1) + '%'
}
// 业务率着色（越低越好）：超阈值红 / 半阈值黄 / 其余绿；无样本灰
function rateCls(v, threshold) {
  if (v == null) return 'na'
  if (v > threshold) return 'bad'
  if (v > threshold / 2) return 'mid'
  return 'good'
}
function runStatusText(lastRun) {
  if (!lastRun) return '未运行'
  return `最近${statusLabel(lastRun.status)}`
}
function runChipCls(lastRun) {
  if (!lastRun) return 'muted'
  return { running: 'running', finished: 'ok', failed: 'bad', interrupted: 'muted' }[lastRun.status] || 'muted'
}
function runStatusCls(st) {
  return { running: 'running', finished: 'finished', failed: 'failed', interrupted: '' }[st] || ''
}
function statusLabel(st) {
  return { running: '执行中', finished: '已完成', failed: '失败', interrupted: '已中断' }[st] || st
}
</script>

<style scoped>
.eval-overview {
  height: 100vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(1200px 400px at 100% -10%, #fbf1e2 0%, transparent 60%),
    var(--color-bg);
}

/* ---- 顶部（图1：标题 + 分段页签 + 搜索/刷新/新建） ---- */
.eval-header {
  border-bottom: 1px solid var(--color-border);
  background: rgba(254, 252, 249, 0.86);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 20;
  pointer-events: none; /* 头部按钮均显式恢复 pointer-events，避免吸顶吞点击 */
}
.header-inner {
  padding: 18px 32px 0;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  flex-wrap: wrap;
}
.title-row { display: flex; align-items: center; gap: 20px; }
.page-title { font-size: 20px; font-weight: 700; margin: 0; letter-spacing: -0.3px; }
.seg-tabs {
  display: flex; gap: 2px; background: #f0ece6; border-radius: 9px; padding: 3px;
  pointer-events: auto;
}
.seg-tab {
  border: none; background: transparent; cursor: pointer; font-size: 13px; font-weight: 600;
  color: var(--color-text-secondary); padding: 6px 18px; border-radius: 7px; transition: all .15s;
}
.seg-tab:hover { color: var(--color-primary); }
.seg-tab.active { background: #cfe3fb; color: #1d4ed8; }

/* ---- 顶级域分段（RAG | Agent）+ 子页签行 ---- */
.domain-tabs {
  display: flex; gap: 2px; background: #f0ece6; border-radius: 9px; padding: 3px;
  pointer-events: auto;
}
.domain-tab {
  border: none; background: transparent; cursor: pointer; font-size: 13px; font-weight: 700;
  color: var(--color-text-secondary); padding: 6px 16px; border-radius: 7px; transition: all .15s;
}
.domain-tab:hover { color: var(--color-primary); }
.domain-tab.active { background: linear-gradient(135deg, #c98a4b, #d9a76a); color: #fff; box-shadow: 0 2px 6px rgba(201,138,75,0.25); }
.subtab-row { padding: 12px 32px 0; max-width: 1400px; width: 100%; margin: 0 auto; pointer-events: auto; box-sizing: border-box; }

/* ---- 在线业务三率 ---- */
.biz-wrap { margin-bottom: 14px; }
.biz-note { font-size: 11.5px; color: var(--color-text-secondary); line-height: 1.5; margin: 6px 0 0; }

/* ---- 构建测试集弹窗 ---- */
.build-tabs { display: flex; gap: 4px; background: #f0ece6; border-radius: 9px; padding: 3px; }
.build-tab {
  flex: 1; border: none; background: transparent; cursor: pointer; font-size: 12.5px; font-weight: 600;
  color: var(--color-text-secondary); padding: 7px 10px; border-radius: 7px; transition: all .15s;
}
.build-tab.active { background: #fff; color: var(--color-primary); box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.build-msg {
  font-size: 12px; line-height: 1.5; padding: 9px 12px; border-radius: 9px; margin: 0;
  background: #fdecea; color: #c0392b; border: 1px solid #f5c6c0;
}
.build-msg.ok { background: #e6f4ea; color: #2e7d46; border-color: #bfe0c8; }

/* ---- Agent 域 Prompt/Skill 嵌入（去掉工作台分隔线，作为页签主内容） ---- */
.workbench-section.agent-pse { margin-top: 0; border-top: none; padding-top: 0; }
.toolbar { display: flex; align-items: center; gap: 8px; pointer-events: auto; }
.search-input {
  border: 1px solid var(--color-border); background: #fff; border-radius: 9px;
  padding: 7px 12px; font-size: 12.5px; width: 200px; color: var(--color-text); outline: none;
}
.search-input:focus { border-color: var(--color-primary); }
.tool-btn {
  display: inline-flex; align-items: center; gap: 5px; border: 1px solid var(--color-border);
  background: #fff; border-radius: 9px; padding: 7px 12px; font-size: 12.5px; font-weight: 600;
  color: var(--color-text-secondary); cursor: pointer;
}
.tool-btn:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-primary); }
.tool-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.btn-primary-btn {
  border: none; background: #17171a; color: #fff; border-radius: 9px; padding: 8px 16px;
  font-size: 12.5px; font-weight: 600; cursor: pointer;
}
.btn-primary-btn:hover:not(:disabled) { background: #2c2c31; }
.btn-primary-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.header-sub {
  margin: 10px auto 14px; padding: 0 32px; max-width: 1400px;
  font-size: 12px; color: var(--color-text-secondary); line-height: 1.5;
}

.overview-body {
  flex: 1; padding: 20px 32px 48px; max-width: 1400px; width: 100%; margin: 0 auto;
  box-sizing: border-box;
}
.inline-error {
  background: #fdecea; border: 1px solid #f5c6c0; color: #c0392b; font-size: 12.5px;
  padding: 9px 13px; border-radius: 10px; margin-bottom: 12px;
}

/* ---- 卡片格（图1） ---- */
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.entry-card {
  background: #fff; border: 1px solid var(--color-border); border-radius: 12px;
  padding: 18px 20px; cursor: pointer; transition: all 0.16s;
  display: flex; flex-direction: column; gap: 10px;
}
.entry-card:hover { border-color: var(--color-primary); box-shadow: 0 4px 16px rgba(201,138,75,0.12); }
.entry-card.focused { border-color: #7fb3f2; box-shadow: 0 0 0 1px #7fb3f2; }
.entry-card-head { display: flex; align-items: center; gap: 10px; }
.entry-icon {
  width: 36px; height: 36px; border-radius: 9px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center; color: #fff;
}
.entry-icon.ds { background: #f0762b; }
.entry-icon.ds.tool-icon { background: #2e9e5b; }
.entry-icon.ev { background: #2563eb; }
.entry-name {
  flex: 1; min-width: 0; font-size: 14.5px; font-weight: 700;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.card-actions { display: flex; gap: 2px; }
.icon-btn {
  width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center;
  border: none; background: transparent; border-radius: 7px; cursor: pointer;
  color: var(--color-text-secondary); transition: all 0.15s;
}
.icon-btn:hover { background: #f0e7da; color: var(--color-primary); }
.icon-btn.danger:hover { background: #fdecea; color: #c0392b; }
.entry-desc {
  margin: 0; font-size: 12.5px; color: var(--color-text-secondary); line-height: 1.55;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.entry-tags { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.entry-foot { display: flex; align-items: center; gap: 8px; margin-top: 2px; }
.tag-chip {
  background: #f3e8fa; color: #8a4bb8; font-size: 11px; font-weight: 600;
  padding: 2px 9px; border-radius: 6px;
}
.tag-chip.muted { background: #f0ece6; color: var(--color-text-secondary); font-weight: 500; }
.tag-chip.ok { background: #e6f4ea; color: #2e7d46; }
.tag-chip.bad { background: #fdecea; color: #c0392b; }
.tag-chip.running { background: #fdf0d8; color: #b5822a; }
.tag-chip.ds-chip { background: #e3edfd; color: #1d4ed8; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-pill {
  font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 20px; white-space: nowrap;
  background: #ede6dc; color: var(--color-text-secondary);
}
.status-pill.sm { padding: 2px 8px; }
.status-pill.running { background: #fdf0d8; color: #b5822a; }
.status-pill.finished { background: #e6f4ea; color: #2e7d46; }
.status-pill.failed { background: #fdecea; color: #c0392b; }

/* ---- 运行按钮 ---- */
.run-btn {
  display: inline-flex; align-items: center; gap: 5px; border: none; cursor: pointer;
  background: #17171a; color: #fff; border-radius: 8px; padding: 6px 14px;
  font-size: 12px; font-weight: 600; transition: background 0.15s;
}
.run-btn:hover:not(:disabled) { background: #2c2c31; }
.run-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.run-btn.sm { padding: 4px 10px; font-size: 11.5px; }

/* ---- 页内运行监控 ---- */
.run-monitor {
  background: #fff; border: 1px solid var(--color-border); border-radius: 14px;
  padding: 16px 18px; margin-bottom: 16px;
  border-left: 3px solid #2563eb;
}
.monitor-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.monitor-title {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 14px; font-weight: 700; color: #1d4ed8;
}
.monitor-progress-text { font-size: 12px; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; }
.monitor-actions { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; }
.progress-track { height: 6px; background: #f0ece6; border-radius: 4px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #4f8ef7, #2563eb); border-radius: 4px; transition: width 0.4s; }
.progress-fill.done { background: #2e9e5b; }
.monitor-metrics { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.monitor-metric {
  background: #faf7f2; border-radius: 9px; padding: 8px 12px; min-width: 74px;
  display: flex; flex-direction: column; align-items: center; gap: 1px;
}
.mm-value { font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; }
.mm-value.bad { color: #c0392b; }
.mm-value.good { color: #2e7d46; }
.mm-value.mid { color: #b5822a; }
.mm-value.low { color: #c0392b; }
.mm-label { font-size: 10.5px; color: var(--color-text-secondary); }
.monitor-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }
.monitor-table th, .monitor-table td {
  padding: 7px 8px; text-align: left; border-bottom: 1px solid var(--color-border);
  color: var(--color-text); vertical-align: top;
}
.monitor-table th { color: var(--color-text-secondary); font-weight: 600; white-space: nowrap; }
.monitor-table .result-row { cursor: pointer; }
.monitor-table .result-row:hover { background: var(--color-primary-light, #fbf1e2); }
.q-cell { line-height: 1.5; }
.exp-caret { display: inline-block; margin-right: 5px; color: var(--color-text-secondary); transition: transform 0.15s; }
.exp-caret.open { transform: rotate(90deg); }
.score { font-weight: 700; font-variant-numeric: tabular-nums; }
.score.good { color: #2e7d46; }
.score.mid { color: #b5822a; }
.score.low { color: #c0392b; }
.score.na { color: var(--color-text-secondary); font-weight: 500; }
.expand-row td { background: #fbf8f3; }
.expand-inner { display: flex; flex-direction: column; gap: 8px; padding: 4px 2px; }
.err-line { color: #c0392b; font-size: 12px; }
.expand-block { display: flex; flex-direction: column; gap: 4px; }
.expand-title { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); }
.answer-box {
  background: #fff; border: 1px solid var(--color-border); border-radius: 8px;
  padding: 8px 10px; font-size: 12px; line-height: 1.6; white-space: pre-wrap;
  max-height: 180px; overflow-y: auto;
}
.empty-hint { font-size: 12px; color: var(--color-text-secondary); margin: 10px 0 0; }

/* ---- 页内整合工作台 ---- */
.workbench-section {
  margin-top: 26px; border-top: 1px dashed var(--color-border); padding-top: 18px;
}
.workbench-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-bottom: 12px; flex-wrap: wrap;
}
.workbench-title { font-size: 14px; font-weight: 700; display: block; }
.workbench-sub { font-size: 11.5px; color: var(--color-text-secondary); display: block; margin-top: 2px; }

/* ---- 实验表格 ---- */
.exp-table {
  width: 100%; border-collapse: collapse; font-size: 12.5px;
  background: #fff; border: 1px solid var(--color-border); border-radius: 12px; overflow: hidden;
}
.exp-table th, .exp-table td { padding: 9px 10px; text-align: left; border-bottom: 1px solid var(--color-border); }
.exp-table th { background: rgba(0,0,0,0.03); font-weight: 600; color: var(--color-text-secondary); white-space: nowrap; }
.exp-table tbody tr:last-child td { border-bottom: none; }
.exp-row { cursor: pointer; }
.exp-row:hover { background: #fdfaf5; }
.exp-ds { font-weight: 600; }
.cell-time { white-space: nowrap; color: var(--color-text-secondary); }

/* ---- 在线评测页签 ---- */
.online-toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.online-days { width: 130px; flex: none; }
.online-note { font-size: 12px; color: var(--color-text-secondary); line-height: 1.5; }
.online-metrics { margin: 0 0 14px; }
.online-table { margin-top: 0; }
.online-q { line-height: 1.55; }
.online-pager { display: flex; align-items: center; justify-content: center; gap: 12px; margin-top: 14px; }
.online-page-text { font-size: 12px; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; }

/* ---- 评估器指标配置（DeepEval 风格） ---- */
.tag-chip.metric-chip { background: #e3edfd; color: #1d4ed8; font-weight: 600; }
.metric-checks { display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 8px 10px; border: 1px solid var(--color-border); border-radius: 9px; background: #fffdfa; }
.metric-check { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; cursor: pointer; color: var(--color-text); }
.metric-check input { accent-color: #2563eb; cursor: pointer; }
.metric-check em { font-style: normal; font-size: 11px; color: var(--color-text-secondary); margin-left: 3px; }

/* ---- 空/加载 ---- */
.mini-loading { padding: 26px; text-align: center; color: var(--color-text-secondary); font-size: 12.5px; }
.empty-block {
  border: 1px dashed var(--color-border); border-radius: 14px; padding: 40px 16px;
  text-align: center; color: var(--color-text-secondary); font-size: 12.5px;
  display: flex; flex-direction: column; gap: 12px; align-items: center; background: rgba(255,255,255,0.5);
}
.outline-btn {
  border: 1px solid var(--color-border); background: #fff; color: var(--color-text-secondary);
  border-radius: 9px; padding: 7px 14px; font-size: 12.5px; font-weight: 600; cursor: pointer;
}
.outline-btn.sm { padding: 5px 11px; font-size: 12px; }
.outline-btn:hover { border-color: var(--color-primary); color: var(--color-primary); }

/* ---- 弹窗 / 表单 ---- */
.modal-overlay { position: fixed; inset: 0; background: rgba(45,42,36,0.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal-box { width: 440px; max-width: 92vw; background: #fff; border-radius: 16px; padding: 22px 24px; box-shadow: 0 16px 48px rgba(0,0,0,0.2); display: flex; flex-direction: column; gap: 12px; }
.modal-title { font-size: 16px; font-weight: 700; margin-bottom: 2px; }
.modal-sub { margin: -4px 0 0; font-size: 12px; color: var(--color-text-secondary); line-height: 1.55; }
.modal-sub.warn { color: #c0392b; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field-label { font-size: 11.5px; font-weight: 600; color: var(--color-text-secondary); }
.field-label em { color: #c0392b; font-style: normal; }
.input {
  width: 100%; padding: 9px 11px; font-size: 13px; border: 1px solid var(--color-border);
  border-radius: 9px; background: #fffdfa; color: var(--color-text); outline: none; transition: border-color 0.15s;
  font-family: inherit; box-sizing: border-box;
}
.input:focus { border-color: var(--color-primary); background: #fff; }
.textarea { resize: vertical; line-height: 1.55; }
select.input { appearance: auto; background: #fff; }

/* ---- 响应式 ---- */
@media (max-width: 768px) {
  .eval-header { padding-top: 44px; }
  .header-inner { padding: 16px 16px 0; }
  .title-row { flex-wrap: wrap; gap: 10px; }
  .subtab-row { padding: 12px 16px 0; }
  .seg-tabs { flex-wrap: wrap; }
  .header-sub { padding: 0 16px; }
  .overview-body { padding: 16px 16px 40px; }
  .toolbar { width: 100%; }
  .search-input { flex: 1; width: auto; }
}
</style>
