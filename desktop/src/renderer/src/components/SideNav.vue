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
  { value: '千川计划', label: '千川投放', icon: '▣' }
]
const MORE_ITEMS = [
  { value: '商品列表', label: '商品列表', icon: '▤' },
  { value: '选品日志', label: '选品记录', icon: '≡' },
  { value: '千川日志', label: '千川发布日志', icon: '◷' },
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
  background: var(--bg-nav);
  border-right: 1px solid var(--border);
  padding: 16px 0 18px;
  overflow-y: auto;
  /* 侧栏滚动条保持轻量。 */
  scrollbar-width: thin;
}

.nav-group + .nav-group { margin-top: 22px; }

.group-title {
  width: 100%;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 15px 6px 17px;
  color: var(--text-muted);
  text-align: left;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .02em;
}

.group-title:hover { color: var(--text); }
.more-group > .group-title { border: 0; background: none; cursor: pointer; font-family: inherit; }
.group-chevron { color: var(--text-muted); font-size: 14px; transition: transform .15s; }
.group-chevron.collapsed { transform: rotate(-90deg); }
.group-items { display: flex; flex-direction: column; gap: 3px; }

.nav-item {
  width: calc(100% - 16px);
  margin: 0 8px;
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 36px;
  padding: 7px 10px;
  border-radius: var(--radius-control);
  color: #424245;
  text-align: left;
  transition: background 0.15s, color 0.15s;
}

.nav-item:hover {
  background: var(--bg-nav-hover);
  color: var(--text);
}

.nav-item.active {
  background: #e7f2ff;
  color: #005bbd;
  font-weight: 600;
}

.nav-icon {
  width: 22px;
  height: 22px;
  display: inline-flex;
  justify-content: center;
  align-items: center;
  border-radius: 6px;
  background: #ededf0;
  color: #6e6e73;
  font-size: 13px;
  line-height: 1;
  flex-shrink: 0;
}

.nav-item.active .nav-icon {
  background: #d4e8ff;
  color: #005bbd;
}

.label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 980px) {
  .nav { padding-top: 9px; }
  .nav-group + .nav-group { margin-top: 12px; }
  .group-title { padding-left: 12px; font-size: 10px; }
  .nav-item { gap: 7px; min-height: 34px; padding: 6px 8px; }
}
</style>
