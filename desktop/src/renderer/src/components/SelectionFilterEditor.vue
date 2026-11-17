<script setup lang="ts">
import { computed } from 'vue'
import type { SelectionFilterSettings } from '../utils/selection_filters'

interface FilterField {
  label: string
  options: string[]
  multiple: boolean
}

const props = withDefaults(defineProps<{
  modelValue: SelectionFilterSettings
  filterGroups: Record<string, FilterField[]>
  featureOptions: string[]
  rankCategoryOptions?: Array<{ path: string[]; label: string }>
  filterCatalogUpdatedAt?: string
  title?: string
  disabled?: boolean
  showSync?: boolean
  syncing?: boolean
}>(), {
  filterCatalogUpdatedAt: '',
  title: '选品条件',
  disabled: false,
  showSync: false,
  syncing: false,
  rankCategoryOptions: () => []
})

const emit = defineEmits<{
  (event: 'update:modelValue', value: SelectionFilterSettings): void
  (event: 'sync'): void
}>()

const sections = computed(() => [
  { title: '带货情况', fields: (props.filterGroups['带货情况'] ?? []).filter((field) => field.label !== '佣金区间') },
  { title: '商品信息', fields: (props.filterGroups['商品信息'] ?? []).filter((field) => field.label !== '佣金区间') }
])

const commissionHint = computed(() => {
  const lower = props.modelValue.thresholds.commissionRate
  const upper = props.modelValue.thresholds.commissionRateMax
  if (lower === null && upper === null) return '留空时不限制佣金率。'
  return `目标佣金率：${lower ?? 0}%–${upper ?? 100}%。选品广场填写精确区间；榜单选择不遗漏目标商品的档位；热点按商品实际数据核验。`
})

function update(change: (value: SelectionFilterSettings) => void): void {
  const next = JSON.parse(JSON.stringify(props.modelValue)) as SelectionFilterSettings
  change(next)
  emit('update:modelValue', next)
}

function filterGroupKey(title: string): 'delivery' | 'product' | 'features' {
  if (title === '带货情况') return 'delivery'
  if (title === '商品信息') return 'product'
  return 'features'
}

function selectedFilterValues(group: string, label: string): string[] {
  const key = filterGroupKey(group)
  if (key === 'features') return props.modelValue.filters.features.filter((item) => item === label)
  const value = props.modelValue.filters[key][label]
  return Array.isArray(value) ? value.map(String) : value ? [String(value)] : []
}

function setFilterValue(group: string, label: string, value: string, checked: boolean, multiple: boolean): void {
  const key = filterGroupKey(group)
  update((settings) => {
    if (key === 'features') {
      const values = new Set(settings.filters.features)
      if (checked) values.add(label)
      else values.delete(label)
      settings.filters.features = [...values]
    } else if (multiple) {
      const values = new Set(selectedFilterValues(group, label))
      if (checked) values.add(value)
      else values.delete(value)
      settings.filters[key][label] = [...values]
    } else {
      settings.filters[key][label] = value
    }
  })
}

function setCandidateLimit(value: string): void {
  update((settings) => { settings.candidateLimit = value === '' ? 0 : Number(value) })
}

function setThreshold(key: keyof SelectionFilterSettings['thresholds'], value: string): void {
  update((settings) => { settings.thresholds[key] = value.trim() === '' ? null : Number(value) })
}

function setTrendPreference(value: string): void {
  update((settings) => { settings.trendPreference = value })
}

function setPoolFallback(checked: boolean): void {
  update((settings) => { settings.poolFallback = checked })
}

function movePool(index: number, delta: number): void {
  const nextIndex = index + delta
  if (nextIndex < 0 || nextIndex >= props.modelValue.poolPriority.length) return
  update((settings) => {
    const [item] = settings.poolPriority.splice(index, 1)
    settings.poolPriority.splice(nextIndex, 0, item)
  })
}

function setSourceEnabled(source: 'hotSpot' | 'allianceRank' | 'selectionSquare', checked: boolean): void {
  update((settings) => {
    if (source === 'hotSpot') settings.sources.hotSpotEnabled = checked
    else if (source === 'allianceRank') settings.sources.allianceRankEnabled = checked
    else settings.sources.selectionSquareEnabled = checked
  })
}

function setSourceOption<K extends keyof SelectionFilterSettings['sources']>(key: K, value: SelectionFilterSettings['sources'][K]): void {
  update((settings) => { settings.sources[key] = value })
}

function setRankCategory(pathKey: string): void {
  const category = props.rankCategoryOptions.find((item) => item.path.join('|') === pathKey)
  setSourceOption('rankCategoryPath', category ? [...category.path] : [])
}

