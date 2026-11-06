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
  { title: '发布与计划', items: [{ label: '商品列表', icon: '▤' }, { label: '发布队列', icon: '▷' }, { label: '千川管理', icon: '▤' }] },
  { title: '账户与系统', items: [{ label: '连接与工具', icon: '⌘' }, { label: '账号管理', icon: '♙' }] }
]

const expanded = ref<Record<string, boolean>>({
  工作台: true,
  选品与素材: true,
  发布与计划: true,
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
          :aria-current="item.label === props.active ? 'page' : undefined"
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
  background: linear-gradient(180deg, #16243e 0%, #121c32 100%);
  padding: 15px 0 18px;
  overflow-y: auto;
  /* 极窄的滚动条，避免深色背景下出现一条突兀的白条 */
  scrollbar-width: thin;
}

.nav-group + .nav-group { margin-top: 11px; }

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
