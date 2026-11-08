<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ active: string }>()

const emit = defineEmits<{
  (e: 'update:active', value: string): void
}>()

// 甲方日常只需要四个入口；低频页面仍可在“更多功能”中找到。
const MAIN_ITEMS = [
  { value: '投放看板', label: '工作概览', icon: '▦' },
  { value: '选品设置', label: '开始选品', icon: '◇' },
  { value: '发布队列', label: '发布进度', icon: '▷' },
  { value: '千川管理', label: '千川素材', icon: '▤' }
]
const MORE_ITEMS = [
  { value: '商品列表', label: '商品列表', icon: '▤' },
  { value: '选品日志', label: '选品记录', icon: '≡' },
  { value: '素材与工具', label: '素材库', icon: '▧' },
  { value: '账号管理', label: '账号管理', icon: '♙' },
  { value: '连接与工具', label: '连接与工具', icon: '⌘' }
]
const moreExpanded = ref(false)

watch(() => props.active, (active) => {
  if (MORE_ITEMS.some((item) => item.value === active)) moreExpanded.value = true
}, { immediate: true })
</script>

<template>
  <aside class="nav">
    <div class="nav-group">
      <div class="group-title">常用功能</div>
      <div class="group-items">
        <button v-for="item in MAIN_ITEMS" :key="item.value" type="button" class="nav-item"
          :class="{ active: item.value === props.active }"
          :aria-current="item.value === props.active ? 'page' : undefined"
          @click="emit('update:active', item.value)">
          <span class="nav-icon" aria-hidden="true">{{ item.icon }}</span>
          <span class="label">{{ item.label }}</span>
        </button>
      </div>
    </div>
    <div class="nav-group more-group">
      <button type="button" class="group-title" :aria-expanded="moreExpanded" @click="moreExpanded = !moreExpanded">
        <span>更多功能</span><span class="group-chevron" :class="{ collapsed: !moreExpanded }">⌄</span>
      </button>
      <div v-if="moreExpanded" class="group-items">
        <button v-for="item in MORE_ITEMS" :key="item.value" type="button" class="nav-item"
          :class="{ active: item.value === props.active }"
          :aria-current="item.value === props.active ? 'page' : undefined"
          @click="emit('update:active', item.value)">
          <span class="nav-icon" aria-hidden="true">{{ item.icon }}</span>
          <span class="label">{{ item.label }}</span>
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.nav {
  background: linear-gradient(180deg, #16243e 0%, #121c32 100%);
  padding: 15px 0 18px;
  overflow-y: auto;
  /* 极窄的滚动条，避免深色背景下出现一条突兀的白条 */
  scrollbar-width: thin;
}

.nav-group + .nav-group { margin-top: 22px; }

.group-title {
  width: 100%;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 15px 6px 17px;
  color: #899bb7;
  text-align: left;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .05em;
}

.group-title:hover { color: #e6eeff; }
.more-group > .group-title { border: 0; background: none; cursor: pointer; font-family: inherit; }
.group-chevron { color: #7c8799; font-size: 14px; transition: transform .15s; }
.group-chevron.collapsed { transform: rotate(-90deg); }
.group-items { display: flex; flex-direction: column; gap: 3px; }

.nav-item {
  width: calc(100% - 16px);
  margin: 0 8px;
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 37px;
  padding: 7px 10px;
  border-radius: 8px;
  color: var(--text-on-dark);
  text-align: left;
  transition: background 0.15s, color 0.15s;
}

.nav-item:hover {
  background: rgba(137, 171, 233, .12);
  color: var(--text-on-dark-strong);
}

.nav-item.active {
  background: linear-gradient(90deg, rgba(78, 130, 245, .32), rgba(78, 130, 245, .13));
  color: var(--text-on-dark-strong);
  box-shadow: inset 3px 0 #6fa1ff;
  font-weight: 600;
}

.nav-icon {
  width: 22px;
  height: 22px;
  display: inline-flex;
  justify-content: center;
  align-items: center;
  border-radius: 6px;
  background: rgba(255, 255, 255, .055);
  color: #9fb2d0;
  font-size: 13px;
  line-height: 1;
  flex-shrink: 0;
}

.nav-item.active .nav-icon {
  background: rgba(126, 171, 255, .18);
  color: #b9d2ff;
}

.label {
  white-space: nowrap;
}
</style>
