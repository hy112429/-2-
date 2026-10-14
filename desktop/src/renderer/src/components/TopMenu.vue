<script setup lang="ts">
const props = defineProps<{ active: string }>()

const emit = defineEmits<{
  (e: 'update:active', value: string): void
}>()

// 顶部菜单项（需求 4）
const MENU_ITEMS = ['百应工作台', '商品查询']
</script>

<template>
  <header class="top">
    <div class="brand">
      <span class="brand-mark">投</span>
      <span class="brand-name">投广管理客户端</span>
    </div>

    <nav class="menus">
      <button
        v-for="item in MENU_ITEMS"
        :key="item"
        type="button"
        class="menu-item"
        :class="{ active: item === props.active }"
        @click="emit('update:active', item)"
      >
        {{ item }}
      </button>
    </nav>

    <div class="version">v0.0.1</div>
  </header>
</template>

<style scoped>
.top {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 0 16px;
  background: var(--bg-top);
  border-bottom: 1px solid var(--border-dark);
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.brand-mark {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  background: var(--brand);
  color: #ffffff;
  border-radius: var(--radius);
  font-size: 13px;
  font-weight: 600;
}

.brand-name {
  color: var(--text-on-dark-strong);
  font-weight: 600;
  letter-spacing: 0.5px;
  white-space: nowrap;
}

.menus {
  display: flex;
  align-items: stretch;
  gap: 4px;
  height: 100%;
}

.menu-item {
  padding: 0 16px;
  color: var(--text-on-dark);
  border-bottom: 2px solid transparent;
  transition: color 0.15s, background 0.15s, border-color 0.15s;
  white-space: nowrap;
}

.menu-item:hover {
  color: var(--text-on-dark-strong);
  background: rgba(255, 255, 255, 0.06);
}

.menu-item.active {
  color: var(--text-on-dark-strong);
  border-bottom-color: var(--brand);
}

.version {
  margin-left: auto;
  color: #7b8496;
  font-size: 12px;
}
</style>