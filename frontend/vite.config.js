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
    rollupOptions: {
      output: {
        // 拆分 vendor：多个小 chunk 可并行下载、独立缓存，降低首屏阻塞
        // 注意：Vite 8 (rolldown) 仅支持函数形式
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (/[\\/]node_modules[\\/](vue|@vue|@vueuse|vue-router|pinia)[\\/]/.test(id)) {
            return 'vue-vendor'
          }
          if (/[\\/]node_modules[\\/](markdown-it|katex)[\\/]/.test(id)) {
            return 'markdown-vendor'
          }
          if (/[\\/]node_modules[\\/](axios|cropperjs)[\\/]/.test(id)) {
            return 'misc-vendor'
          }
          return undefined
        },
      },
    },
  },
})
