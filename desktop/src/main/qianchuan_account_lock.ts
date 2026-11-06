const busyAccounts = new Set<number>()

export function reserveQianchuanAccount(id: number): boolean {
  if (busyAccounts.has(id)) return false
  busyAccounts.add(id)
  return true
}

export function releaseQianchuanAccount(id: number): void {
  busyAccounts.delete(id)
}
