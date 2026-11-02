<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import TopMenu from './components/TopMenu.vue'
import SideNav from './components/SideNav.vue'
import WorkArea from './components/WorkArea.vue'
import StatusBar from './components/StatusBar.vue'
import ToastHost from './components/ToastHost.vue'
import { notifyGlobal } from './utils/notifications'

// 当前选中的顶部菜单与左侧导航。
const activeTop = ref('百应工作台')
const activeNav = ref('投放看板')

function activateNav(value: string): void {
  activeNav.value = value
  activeTop.value = '百应工作台'
}

// 状态栏数据。素材库记录数从 SQLite 实时查询；扫描/删除后由素材库界面广播刷新。
const assetCount = ref(0)
const recordCount = ref(0)
const downloadDir = ref('')
const startupReady = ref(false)
const startupError = ref('')
let databasePath = ''
let stopDownloadDirChanged: (() => void) | undefined
let stopAutoPublishNotification: (() => void) | undefined

function runEngineOnce(script: string, args: string[]): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let taskId: string | undefined
    let settled = false
    const earlyEvents: EngineEventPayload[] = []
    let off = (): void => undefined
    let timer = 0
    const finish = (error?: Error, data?: Record<string, unknown>): void => {
      if (settled) return
      settled = true
      window.clearTimeout(timer)
      off()
      if (error) reject(error)
      else resolve(data ?? {})
    }
    const accept = (event: EngineEventPayload): void => {
      if (event.type === 'error') {
        finish(new Error(event.message ?? '初始化本地数据库失败'))
      } else if (event.type === 'result') {
        const data = (event.data ?? {}) as Record<string, unknown>
        if (data.ok === false) finish(new Error(String(data.error ?? '初始化本地数据库失败')))
        else finish(undefined, data)
      }
    }
    timer = window.setTimeout(() => finish(new Error('初始化本地数据库超时')), 30_000)

    off = window.api.engine.onEvent((event) => {
      if (!taskId) {
        earlyEvents.push(event)
        return
      }
      if (event.taskId === taskId) accept(event)
    })

    void window.api.engine.run({ script, args }).then((reply) => {
      if (!reply.taskId) {
        finish(new Error(reply.error ?? '无法启动数据库初始化'))
        return
      }
      taskId = reply.taskId
      for (const event of earlyEvents) {
        if (event.taskId === taskId) accept(event)
      }
    }).catch((error: unknown) => {
      finish(error instanceof Error ? error : new Error(String(error)))
    })
  })
}

/** 查询素材库记录数（assets 表行数） */
function queryAssetCount(): Promise<number> {
  return new Promise((resolve) => {
    let settled = false
    let taskId: string | null = null
    let off: () => void = () => undefined
    const settle = (fn: () => void): void => {
      if (settled) return
      settled = true
      off()
      fn()
    }
    off = window.api.engine.onEvent((e) => {
      if (taskId && e.taskId !== taskId) return
      if (e.type === 'result') {
        const d = e.data as { rows?: Array<{ n?: number }> }
        settle(() => resolve(Number(d?.rows?.[0]?.n ?? 0)))
      } else if (e.type === 'error') {
        settle(() => resolve(0)) // 查询失败显示 0，不打断界面
      }
    })
    window.setTimeout(() => settle(() => resolve(0)), 10_000)
    void window.api.engine
      .run({
        script: 'db.py',
        args: [
          '--db',
          databasePath,
          'sql',
          '--query',
          'SELECT COUNT(*) AS n FROM assets'
        ]
      })
      .then((reply) => {
        if (!reply.taskId) settle(() => resolve(0))
        else taskId = reply.taskId
      })
  })
}

async function refreshAssetCount(): Promise<void> {
  assetCount.value = await queryAssetCount()
}

onMounted(async () => {
  window.addEventListener('asset-library-changed', refreshAssetCount)
  stopDownloadDirChanged = window.api.dir.onDownloadDirChanged((path) => { downloadDir.value = path })
  stopAutoPublishNotification = window.api.publish.onProgress((progress) => {
    if (progress.state !== 'succeeded' && progress.state !== 'failed') return
    const task = progress.queueId ? `任务 #${progress.queueId}` : '自动发布任务'
    const product = progress.productName ? `「${progress.productName}」` : ''
    const details = progress.error || progress.message || progress.stage
    notifyGlobal(`${task}${product}${progress.state === 'succeeded' ? '处理完成' : '失败'}：${details}`, progress.state === 'failed' ? 'error' : 'success')
  })
  try {
    const paths = await window.api.appPaths.get()
    databasePath = paths.databasePath
    downloadDir.value = await window.api.dir.getDownloadDir()
    // 仅新安装时创建空库；已存在的开发库不在启动时迁移或改写。
    if (!paths.databaseExists) {
      await runEngineOnce('db.py', ['--db', databasePath, 'init'])
    }
    startupReady.value = true
    void refreshAssetCount()
  } catch (error) {
    startupError.value = error instanceof Error ? error.message : String(error)
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('asset-library-changed', refreshAssetCount)
  stopDownloadDirChanged?.()
  stopAutoPublishNotification?.()
})
</script>

<template>
  <!--
    整体骨架：三行网格
      第 1 行 顶部菜单
      第 2 行 左侧导航 + 中部工作区（两列网格）
      第 3 行 底部状态栏（横跨整宽）
  -->
  <div class="shell">
    <TopMenu v-model:active="activeTop" />

    <div class="body">
      <SideNav :active="activeNav" @update:active="activateNav" />
      <div v-if="startupError" class="startup-state error">本地数据初始化失败：{{ startupError }}</div>
      <div v-else-if="!startupReady" class="startup-state">正在准备本机数据目录…</div>
      <WorkArea v-else :top="activeTop" :nav="activeNav" @navigate="activateNav" />
    </div>

    <StatusBar
      :asset-count="assetCount"
      :record-count="recordCount"
      :download-dir="downloadDir"
    />

    <ToastHost />
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  grid-template-rows: var(--h-top) 1fr var(--h-status);
  height: 100%;
  overflow: hidden;
}

.body {
  display: grid;
  grid-template-columns: var(--w-nav) 1fr;
  /* min-height: 0 是网格布局的关键：不加它，子元素的高度会被内容撑开，
     内部的滚动区域就不会滚动，而是把整个窗口撑出滚动条。 */
  min-height: 0;
  overflow: hidden;
}

.startup-state {
  display: grid;
  place-items: center;
  min-width: 0;
  color: #667085;
  background: #f4f6fa;
  padding: 24px;
}

.startup-state.error {
  color: #c0392b;
}
</style>
