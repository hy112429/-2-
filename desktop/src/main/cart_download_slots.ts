/** 多账号下载共用有限工作槽；同账号的顺序仍由调用方保证。 */
const MAX_CONCURRENT_CART_DOWNLOADS = 4
let active = 0
const waiters: Array<() => void> = []

export async function withCartDownloadSlot<T>(work: () => Promise<T>): Promise<T> {
  if (active < MAX_CONCURRENT_CART_DOWNLOADS) {
    active += 1
  } else {
    await new Promise<void>((resolve) => waiters.push(resolve))
  }
  try {
    return await work()
  } finally {
    const next = waiters.shift()
    if (next) next()
    else active -= 1
  }
}
