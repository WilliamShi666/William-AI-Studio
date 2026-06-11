import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  base: '/tutor/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './'),
    },
  },
  server: {
    port: 5173,
    host: true,
    open: true,
    proxy: {
      // 将前端的 /mr-api/* 请求转发到对应的后端服务
      '/mr-api/milvus': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/mr-api\/milvus/, ''),
      },
      '/mr-api/chat': {
        target: 'http://127.0.0.1:8501',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/mr-api\/chat/, ''),
      },
      '/mr-api/extraction': {
        target: 'http://127.0.0.1:8006',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/mr-api\/extraction/, ''),
      },
      '/mr-api/chunk': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/mr-api\/chunk/, ''),
      },
      '/mr-api/debate': {
        target: 'http://127.0.0.1:8602',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/mr-api\/debate/, ''),
      },
      // 将前端的 /debate-api 请求转发到本机的多智能体辩论服务
      '/debate-api': {
        target: 'http://127.0.0.1:8602',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/debate-api/, ''),
      },
    },
  },
  preview: {
    port: 5173,
    host: true,
    allowedHosts: ['www.royswailab.online'],
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom'],
          'motion-vendor': ['motion'],
          'ui-vendor': ['lucide-react', 'sonner'],
        },
      },
    },
  },
})
