<template>
  <div class="guide-page">
    <!-- 顶部导航 -->
    <header class="guide-header">
      <div class="guide-header-inner">
        <div class="guide-brand">
          <div class="guide-logo">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2z"/>
              <path d="M12 6v6l4 2" stroke-linecap="round"/>
              <path d="M8 12c0 2.21 1.79 4 4 4" stroke-linecap="round"/>
            </svg>
          </div>
          <span class="guide-brand-name">见字如面</span>
        </div>
        <router-link to="/login" class="btn-back">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
            <line x1="19" y1="12" x2="5" y2="12"/>
            <polyline points="12 19 5 12 12 5"/>
          </svg>
          返回登录
        </router-link>
      </div>
    </header>

    <main class="guide-main">
      <!-- 封面 -->
      <section class="guide-hero">
        <h1 class="hero-title">系统使用指南</h1>
        <p class="hero-desc">
          「见字如面」是一款手写笔记知识库问答系统：把你的手写笔记拍照上传，
          系统会自动识别成文字并存入知识库，之后你就可以像聊天一样向它提问，
          它会基于你的笔记给出带来源引用的回答。你还可以直接把图片、Word、PDF
          拖进聊天框，系统会自动识别、总结或帮你存入知识库；系统还会记住你的偏好，
          越用越懂你。
        </p>
        <p class="hero-tip">第一次使用？按下面 4 步走，5 分钟上手 ↓</p>
      </section>

      <!-- 快速上手 -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">0</span>
          快速上手：4 步用起来
        </h2>
        <div class="quick-steps">
          <div class="quick-step">
            <div class="quick-step-num">1</div>
            <h3>注册并登录</h3>
            <p>在登录页点击「立即注册」，填写用户名、密码和联系方式即可。</p>
          </div>
          <div class="quick-step">
            <div class="quick-step-num">2</div>
            <h3>上传第一篇笔记</h3>
            <p>点击左侧「上传笔记」，把手写笔记拍照上传，系统自动识别文字。</p>
          </div>
          <div class="quick-step">
            <div class="quick-step-num">3</div>
            <h3>确认并保存</h3>
            <p>检查识别结果，需要修改就直接编辑，然后点击「保存到知识库」。</p>
          </div>
          <div class="quick-step">
            <div class="quick-step-num">4</div>
            <h3>开始提问</h3>
            <p>回到对话页面，直接输入问题，系统会基于你的笔记给出回答，并自动将笔记编译为百科词条提升回答质量。</p>
          </div>
        </div>
      </section>

      <!-- 注册登录 -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">1</span>
          注册、登录与游客体验
        </h2>
        <div class="card">
          <h3 class="card-title">注册账号</h3>
          <ol class="step-list">
            <li>打开系统登录页，点击「立即注册」</li>
            <li>填写用户名（至少 2 个字符）、密码（至少 6 位）、联系方式（手机号或邮箱）</li>
            <li>点击「注册」，成功后自动跳转到登录页，用刚注册的账号登录即可</li>
          </ol>
          <div class="callout callout-info">
            提示：如果提示「注册功能已关闭」，说明管理员暂停了开放注册，请联系管理员开通。
          </div>
        </div>
        <div class="card">
          <h3 class="card-title">游客模式</h3>
          <p class="card-text">
            不想注册也可以先体验：登录页点击「以游客身份体验」，可以直接进入对话页面提问。
            游客有一定的免费使用次数限制，且无法上传笔记；想要完整功能请注册账号。
          </p>
          <div class="callout callout-tip">
            小技巧：在聊天框里拖入一张图片，即使不上传也能立刻体验 AI 识别与总结（游客仅支持识别/总结，不会写入知识库）。
          </div>
        </div>
      </section>

      <!-- 上传笔记 -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">2</span>
          上传笔记：把手写内容变成知识
        </h2>
        <div class="card">
          <h3 class="card-title">支持三种方式导入</h3>
          <div class="feature-grid">
            <div class="feature-item">
              <div class="feature-icon">📷</div>
              <h4>图片 OCR</h4>
              <p>拍摄手写笔记上传，系统自动识别图片中的文字（支持 JPG、PNG 等常见格式）。</p>
            </div>
            <div class="feature-item">
              <div class="feature-icon">📄</div>
              <h4>Word 导入</h4>
              <p>直接上传 .doc / .docx 文件，系统按页提取文字内容。</p>
            </div>
            <div class="feature-item">
              <div class="feature-icon">📑</div>
              <h4>PDF 导入</h4>
              <p>上传 PDF 文件，系统按页提取文字内容，可自选哪些页入库。</p>
            </div>
          </div>
        </div>

        <div class="card">
          <h3 class="card-title">图片上传的完整流程</h3>
          <ol class="step-list">
            <li><b>选择知识库</b>：先选「公共知识库」或「个人知识库」，再选择具体的目标库。公共库所有人可见；个人库只有你自己能看到，你也可以新建属于自己的知识库。</li>
            <li><b>选择 OCR 引擎</b>：上传界面可自由切换识别引擎（本地离线 / 云端高精度等），不同引擎适合不同字迹，可按识别效果随时换。</li>
            <li><b>上传图片</b>：点击上传区域选择图片，或直接拖拽进去；支持一次选择多张图片批量处理。</li>
            <li><b>自动识别</b>：系统用 OCR 识别图片文字，结果显示在右侧编辑框。</li>
            <li><b>编辑与润色</b>：识别结果可以直接在文本框里修改；也可以点击「一键润色」，让 AI 帮你理顺语句。<b>最终保存的就是你编辑/润色后的文字</b>。</li>
            <li><b>自定义标题</b>：保存前可自定义知识文档的标题（默认自动填充文件名），便于后续按标题精准搜索。</li>
            <li><b>保存图片到知识库</b>：点击保存按钮，文字会被切分成小段并建立索引，之后就能被检索到了；批量上传时可逐条确认后一键全部入库。</li>
          </ol>
          <div class="callout callout-tip">
            小技巧：预览图片时，可以长按图片拖动查看细节、滚轮/双指缩放、旋转图片，方便核对识别结果。
          </div>
        </div>

        <div class="card">
          <h3 class="card-title">Word / PDF 导入流程</h3>
          <ol class="step-list">
            <li>选择 Word 或 PDF 方式上传文件</li>
            <li>系统解析出每一页的文字，进入分页预览界面</li>
            <li>逐页检查：不需要的页可以取消勾选，文字有误差可以直接编辑该页内容</li>
            <li>点击保存，勾选的页面文字全部存入知识库</li>
          </ol>
          <div class="callout callout-tip">
            文档中的表格会自动解析为 Markdown 格式保留结构，入库后检索和展示都不会错乱；Word 文档还会生成与原始排版一致的预览版本，方便后续对照原文。
          </div>
        </div>
      </section>

      <!-- 智能问答 -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">3</span>
          智能问答：向你的笔记提问
        </h2>
        <div class="card">
          <h3 class="card-title">怎么提问</h3>
          <ol class="step-list">
            <li>点击左侧「新对话」，或直接进入对话页面</li>
            <li>在底部输入框输入问题，回车或点击发送</li>
            <li>回答会以打字机效果逐字显示，无需等待完整生成</li>
          </ol>
        </div>
        <div class="card">
          <h3 class="card-title">输入框上方的两个开关</h3>
          <div class="feature-grid">
            <div class="feature-item">
              <div class="feature-icon">📚</div>
              <h4>知识检索</h4>
              <p>开启后，系统先在你的知识库中查找相关内容，再结合笔记作答。回答会附带来源引用，可展开查看原文和原始图片。检索采用「关键词 + 语义」双路混合，专有名词和语义相近的问法都能找到。</p>
            </div>
            <div class="feature-item">
              <div class="feature-icon">🌐</div>
              <h4>联网搜索</h4>
              <p>开启后，系统会搜索互联网上的最新信息辅助回答，适合笔记里没有覆盖的问题。</p>
            </div>
          </div>
          <div class="callout callout-info">
            两个开关可以任意组合：只查笔记、只联网、两者结合，或都不开（纯靠 AI 自身知识）。
          </div>
        </div>
        <div class="card">
          <h3 class="card-title">回答下方可以做什么</h3>
          <ul class="bullet-list">
            <li><b>查看来源</b>：展开知识来源，能看到回答依据的原文片段和笔记原图</li>
            <li><b>跳转原文</b>：来源是 Word / PDF 时，可点击「在线预览（第 N 页）」直接跳转到原文档对应页面，对照上下文核验</li>
            <li><b>收藏</b>：觉得回答有用，点击收藏按钮存到「知识收藏」，还可以加标签</li>
            <li><b>点赞 / 点踩</b>：对回答质量给出反馈，帮助系统改进</li>
          </ul>
        </div>
        <div class="card">
          <h3 class="card-title">对话历史与会话摘要</h3>
          <p class="card-text">
            每次对话都会自动保存在左侧「对话历史」中，按日期分组。
            点击任意历史会话即可继续当时的对话；不需要的会话可将鼠标悬停后点击删除图标。
            每个人只能看到自己的对话记录。
            较长的会话系统会自动压缩成摘要，重新打开时上下文不会丢失，还能加快响应速度。
          </p>
        </div>
      </section>

      <!-- 聊天框附件 -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">4</span>
          聊天框附件：随手拖入，智能处理
        </h2>
        <div class="card">
          <h3 class="card-title">三种方式添加附件</h3>
          <p class="card-text">
            在对话页面，你可以<b>拖拽文件到输入区</b>、<b>直接粘贴截图</b>，
            或点击输入框左侧的<b>回形针图标</b>选择文件。支持图片（JPG、PNG 等）、Word（.docx）和 PDF，
            添加后会在输入框上方显示文件卡片，可逐个移除；单个文件不超过 20MB 即可。
            发送后点击消息中的附件卡片，即可<b>在新页面预览原图或文档内容</b>，也可以下载原文件。
          </p>
        </div>
        <div class="card">
          <h3 class="card-title">系统根据你的话语自动选择处理方式</h3>
          <div class="feature-grid">
            <div class="feature-item">
              <div class="feature-icon">👀</div>
              <h4>只识别 / 只总结</h4>
              <p>直接发文件或说「识别这张图片」「总结这个 PDF」：识别/提取内容后直接回答，<b>不会写入知识库</b>。</p>
            </div>
            <div class="feature-item">
              <div class="feature-icon">📥</div>
              <h4>存入知识库</h4>
              <p>说「把这张图片上传到知识库」「保存这份文档」：识别/解析后自动存入你当前选择的知识库，之后可直接提问。</p>
            </div>
            <div class="feature-item">
              <div class="feature-icon">✨</div>
              <h4>总结提炼</h4>
              <p>说「总结一下这个文档的重点」：AI 生成结构化摘要回复给你，默认不入库。</p>
            </div>
          </div>
          <div class="callout callout-info">
            不说明意图时默认「只处理不入库」，不会意外写入你的知识库；想入库请在话语中带上「上传 / 保存 / 入库」。
          </div>
          <div class="callout callout-tip">
            游客拖入附件时仅支持识别与总结，不会写入知识库；登录用户附件处理完成后，还可以追问「把它保存到知识库」完成入库。
          </div>
        </div>
      </section>

      <!-- 用户记忆 -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">5</span>
          用户记忆：越用越懂你
        </h2>
        <div class="card">
          <p class="card-text">
            系统会在对话过程中自动记住与你相关的重要信息（如你的称呼偏好、常用语言、关注的主题），
            之后的每次新对话都会参考这些记忆，回答更贴合你的习惯。
            记忆仅属于你本人，对其他用户不可见；长会话也会自动压缩成摘要保存，重新打开时上下文不丢失。
          </p>
        </div>
      </section>

      <!-- 知识库管理 -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">6</span>
          知识库管理：查看和维护你的知识
        </h2>
        <div class="card">
          <ol class="step-list">
            <li><b>进入页面</b>：点击左侧「知识库管理」</li>
            <li><b>两级筛选</b>：先选择「公共知识库」或「个人知识库」，再选择具体的知识库，下方列表只显示对应范围内的知识条目</li>
            <li><b>浏览条目</b>：每条知识显示文字内容、所属知识库和来源图片缩略图，点击图片可放大查看</li>
            <li><b>搜索知识</b>：输入关键词时标题/文件名实时过滤；按回车键可触发语义检索，深入内容层面找出措辞不同但意思相近的条目</li>
            <li><b>删除条目</b>：属于你个人知识库中的条目，你可以自行删除；公共库条目由管理员维护</li>
          </ol>
          <div class="callout callout-tip">
            权限说明：你始终只能看到公共知识库 + 属于你自己的个人知识库，其他人的私人内容对你是不可见的。
          </div>
        </div>
      </section>

      <!-- 知识编译（llm-wiki） -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">7</span>
          知识编译：系统自动帮你整理笔记（llm-wiki）
        </h2>
        <div class="card">
          <p class="card-text">
            笔记保存成功后，系统会在后台自动把资料「编译」成结构化的百科词条，
            包含<b>概述、关键要点、相关概念、原文精选</b>等部分。之后提问时，
            检索会优先命中这些百科卡片，让回答更完整、更有条理。
          </p>
          <ul class="bullet-list">
            <li><b>自动生效</b>：编译默认开启、全程后台进行，无需任何操作；内容过短的笔记会跳过编译，仍参与普通检索</li>
            <li><b>知识百科页</b>：点击左侧「知识百科」，可浏览所有可见词条——包括你个人知识库编译出的私人词条；支持按类型/知识库筛选、查看 Markdown 详情、删除自己的词条</li>
            <li><b>可见范围</b>：词条与原始资料权限一致——公共库的词条全员可见，私人笔记的词条仅属主与管理员可见</li>
            <li><b>来源可跳回</b>：问答来源中的百科卡片会标注词条标题，点击仍可跳转到原始文档/图片/页码</li>
          </ul>
          <div class="callout callout-tip">
            编译需要几十秒到几分钟。若笔记保存后暂未出现词条，可在系统设置页确认「知识编译」开关状态，或用「存量补编译」一键补齐。
          </div>
        </div>
      </section>

      <!-- 知识收藏 -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">8</span>
          知识收藏：沉淀有价值的内容
        </h2>
        <div class="card">
          <ul class="bullet-list">
            <li>在问答页面，对 AI 回答或知识来源片段点击「收藏」，即可存入个人收藏夹</li>
            <li>收藏时可以添加自定义标签（如「工作」「学习」），方便分类</li>
            <li>在左侧「知识收藏」页面浏览全部收藏，支持关键词搜索和按标签筛选</li>
            <li><b>点击卡片查看详情</b>：弹窗展示收藏全文（Markdown 渲染，含表格）、标签与收藏时间</li>
            <li><b>来源跳转</b>：来自 Word / PDF 的收藏会保留页码，详情中可直接跳转到原文档对应页；图片类来源则展示原图</li>
            <li>收藏内容仅自己可见，可随时删除</li>
          </ul>
        </div>
      </section>

      <!-- 个人中心 -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">9</span>
          个人中心：修改信息与使用看板
        </h2>
        <div class="card">
          <p class="card-text">
            点击页面左下角的<b>用户名</b>，会弹出功能菜单：
          </p>
          <div class="feature-grid">
            <div class="feature-item">
              <div class="feature-icon">👤</div>
              <h4>修改个人信息</h4>
              <p>修改用户名、联系方式；如需改密码，填写原密码和新密码即可。</p>
            </div>
            <div class="feature-item">
              <div class="feature-icon">📊</div>
              <h4>查看使用情况</h4>
              <p>使用看板展示你的提问次数、知识库检索次数、联网搜索次数、上传笔记数、Token 消耗、活跃天数等，支持近 7/30/90 天切换，还有每日趋势图。</p>
            </div>
          </div>
        </div>
        <div class="card">
          <h3 class="card-title">退出登录</h3>
          <p class="card-text">
            点击左下角的退出图标，确认后即可退出账号。
          </p>
        </div>
      </section>

      <!-- FAQ -->
      <section class="guide-section">
        <h2 class="section-title">
          <span class="section-num">10</span>
          常见问题
        </h2>
        <div class="faq-list">
          <details class="faq-item" open>
            <summary>OCR 识别的文字有错别字怎么办？</summary>
            <p>识别结果在保存前可以自由编辑，直接修改后再保存即可；也可以点击「一键润色」让 AI 帮忙整理语句。保存进知识库的就是你最终确认的文字。</p>
          </details>
          <details class="faq-item">
            <summary>为什么提问时系统说知识库里没有相关内容？</summary>
            <p>请先确认「知识检索」开关已开启，且你的问题对应的笔记确实已经上传并保存成功。系统采用关键词 + 语义双路混合检索，换个问法通常也能命中；若仍未找到，可开启「联网搜索」获取外部信息。</p>
          </details>
          <details class="faq-item">
            <summary>拖进聊天框的文件会自动存进知识库吗？</summary>
            <p>不会。默认只做识别或总结，不会写入知识库；只有当你的话语中明确包含「上传 / 保存 / 入库」等意图时，系统才会把处理结果存入你当前选择的知识库。</p>
          </details>
          <details class="faq-item">
            <summary>系统记住的「用户记忆」是什么？别人能看到吗？</summary>
            <p>是系统在对话中自动提取的与你相关的偏好和关键信息，仅用于让回答更贴合你，仅本人可见。长会话也会被自动压缩成摘要保存，重新打开时上下文不会丢失。</p>
          </details>
          <details class="faq-item">
            <summary>个人知识库里的内容别人能看到吗？</summary>
            <p>不能。个人知识库仅本人可见，检索时也只会检索公共库和你自己的私人库。</p>
          </details>
          <details class="faq-item">
            <summary>上传的笔记为什么没有出现百科词条？</summary>
            <p>知识编译在保存成功后后台异步执行，通常需几十秒到几分钟。若长时间未出现：可能是笔记内容过短（不足编译门槛会跳过）、编译开关被关闭，或 LLM 暂时限流失败——前两种可在系统设置「llm-wiki 知识编译」区块确认开关并用「存量补编译」补齐，第三种稍后重试即可，均不影响原始资料与正常检索。</p>
          </details>
          <details class="faq-item">
            <summary>游客模式有什么限制？</summary>
            <p>游客可以免费体验对话提问和聊天框附件识别/总结，但有次数限制，且不能把内容存入知识库、使用收藏等完整功能。注册账号即可解锁全部功能。</p>
          </details>
          <details class="faq-item">
            <summary>忘记登录状态了，需要重新登录吗？</summary>
            <p>登录状态默认会保持一段时间，关闭浏览器甚至重启服务后再打开通常无需重新登录；若提示登录已过期，重新登录即可，历史数据不会丢失。</p>
          </details>
        </div>
      </section>

      <footer class="guide-footer">
        见字如面 — 文墨凝思，星辉入梦
      </footer>
    </main>
  </div>
