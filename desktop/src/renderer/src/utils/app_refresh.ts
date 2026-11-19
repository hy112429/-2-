import { onBeforeUnmount, onMounted } from 'vue'

type RefreshTask = () => void | Promise<unknown>
const mountedTasks = new Set<RefreshTask>()

/** 当前页面登记只读数据刷新；卸载后自动退出全局刷新。 */
export function useAppRefresh(task: RefreshTask): void {
  onMounted(() => { mountedTasks.add(task) })
  onBeforeUnmount(() => { mountedTasks.delete(task) })
}

export async function refreshMountedViews(): Promise<void> {
  const results = await Promise.allSettled([...mountedTasks].map((task) => Promise.resolve().then(task)))
  const failure = results.find((result): result is PromiseRejectedResult => result.status === 'rejected')
  if (failure) throw failure.reason
}
