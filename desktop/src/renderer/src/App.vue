<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import TopMenu from './components/TopMenu.vue'
import SideNav from './components/SideNav.vue'
import WorkArea from './components/WorkArea.vue'
import StatusBar from './components/StatusBar.vue'

// 当前选中的顶部菜单与左侧导航。
const activeTop = ref('百应工作台')
const activeNav = ref('投放看板')

// 状态栏数据。素材库记录数从 SQLite 实时查询；扫描/删除后由素材库界面广播刷新。
const assetCount = ref(0)
const recordCount = ref(0)
const downloadDir = ref('D:\\software\\投广客户端\\downloads')

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
          'D:\\software\\投广客户端\\data\\ad_client.db',
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

onMounted(() => {
  void refreshAssetCount()
  // 素材库扫描/删除完成后会广播该事件，状态栏随之刷新
  window.addEventListener('asset-library-changed', refreshAssetCount)
})

onBeforeUnmount(() => {
  window.removeEventListener('asset-library-changed', refreshAssetCount)
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
      <SideNav v-model:active="activeNav" />
      <WorkArea :top="activeTop" :nav="activeNav" />
    </div>

    <StatusBar
      :asset-count="assetCount"
      :record-count="recordCount"
      :download-dir="downloadDir"
    />
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
</style>