</template>

<script setup>
</script>

<style scoped>
.guide-page {
  /* 父容器 .main-content 为 100vh + overflow:hidden，页面需自身承担滚动 */
  height: 100vh;
  overflow-y: auto;
  background: linear-gradient(135deg, #f5f0ea 0%, #ede6dc 50%, #e6ddd2 100%);
}

/* ---- 顶部导航 ---- */
.guide-header {
  position: sticky;
  top: 0;
  z-index: 10;
  background: rgba(255, 255, 255, 0.85);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid #e0d8cc;
}

.guide-header-inner {
  max-width: 860px;
  margin: 0 auto;
  padding: 12px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.guide-brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.guide-logo {
  width: 34px;
  height: 34px;
  background: linear-gradient(135deg, #c98a4b, #e9bc7d);
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
}

.guide-logo svg {
  width: 20px;
  height: 20px;
}

.guide-brand-name {
  font-size: 16px;
  font-weight: 700;
  color: #3a2e24;
}

.btn-back {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border-radius: 8px;
  border: 1px solid #ddd5cb;
  background: white;
  color: #5a4a3a;
  font-size: 13px;
  text-decoration: none;
  transition: all 0.2s;
}

.btn-back:hover {
  border-color: #c98a4b;
  color: #c98a4b;
}

/* ---- 主体 ---- */
.guide-main {
  max-width: 860px;
  margin: 0 auto;
  padding: 32px 20px 60px;
}

.guide-hero {
  text-align: center;
  margin-bottom: 40px;
}

.hero-title {
  font-size: 30px;
  font-weight: 700;
  color: #3a2e24;
  margin-bottom: 14px;
}

.hero-desc {
  font-size: 15px;
  line-height: 1.8;
  color: #5a4a3a;
  max-width: 640px;
  margin: 0 auto 12px;
}

.hero-tip {
  font-size: 13px;
  color: #c98a4b;
  font-weight: 600;
}

/* ---- 章节 ---- */
.guide-section {
  margin-bottom: 40px;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 20px;
  font-weight: 700;
  color: #3a2e24;
  margin-bottom: 16px;
}

.section-num {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: linear-gradient(135deg, #c98a4b, #d9a76a);
  color: white;
  font-size: 15px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

/* ---- 快速上手 ---- */
.quick-steps {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}

.quick-step {
  background: white;
  border-radius: 14px;
  padding: 18px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04);
}

.quick-step-num {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: #faf3e8;
  color: #c98a4b;
  font-weight: 700;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 10px;
}

.quick-step h3 {
  font-size: 14px;
  font-weight: 700;
  color: #3a2e24;
  margin-bottom: 6px;
}

.quick-step p {
  font-size: 12px;
  line-height: 1.6;
  color: #8c7e6e;
}

/* ---- 卡片 ---- */
.card {
  background: white;
  border-radius: 14px;
  padding: 20px 22px;
  margin-bottom: 14px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04);
}

.card-title {
  font-size: 15px;
  font-weight: 700;
  color: #3a2e24;
  margin-bottom: 12px;
}

.card-text {
  font-size: 13px;
  line-height: 1.9;
  color: #5a4a3a;
}

.step-list {
  margin: 0;
  padding-left: 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.step-list li {
  font-size: 13px;
  line-height: 1.8;
  color: #5a4a3a;
}

.step-list li b {
  color: #3a2e24;
}

.bullet-list {
  margin: 0;
  padding-left: 20px;
  list-style: disc;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.bullet-list li {
  font-size: 13px;
  line-height: 1.8;
  color: #5a4a3a;
}

.bullet-list li b {
  color: #3a2e24;
}

/* ---- 功能网格 ---- */
.feature-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}

.feature-item {
  background: #faf8f5;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 14px 16px;
}

.feature-icon {
  font-size: 20px;
  margin-bottom: 8px;
}

.feature-item h4 {
  font-size: 13px;
  font-weight: 700;
  color: #3a2e24;
  margin-bottom: 6px;
}

.feature-item p {
  font-size: 12px;
  line-height: 1.7;
  color: #8c7e6e;
}

/* ---- 提示框 ---- */
.callout {
  margin-top: 12px;
  padding: 10px 14px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 1.7;
}

.callout-info {
  background: #f0f4fa;
  color: #4a5a7a;
}

.callout-tip {
  background: #faf3e8;
  color: #a06a20;
}

/* ---- FAQ ---- */
.faq-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.faq-item {
  background: white;
  border-radius: 12px;
  padding: 0 18px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.04);
}

.faq-item summary {
  padding: 14px 0;
  font-size: 13px;
  font-weight: 600;
  color: #3a2e24;
  cursor: pointer;
  list-style: none;
  position: relative;
  padding-right: 24px;
}

.faq-item summary::after {
  content: '+';
  position: absolute;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  color: #c98a4b;
  font-size: 16px;
}

.faq-item[open] summary::after {
  content: '−';
}

.faq-item p {
  padding: 0 0 14px;
  font-size: 13px;
  line-height: 1.8;
  color: #8c7e6e;
}

/* ---- 页脚 ---- */
.guide-footer {
  text-align: center;
  font-size: 12px;
  color: #b0a494;
  padding-top: 20px;
  border-top: 1px dashed #ddd5cb;
}

@media (max-width: 640px) {
  .hero-title {
    font-size: 24px;
  }

  .guide-main {
    padding: 24px 14px 48px;
  }
}
</style>
