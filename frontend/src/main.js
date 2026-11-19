import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { useAuthStore } from './stores/auth'
import './style.css'
import './mobile.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)

// 挂载前同步恢复登录态（localStorage），保证子组件 onMounted 时 isAuthenticated 已就绪
// （否则 AppSidebar 首挂时跳过会话列表加载，刷新后侧栏为空）；init 幂等，内部异步验证不阻塞
useAuthStore().init()

// Vue 挂载后立即收起启动屏，不等 auth.init 完成
app.mount('#app')
window.__removeSplash()
