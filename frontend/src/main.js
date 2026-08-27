import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)

// Vue 挂载后立即收起启动屏，不等 auth.init 完成
app.mount('#app')
window.__removeSplash()
