import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [
    vue({
      template: {
        compilerOptions: {
          isCustomElement: (tag) => tag.startsWith('cropper-'),
        },
      },
    }),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      // 用 127.0.0.1 而非 localhost：Node 18+ 会优先尝试 IPv6 (::1)，
      // 后端仅监听 IPv4 时会导致代理 ETIMEDOUT/ECONNREFUSED
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/uploads': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 不再配置 manualChunks：Vite 8 (rolldown) 下强制把 axios 等拆进独立 vendor chunk
    // 会产生跨 chunk 循环初始化（misc-vendor 顶层调用 api chunk 尚未初始化的导出，
    // 生产包抛 "e is not a function"，dev 不打包故本地正常）。
    // 路由级动态 import 仍会自然分块，缓存与并行下载不受明显影响。
  },
})