function moveSource(index: number, delta: number): void {
  const nextIndex = index + delta
  if (nextIndex < 0 || nextIndex >= props.modelValue.sources.priority.length) return
  update((settings) => {
    const [item] = settings.sources.priority.splice(index, 1)
    settings.sources.priority.splice(nextIndex, 0, item)
  })
}

const sourceLabels: Record<'hotSpot' | 'allianceRank', string> = {
  hotSpot: '热点趋势',
  allianceRank: '联盟榜单'
}
</script>

<template>
  <div class="filter-editor" :class="{ disabled }">
    <div class="editor-title">
      <h2>{{ title }}</h2>
      <button v-if="showSync" class="btn" :disabled="disabled || syncing" @click="emit('sync')">
        {{ syncing ? '同步中…' : '同步百应筛选项' }}
      </button>
    </div>
    <div class="form-grid">
      <label>每轮每账号新增商品目标数（1–10）<input :value="modelValue.candidateLimit" type="number" min="1" max="10" :disabled="disabled" @input="setCandidateLimit(($event.target as HTMLInputElement).value)"></label>
      <label>带货数据走势<select :value="modelValue.trendPreference" :disabled="disabled" @change="setTrendPreference(($event.target as HTMLSelectElement).value)"><option>上升优先</option><option>不限走势</option></select></label>
      <label>销售额阈值<input :value="modelValue.thresholds.salesAmount ?? ''" type="number" min="0" placeholder="不限制" :disabled="disabled" @input="setThreshold('salesAmount', ($event.target as HTMLInputElement).value)"></label>
      <label>销量阈值<input :value="modelValue.thresholds.salesCount ?? ''" type="number" min="0" placeholder="不限制" :disabled="disabled" @input="setThreshold('salesCount', ($event.target as HTMLInputElement).value)"></label>
      <label>转化率阈值（%）<input :value="modelValue.thresholds.conversionRate ?? ''" type="number" min="0" step="0.1" placeholder="不限制" :disabled="disabled" @input="setThreshold('conversionRate', ($event.target as HTMLInputElement).value)"></label>
      <label>浏览量阈值<input :value="modelValue.thresholds.views ?? ''" type="number" min="0" placeholder="不限制" :disabled="disabled" @input="setThreshold('views', ($event.target as HTMLInputElement).value)"></label>
      <label>最低佣金率（%）<input :value="modelValue.thresholds.commissionRate ?? ''" type="number" min="0" max="100" step="0.1" placeholder="不限制" aria-label="最低佣金率百分比" :disabled="disabled" @input="setThreshold('commissionRate', ($event.target as HTMLInputElement).value)"></label>
      <label>最高佣金率（%）<input :value="modelValue.thresholds.commissionRateMax ?? ''" type="number" min="0" max="100" step="0.1" placeholder="不限制" aria-label="最高佣金率百分比" :disabled="disabled" @input="setThreshold('commissionRateMax', ($event.target as HTMLInputElement).value)"></label>
    </div>
    <p class="hint">{{ commissionHint }}</p>
    <label class="check-row"><input type="checkbox" :checked="modelValue.poolFallback" :disabled="disabled" @change="setPoolFallback(($event.target as HTMLInputElement).checked)">四池不足时从池外兜底补足</label>
    <p class="hint">
      筛选目录保存在本机；{{ filterCatalogUpdatedAt ? `上次同步：${new Date(filterCatalogUpdatedAt).toLocaleString()}` : '尚无本地目录，首次同步时需要托管 Chrome 已登录百应' }}。
      三个来源统一按下方数据条件在客户端筛选；佣金率按最低值及可选最高值、体验分按下限核验。
    </p>
    <p class="hint">自动选品使用独立托管页面；执行期间请不要手动操作该页面，其他百应标签可继续使用。</p>
    <section class="platform-filter-section">
      <div class="editor-title"><h3>共享候选池来源</h3><span class="hint">至少启用一处来源；同一商品跨来源去重并保留来源记录。</span></div>
      <div class="platform-filter-multi source-toggles">
        <label class="check-row"><input type="checkbox" :checked="modelValue.sources.hotSpotEnabled" :disabled="disabled" @change="setSourceEnabled('hotSpot', ($event.target as HTMLInputElement).checked)">选品热点</label>
        <label class="check-row"><input type="checkbox" :checked="modelValue.sources.allianceRankEnabled" :disabled="disabled" @change="setSourceEnabled('allianceRank', ($event.target as HTMLInputElement).checked)">带货商品榜</label>
        <label class="check-row"><input type="checkbox" :checked="modelValue.sources.selectionSquareEnabled" :disabled="disabled" @change="setSourceEnabled('selectionSquare', ($event.target as HTMLInputElement).checked)">选品广场</label>
      </div>
      <div v-if="modelValue.sources.allianceRankEnabled" class="form-grid">
        <label>榜单<select :value="modelValue.sources.rankBoard" :disabled="disabled" @change="setSourceOption('rankBoard', ($event.target as HTMLSelectElement).value as SelectionFilterSettings['sources']['rankBoard'])"><option>爆款榜</option><option>趋势榜</option><option>带货榜</option><option>常销榜</option></select></label>
        <label>榜单数据周期<select :value="modelValue.sources.rankPeriod" :disabled="disabled" @change="setSourceOption('rankPeriod', ($event.target as HTMLSelectElement).value as SelectionFilterSettings['sources']['rankPeriod'])"><option>实时</option><option>昨日</option><option>近7日</option><option>近30日</option></select></label>
        <label>榜单载体<select :value="modelValue.sources.rankCarrier" :disabled="disabled" @change="setSourceOption('rankCarrier', ($event.target as HTMLSelectElement).value as SelectionFilterSettings['sources']['rankCarrier'])"><option>不限载体</option><option>短视频</option><option>直播</option><option>橱窗</option><option>图文</option></select></label>
        <label>榜单分类<select :value="modelValue.sources.rankCategoryPath.join('|')" :disabled="disabled" @change="setRankCategory(($event.target as HTMLSelectElement).value)">
          <option value="">总榜（全部分类）</option>
          <option v-for="category in rankCategoryOptions" :key="category.path.join('|')" :value="category.path.join('|')">{{ category.label }}</option>
        </select></label>
        <label>榜单售价区间<select :value="modelValue.sources.rankPriceRange" :disabled="disabled" @change="setSourceOption('rankPriceRange', ($event.target as HTMLSelectElement).value as SelectionFilterSettings['sources']['rankPriceRange'])">
          <option>不限</option><option>1-10</option><option>10-50</option><option>50-100</option><option>100-500</option><option>500-5000</option><option>5000+</option>
        </select></label>
      </div>
      <p v-if="modelValue.sources.allianceRankEnabled" class="hint">榜单佣金率、体验分沿用上方统一阈值；售价与分类只作用于榜单。分类按百应稳定 ID 选中并回读，原有排除类目继续生效。</p>
      <div v-if="modelValue.sources.hotSpotEnabled" class="form-grid">
        <label>热点最低曝光热度（万次）<input :value="modelValue.sources.hotSpotMinExposure ?? ''" type="number" min="0" step="0.01" placeholder="不限" :disabled="disabled" @input="setSourceOption('hotSpotMinExposure', ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value))"></label>
        <label>热点最低关联商品数<input :value="modelValue.sources.hotSpotMinRelatedProducts ?? ''" type="number" min="0" placeholder="不限" :disabled="disabled" @input="setSourceOption('hotSpotMinRelatedProducts', ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value))"></label>
      </div>
      <p class="hint">热点分类遵循下方类目范围和排除类目。榜单默认爆款榜，可改选其他三榜；页面筛选必须回读确认。平台未提供某项必需数据时，该商品不计入达标池量。</p>
      <ol class="pool-list source-priority-list">
        <li v-for="(source, index) in modelValue.sources.priority" :key="source">
          <span>{{ index + 1 }}. {{ sourceLabels[source] }}</span>
          <span><button :disabled="disabled || index === 0" @click="moveSource(index, -1)">↑</button><button :disabled="disabled || index === modelValue.sources.priority.length - 1" @click="moveSource(index, 1)">↓</button></span>
        </li>
      </ol>
      <p class="hint">来源影响采集范围；来源和商品筛选结果都记录在候选池中。</p>
    </section>
    <div v-if="!Object.values(filterGroups).some((items) => items.length)" class="empty-hint">本机尚无筛选目录。首次使用时保持托管 Chrome 已登录百应并同步；以后进入本页会直接使用本地缓存。</div>
    <section v-for="section in sections" :key="section.title" class="platform-filter-section">
      <h3>{{ section.title }}</h3>
      <div class="platform-filter-grid">
        <div v-if="section.title === '商品信息'" class="platform-filter-field commission-threshold-field">
          <span>最低体验分</span>
          <div class="commission-threshold-input">
            <span>≥</span>
            <input
              :value="modelValue.thresholds.experienceScore ?? ''"
              type="number"
              min="0"
              max="100"
              step="0.1"
              placeholder="不限"
              aria-label="最低体验分"
              :disabled="disabled"
              @input="setThreshold('experienceScore', ($event.target as HTMLInputElement).value)"
            >
          </div>
          <small>按来源返回的体验分筛选；未返回体验分时不满足已设置的门槛。</small>
        </div>
        <div v-for="field in section.fields" :key="field.label" class="platform-filter-field">
          <span>{{ field.label }}</span>
          <select
            v-if="!field.multiple"
            :value="selectedFilterValues(section.title, field.label)[0] ?? ''"
            :disabled="disabled"
            @change="setFilterValue(section.title, field.label, ($event.target as HTMLSelectElement).value, true, false)"
          >
            <option value="">不限</option>
            <option v-for="option in field.options.filter((item) => item !== '不限')" :key="option" :value="option">{{ option }}</option>
          </select>
          <div v-else class="platform-filter-multi">
            <label v-for="option in field.options" :key="option" class="check-row">
              <input type="checkbox" :disabled="disabled" :checked="selectedFilterValues(section.title, field.label).includes(option)" @change="setFilterValue(section.title, field.label, option, ($event.target as HTMLInputElement).checked, true)">
              {{ option }}
            </label>
          </div>
        </div>
      </div>
    </section>
    <section class="platform-filter-section">
      <h3>特色货品</h3>
      <div v-if="featureOptions.length" class="platform-filter-chips">
        <label v-for="option in featureOptions" :key="option" class="filter-chip">
          <input type="checkbox" :disabled="disabled" :checked="selectedFilterValues('特色货品', option).length > 0" @change="setFilterValue('特色货品', option, option, ($event.target as HTMLInputElement).checked, true)">
          {{ option }}
        </label>
      </div>
      <p v-else class="hint">特色货品选项会随百应页面动态同步。</p>
    </section>
    <section class="platform-filter-section">
      <div class="editor-title"><h3>商品池优先级</h3><span class="hint">池内优先，不足时按上方设置从池外兜底。</span></div>
      <ol class="pool-list">
        <li v-for="(pool, index) in modelValue.poolPriority" :key="pool">
          <span>{{ index + 1 }}. {{ pool }}</span>
          <span><button :disabled="disabled || index === 0" @click="movePool(index, -1)">↑</button><button :disabled="disabled || index === modelValue.poolPriority.length - 1" @click="movePool(index, 1)">↓</button></span>
        </li>
      </ol>
    </section>
  </div>
