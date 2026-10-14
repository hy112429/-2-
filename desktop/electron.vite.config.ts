import { resolve } from 'path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import vue from '@vitejs/plugin-vue'

/**
 * electron-vite 配置：一份配置同时管三个进程
 *   main     主进程（Node 环境，操作窗口/系统）
 *   preload  预加载脚本（桥梁，能同时碰 Node 和 浏览器 API）
 *   renderer 渲染进程（就是那个网页，跑 Vue）
 */
export default defineConfig({
  main: {
    // externalizeDepsPlugin：把 package.json 里的依赖标记为"外部依赖"，
    // 不打进 bundle。主进程运行在 Node 里，依赖直接从 node_modules 加载即可。
    plugins: [externalizeDepsPlugin()]
  },
  preload: {
    plugins: [externalizeDepsPlugin()]
  },
  renderer: {
    resolve: {
      alias: {
        // 渲染进程里用 @renderer/xxx 指代 src/renderer/src/xxx，避免一堆 ../../../
        '@renderer': resolve('src/renderer/src')
      }
    },
    plugins: [vue()]
  }
})