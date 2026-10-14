<script setup lang="ts">
// 状态栏三项数据（需求 4）。
// 现在由 App.vue 传占位值进来，Step 5 接上 SQLite 后换成真实查询结果，
// 这个组件本身不用改。
defineProps<{
  assetCount: number
  recordCount: number
  downloadDir: string
}>()
</script>

<template>
  <footer class="status">
    <span class="item">
      素材库读取数：<b>{{ assetCount }}</b>
    </span>
    <span class="sep">|</span>
    <span class="item">
      记录数：<b>{{ recordCount }}</b>
    </span>
    <span class="sep">|</span>
    <span class="item path-item" :title="downloadDir">
      当前下载目录：<span class="path">{{ downloadDir }}</span>
    </span>
    <span class="state">就绪</span>
  </footer>
</template>

<style scoped>
.status {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  background: var(--bg-status);
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 12px;
  /* 路径可能要选中复制，这里放开文本选择 */
  user-select: text;
}

.item {
  white-space: nowrap;
}

.item b {
  color: var(--text);
  font-weight: 600;
}

.sep {
  color: var(--border);
}

.path-item {
  /* 路径可能很长，让它可伸缩并省略号截断，避免把状态栏挤爆 */
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.path {
  font-family: Consolas, 'Courier New', monospace;
  color: var(--text);
}

.state {
  margin-left: auto;
  color: var(--brand);
  white-space: nowrap;
}
</style>