</template>

<style scoped>
.filter-editor { display: flex; flex-direction: column; gap: 9px; }
.editor-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.editor-title h2, .editor-title h3 { margin: 0; font-size: 15px; }
.form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.form-grid > label { display: flex; flex-direction: column; gap: 5px; color: var(--text-muted); font-size: 12px; }
.form-grid input, .form-grid select, .platform-filter-field select, .commission-threshold-input input { min-width: 0; padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-app); color: var(--text); }
.check-row { display: flex; align-items: center; gap: 6px; margin: 0; color: var(--text-muted); font-size: 12px; }
.hint { margin: 0; color: var(--text-muted); font-size: 12px; line-height: 1.55; }
.empty-hint { padding: 10px 12px; border-radius: 6px; background: var(--bg-placeholder); color: var(--text-muted); font-size: 12px; }
.platform-filter-section { display: flex; flex-direction: column; gap: 8px; padding-top: 8px; border-top: 1px solid var(--border); }
.platform-filter-section h3 { margin: 0; font-size: 13px; }
.platform-filter-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }
.platform-filter-field { display: flex; flex-direction: column; gap: 5px; color: var(--text-muted); font-size: 12px; }
.commission-threshold-input { display: flex; align-items: center; gap: 7px; }
.commission-threshold-input input { flex: 1; width: 0; }
.commission-threshold-field small { color: var(--text-muted); font-size: 11px; line-height: 1.4; }
.platform-filter-multi, .platform-filter-chips { display: flex; flex-wrap: wrap; gap: 7px 12px; }
.filter-chip { display: inline-flex; align-items: center; gap: 5px; padding: 5px 8px; border: 1px solid var(--border); border-radius: 16px; color: var(--text-muted); font-size: 12px; }
.pool-list { display: flex; flex-direction: column; gap: 5px; margin: 0; padding: 0; list-style: none; }
.pool-list li { display: flex; align-items: center; justify-content: space-between; padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; }
.pool-list li > span:last-child { display: flex; gap: 6px; }
.pool-list button { padding: 3px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-app); color: var(--text); }
.disabled { opacity: .6; }
@media (max-width: 820px) { .form-grid, .platform-filter-grid { grid-template-columns: 1fr; } }
</style>
