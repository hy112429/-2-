<script setup lang="ts">
const props = defineProps<{ active: string }>()

const emit = defineEmits<{
  (e: 'update:active', value: string): void
}>()

// 左侧导航（需求 4）。
// 用一个 CSS 画的小方块当图标，避免为了几个图标引入一整套图标库。
const NAV_ITEMS = ['投放看板', '素材与工具', '千川管理', '账号管理']
</script>

<template>
  <aside class="nav">
    <button
      v-for="item in NAV_ITEMS"
      :key="item"
      type="button"
      class="nav-item"
      :class="{ active: item === props.active }"
      @click="emit('update:active', item)"
    >
      <span class="dot" />
      <span class="label">{{ item }}</span>
    </button>
  </aside>
</template>

<style scoped>
.nav {
  background: var(--bg-nav);
  padding: 12px 0;
  overflow-y: auto;
  /* 极窄的滚动条，避免深色背景下出现一条突兀的白条 */
  scrollbar-width: thin;
}

.nav-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  color: var(--text-on-dark);
  text-align: left;
  border-left: 3px solid transparent;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}

.nav-item:hover {
  background: var(--bg-nav-hover);
  color: var(--text-on-dark-strong);
}

.nav-item.active {
  background: var(--bg-nav-hover);
  color: var(--text-on-dark-strong);
  border-left-color: var(--brand);
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 2px;
  background: currentColor;
  opacity: 0.55;
  flex-shrink: 0;
}

.nav-item.active .dot {
  opacity: 1;
  background: var(--brand);
}

.label {
  white-space: nowrap;
}
</style>