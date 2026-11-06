<script setup lang="ts">
import { onMounted, ref } from 'vue'

const props = defineProps<{ active: string }>()

const emit = defineEmits<{
  (e: 'update:active', value: string): void
}>()

// 顶部菜单项（需求 4）
const MENU_ITEMS = ['百应工作台', '商品查询']
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
        :key="item"
        type="button"
        class="menu-item"
        :class="{ active: item === props.active }"
        :aria-current="item === props.active ? 'page' : undefined"
        @click="emit('update:active', item)"
      >
        {{ item }}
      </button>
    </nav>

    <div class="version">v{{ appVersion }}</div>
  </header>
</template>

<style scoped>
.top {
  display: flex;
  align-items: center;
  gap: 28px;
  padding: 0 18px;
  background: linear-gradient(105deg, #18243c 0%, #213659 100%);
  border-bottom: 1px solid var(--border-dark);
  box-shadow: 0 2px 12px rgba(16, 30, 55, .16);
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
  background: linear-gradient(135deg, #6297ff, #2d68e4);
  color: #ffffff;
  border-radius: 9px;
  box-shadow: 0 3px 10px rgba(45, 104, 228, .28);
  font-size: 14px;
  font-weight: 700;
}

.brand-name {
  color: var(--text-on-dark-strong);
  font-weight: 600;
  letter-spacing: 0.3px;
  white-space: nowrap;
}

.menus {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px;
  border: 1px solid rgba(255, 255, 255, .08);
  border-radius: 10px;
  background: rgba(8, 20, 39, .22);
}

.menu-item {
  min-height: 32px;
  padding: 0 15px;
  color: var(--text-on-dark);
  border-radius: 7px;
  transition: color 0.15s, background 0.15s;
  white-space: nowrap;
}

.menu-item:hover {
  color: var(--text-on-dark-strong);
  background: rgba(255, 255, 255, 0.09);
}

.menu-item.active {
  color: var(--text-on-dark-strong);
  background: rgba(104, 154, 255, .27);
  box-shadow: inset 0 0 0 1px rgba(169, 199, 255, .16);
}

.version {
  margin-left: auto;
  padding: 3px 8px;
  border: 1px solid rgba(182, 203, 238, .18);
  border-radius: 999px;
  color: #b8c7df;
  font-size: 11px;
}
</style>
