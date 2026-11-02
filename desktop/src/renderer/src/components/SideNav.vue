<script setup lang="ts">
import { ref, watch } from 'vue'

const props = defineProps<{ active: string }>()

const emit = defineEmits<{
  (e: 'update:active', value: string): void
}>()

// 按日常操作顺序分组，降低选品、素材和发布入口之间的切换成本。
const NAV_GROUPS = [
  { title: '工作台', items: [{ label: '投放看板', icon: '▦' }] },
  { title: '选品与素材', items: [{ label: '选品设置', icon: '◇' }, { label: '选品日志', icon: '≡' }, { label: '素材与工具', icon: '▧' }] },
  { title: '发布管理', items: [{ label: '发布队列', icon: '▷' }, { label: '千川管理', icon: '▤' }] },
  { title: '账户与系统', items: [{ label: '连接与工具', icon: '⌘' }, { label: '账号管理', icon: '♙' }] }
]

const expanded = ref<Record<string, boolean>>({
  工作台: true,
  选品与素材: true,
  发布管理: true,
  账户与系统: true
})

function toggleGroup(title: string): void {
  expanded.value[title] = !expanded.value[title]
}

watch(() => props.active, (active) => {
  const group = NAV_GROUPS.find((item) => item.items.some((navItem) => navItem.label === active))
  if (group) expanded.value[group.title] = true
})
</script>

<template>
  <aside class="nav">
    <div v-for="group in NAV_GROUPS" :key="group.title" class="nav-group">
      <button type="button" class="group-title" :aria-expanded="expanded[group.title]" @click="toggleGroup(group.title)">
        <span>{{ group.title }}</span>
        <span class="group-chevron" :class="{ collapsed: !expanded[group.title] }">⌄</span>
      </button>
      <div v-if="expanded[group.title]" class="group-items">
        <button
          v-for="item in group.items"
          :key="item.label"
          type="button"
          class="nav-item"
          :class="{ active: item.label === props.active }"
          @click="emit('update:active', item.label)"
        >
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
  padding: 10px 0 14px;
  overflow-y: auto;
  /* 极窄的滚动条，避免深色背景下出现一条突兀的白条 */
  scrollbar-width: thin;
}

.nav-group + .nav-group { margin-top: 8px; }

.group-title {
  width: 100%;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 14px 6px 17px;
  color: #8790a1;
  text-align: left;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .05em;
}

.group-title:hover { color: var(--text-on-dark); }
.group-chevron { color: #7c8799; font-size: 14px; transition: transform .15s; }
.group-chevron.collapsed { transform: rotate(-90deg); }
.group-items { display: flex; flex-direction: column; gap: 2px; }

.nav-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 36px;
  padding: 8px 13px;
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

.nav-icon {
  width: 17px;
  display: inline-flex;
  justify-content: center;
  align-items: center;
  color: #a7b0c0;
  font-size: 14px;
  line-height: 1;
  flex-shrink: 0;
}

.nav-item.active .nav-icon {
  color: var(--brand);
}

.label {
  white-space: nowrap;
}
</style>
