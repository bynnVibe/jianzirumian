import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { activeEntry, conversationEntries, nearBottom } from './conversationNavigation'

export function useConversationNavigation(container, messages, sessionId, response) {
  const entries = computed(() => conversationEntries(messages.value, sessionId.value))
  const active = ref('')
  const following = ref(true)
  let observer, frame = 0, jumpFrame = 0, jumping = false, lastTop = 0
  function updateActive() {
    frame = 0
    const element = container.value
    if (!element) return
    const positions = entries.value.map((entry) => document.getElementById(entry.id)?.getBoundingClientRect().top ?? Infinity)
    active.value = activeEntry(entries.value, positions, element.getBoundingClientRect().top)
  }
  function scheduleActive() {
    if (!frame) frame = requestAnimationFrame(updateActive)
  }
  function scrollToBottom() {
    following.value = true
    const element = container.value
    if (element) { element.scrollTop = element.scrollHeight; lastTop = element.scrollTop }
    scheduleActive()
  }
  function onScroll() {
    const element = container.value
    if (!element) return
    if (!jumping) {
      if (!nearBottom(element)) following.value = false
      else if (element.scrollTop > lastTop) following.value = true
    }
    lastTop = element.scrollTop
    scheduleActive()
  }
  function navigate(id) {
    const element = container.value
    const target = document.getElementById(id)
    if (!element || !target || !element.contains(target)) return
    following.value = false
    jumping = true
    cancelAnimationFrame(jumpFrame)
    element.scrollTop += target.getBoundingClientRect().top - element.getBoundingClientRect().top - 16
    lastTop = element.scrollTop
    active.value = id
    jumpFrame = requestAnimationFrame(() => { jumping = false })
  }
  async function contentChanged() {
    await nextTick()
    if (following.value) scrollToBottom()
    else scheduleActive()
  }
  watch([messages, () => messages.value.length, response], contentChanged, { flush: 'post' })
  watch(sessionId, async () => {
    following.value = true
    active.value = ''
    jumping = false
    cancelAnimationFrame(jumpFrame)
    await contentChanged()
  })
  watch(container, (element, previous) => {
    previous?.removeEventListener('scroll', onScroll)
    observer?.disconnect()
    element?.addEventListener('scroll', onScroll, { passive: true })
    if (element && typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(contentChanged)
      observer.observe(element)
      if (element.firstElementChild) observer.observe(element.firstElementChild)
    }
    if (element) contentChanged()
  }, { flush: 'post' })
  onBeforeUnmount(() => {
    observer?.disconnect()
    container.value?.removeEventListener('scroll', onScroll)
    cancelAnimationFrame(frame)
    cancelAnimationFrame(jumpFrame)
  })
  return { entries, active, following, navigate, scrollToBottom }
}
