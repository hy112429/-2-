<script setup lang="ts">
defineProps<{
  assetCount: number
  recordCount: number
  downloadDir: string
}>()
</script>

<template>
  <footer class="status">
    <span class="item">本地素材 <b>{{ assetCount }}</b> 条</span>
    <details class="status-details">
      <summary>本机信息</summary>
      <div class="status-popover">
        <span>记录数：{{ recordCount }}</span>
        <span>下载目录：{{ downloadDir || '尚未设置' }}</span>
      </div>
    </details>
    <span class="state">本机就绪</span>
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
  box-shadow: 0 -2px 10px rgba(28, 50, 89, .035);
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

.status-details { position: relative; }
.status-details summary { cursor: pointer; color: #687894; }
.status-popover { position: absolute; z-index: 5; bottom: 25px; left: 0; display: flex; flex-direction: column; gap: 6px; width: min(440px, 75vw); padding: 12px; border: 1px solid var(--border); border-radius: 7px; background: #fff; box-shadow: var(--shadow-card); color: var(--text); overflow-wrap: anywhere; }

.state {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #24825e;
  white-space: nowrap;
}
.state::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #3aaf7b; }
</style>
