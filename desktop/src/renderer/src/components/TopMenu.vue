<script setup lang="ts">
import { onMounted, ref } from 'vue'

const props = defineProps<{ active: string }>()

const emit = defineEmits<{
  (e: 'update:active', value: string): void
}>()

const MENU_ITEMS = [
  { value: '百应工作台', label: '工作台' },
  { value: '商品查询', label: '按商品 ID 下载' }
]
const appVersion = ref('')

onMounted(() => {
  void window.api.appInfo.getVersion()
    .then((version) => { appVersion.value = version })
    .catch(() => { appVersion.value = '未知' })
})
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
        :key="item.value"
        type="button"
        class="menu-item"
        :class="{ active: item.value === props.active }"
        :aria-current="item.value === props.active ? 'page' : undefined"
        @click="emit('update:active', item.value)"
      >
        {{ item.label }}
      </button>
    </nav>

    <div class="version">v{{ appVersion }}</div>
  </header>
</template>

<style scoped>
.top {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 0 16px;
  background: rgba(255, 255, 255, .96);
  border-bottom: 1px solid var(--border);
  box-shadow: 0 1px 0 rgba(0, 0, 0, .025);
  z-index: 2;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.brand-mark {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  background: var(--text);
  color: #ffffff;
  border-radius: 9px;
  font-size: 14px;
  font-weight: 700;
}

.brand-name {
  color: var(--text);
  font-weight: 650;
  letter-spacing: -0.02em;
  white-space: nowrap;
}

.menus {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-app);
}

.menu-item {
  min-height: 32px;
  padding: 0 13px;
  color: var(--text-muted);
  border-radius: 999px;
  transition: color 0.15s, background 0.15s;
  white-space: nowrap;
}

.menu-item:hover {
  color: var(--text);
  background: #eaeaec;
}

.menu-item.active {
  color: var(--text);
  background: #ffffff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .09);
  font-weight: 600;
}

.version {
  margin-left: auto;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-muted);
  font-size: 11px;
}
</style>
