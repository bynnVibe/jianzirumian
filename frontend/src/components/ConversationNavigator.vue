<template>
  <nav v-if="entries.length" ref="root" class="conversation-nav" aria-label="当前对话目录"
    @mouseenter="open = true" @mouseleave="closeUnlessFocused" @focusin="open = true"
    @focusout="handleFocusOut" @keydown.esc.stop="open = false">
    <button class="nav-trigger" :aria-expanded="open" aria-controls="conversation-index"
      aria-label="展开当前对话目录" @click="open = !open">
      <span class="mobile-label">目录</span>
      <span class="nav-ticks" aria-hidden="true">
        <i v-for="entry in ticks" :key="entry.id" :class="{ current: entry.id === sampledActive }"></i>
      </span>
    </button>
    <div v-show="open" id="conversation-index" class="nav-popover">
      <div class="nav-heading">当前对话 <span>{{ entries.length }} 个提问</span></div>
      <ol>
        <li v-for="(entry, index) in entries" :key="entry.id">
          <button :aria-current="active === entry.id ? 'location' : undefined"
            :class="{ selected: active === entry.id }" :title="entry.title" @click="select(entry.id)">
            <span class="nav-number">{{ index + 1 }}</span><span>{{ entry.title }}</span>
          </button>
        </li>
      </ol>
    </div>
  </nav>
</template>

<script setup>
import { computed, ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { sampledEntries } from '@/utils/conversationNavigation'
const props = defineProps({ entries: { type: Array, default: () => [] }, active: String, sessionId: String })
const emit = defineEmits(['navigate'])
const root = ref(null)
const open = ref(false)
const ticks = computed(() => sampledEntries(props.entries))
const sampledActive = computed(() => {
  const index = props.entries.findIndex((entry) => entry.id === props.active)
  return ticks.value.reduce((best, entry) => {
    const distance = Math.abs(props.entries.indexOf(entry) - index)
    return !best || distance < best.distance ? { id: entry.id, distance } : best
  }, null)?.id
})
watch(() => props.sessionId, () => { open.value = false })
function select(id) { emit('navigate', id); open.value = false }
function closeUnlessFocused() { if (!root.value?.contains(document.activeElement)) open.value = false }
function handleFocusOut(event) { if (!root.value?.contains(event.relatedTarget)) open.value = false }
function outside(event) { if (!root.value?.contains(event.target)) open.value = false }
onMounted(() => document.addEventListener('pointerdown', outside))
onBeforeUnmount(() => document.removeEventListener('pointerdown', outside))
</script>

<style scoped>
.conversation-nav { position: absolute; right: 8px; top: 24%; z-index: 15; display: flex; align-items: flex-start; }
.nav-trigger { width: 32px; border: 0; padding: 12px 8px; background: transparent; cursor: pointer; border-radius: 8px; color: var(--color-text-secondary); }
.nav-trigger:hover { background: #f0ebe4; }
.nav-ticks { display: flex; flex-direction: column; gap: 5px; max-height: 38vh; }
.nav-ticks i { display: block; height: 2px; min-height: 1px; width: 12px; background: #d0c6b8; border-radius: 3px; }
.nav-ticks i.current { width: 19px; background: var(--color-primary); }
.nav-popover { position: absolute; right: 30px; top: 0; width: min(310px, calc(100vw - 68px)); background: #fffdf9; border: 1px solid var(--color-border); border-radius: 12px; box-shadow: 0 8px 30px #372b1720; padding: 12px; }
.nav-heading { font-size: 13px; font-weight: 600; padding: 2px 6px 10px; }
.nav-heading span { float: right; color: var(--color-text-secondary); font-size: 11px; font-weight: 400; }
ol { list-style: none; padding: 0; margin: 0; max-height: 44vh; overflow-y: auto; overscroll-behavior: contain; }
li button { border: 0; background: transparent; display: flex; gap: 8px; width: 100%; padding: 10px 8px; text-align: left; border-radius: 6px; cursor: pointer; font-size: 13px; color: var(--color-text); }
li button > span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
li button:hover, li button.selected { background: #f2ece2; color: var(--color-primary); }
.nav-number { color: #aa9d8d; flex-shrink: 0; font-size: 11px; min-width: 18px; padding-top: 2px; }
button:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
.mobile-label { display: none; }
@media (max-width: 768px) {
  .conversation-nav { top: 10px; right: 10px; }
  .nav-trigger { width: auto; padding: 7px 10px; background: #fffdf9; border: 1px solid var(--color-border); font-size: 12px; }
  .mobile-label { display: inline; }
  .nav-ticks { display: none; }
  .nav-popover { top: 36px; right: 0; }
}
</style>
