import { resolve } from 'path'
import { defineConfig } from 'electron-vite'
import vue from '@vitejs/plugin-vue'
import { copyFileSync, existsSync } from 'fs'

export default defineConfig({
  main: {
    build: {
      // 将 yahoo-finance2 从外部依赖中排除，让它被打包进主进程代码
      // 这样可以确保 ES 模块的默认导出被 esbuild 正确处理
      externalizeDeps: {
        exclude: ['yahoo-finance2']
      },
      rollupOptions: {
        // 复制 sql.js wasm 文件到输出目录
        plugins: [
          {
            name: 'copy-sql-wasm',
            writeBundle() {
              const wasmSrc = resolve(__dirname, 'node_modules/sql.js/dist/sql-wasm.wasm')
              const wasmDest = resolve(__dirname, 'out/main/sql-wasm.wasm')
              
              if (existsSync(wasmSrc)) {
                try {
                  copyFileSync(wasmSrc, wasmDest)
                  console.log('[Build] Copied sql-wasm.wasm to output directory')
                } catch (error) {
                  console.error('[Build] Failed to copy sql-wasm.wasm:', error)
                }
              }
            }
          }
        ]
      }
    }
  },
  preload: {},
  renderer: {
    resolve: {
      alias: {
        '@renderer': resolve('src/renderer/src')
      }
    },
    plugins: [vue()],
    define: {
      global: 'globalThis'
    }
  }
})
