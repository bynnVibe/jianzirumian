<template>
  <div class="pse-view">
    <!-- 顶部标题栏：返回总览 -->
    <header class="eval-header">
      <div class="header-inner">
        <button class="back-btn" @click="goOverview">← 返回回归评测</button>
        <div class="header-title">
          <span class="header-eyebrow">Prompt / Skill</span>
          <h1>Prompt / Skill 专项评测</h1>
          <p class="header-sub">
            对系统提示词（Prompt）与用户技能（Skill）做专项评测：静态断言 + Rubric 多维打分 + A/B 行为对比，据结果一键优化并上线
          </p>
        </div>
      </div>
    </header>

    <div class="pse-wrap">
      <PromptSkillEval />
    </div>
  </div>
</template>

<script setup>
import { useRouter } from 'vue-router'
import PromptSkillEval from '@/components/PromptSkillEval.vue'

const router = useRouter()

function goOverview() {
  router.push({ name: 'EvalOverview' })
}
</script>

<style scoped>
.pse-view {
  height: 100vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(1200px 400px at 100% -10%, #fbf1e2 0%, transparent 60%),
    var(--color-bg);
}
.eval-header {
  border-bottom: 1px solid var(--color-border);
  background: rgba(254, 252, 249, 0.86);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 20;
}
.header-inner { padding: 16px 32px 18px; max-width: 1400px; width: 100%; margin: 0 auto; }
.back-btn {
  border: none; background: none; cursor: pointer; padding: 0; margin-bottom: 8px;
  font-size: 12.5px; font-weight: 600; color: var(--color-primary); font-family: inherit;
}
.back-btn:hover { text-decoration: underline; }
.header-eyebrow {
  font-size: 11px; letter-spacing: 2.5px; text-transform: uppercase;
  color: var(--color-primary); font-weight: 700;
}
.header-title h1 { font-size: 22px; font-weight: 700; margin: 2px 0 4px; letter-spacing: -0.3px; }
.header-sub { font-size: 12.5px; color: var(--color-text-secondary); max-width: 760px; line-height: 1.5; }
.pse-wrap { flex: 1; padding: 20px 32px 40px; max-width: 1400px; width: 100%; margin: 0 auto; box-sizing: border-box; }

@media (max-width: 768px) {
  .eval-header { padding-top: 44px; }
  .header-inner { padding: 16px; }
  .pse-wrap { padding: 16px 16px 40px; }
}
</style>
