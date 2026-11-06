import { app } from 'electron'
import { createHash, randomBytes } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { hostname, networkInterfaces } from 'node:os'
import { join } from 'node:path'

const MACHINE_ID_PATTERN = /^[a-f0-9]{64}$/

/** 首次生成后保存在当前客户端 userData；更新客户端不会更换绑定身份。 */
export function getOrCreateCenterMachineId(): string {
  const file = join(app.getPath('userData'), 'center-machine-id.json')
  if (existsSync(file)) {
    const saved = JSON.parse(readFileSync(file, 'utf8')) as { machineId?: string }
    if (!saved.machineId || !MACHINE_ID_PATTERN.test(saved.machineId)) {
      throw new Error('本机绑定标识损坏，请先人工核对原绑定，不自动生成新标识。')
    }
    return saved.machineId
  }
  const macs = Object.values(networkInterfaces()).flatMap((rows) => rows ?? [])
    .filter((row) => !row.internal && row.mac && row.mac !== '00:00:00:00:00:00')
    .map((row) => row.mac.toLowerCase()).sort()
  const machineId = createHash('sha256')
    .update(hostname()).update('\0').update(macs.join(',')).update('\0').update(randomBytes(32))
    .digest('hex')
  mkdirSync(app.getPath('userData'), { recursive: true })
  writeFileSync(file, JSON.stringify({ machineId }), { encoding: 'utf8', flag: 'wx', mode: 0o600 })
  return machineId
}

/** 服务端地址未配置时保持二期客户端的本机工作方式；配置后每次启动幂等登记。 */
export async function registerCenterMachineOnStartup(): Promise<void> {
  const raw = process.env['TOUGUANG_CENTER_URL']?.trim()
  if (!raw) return
  try {
    const base = new URL(raw)
    if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
      throw new Error('服务端地址必须是无凭证的 HTTP(S) 根地址')
    }
    const machineId = getOrCreateCenterMachineId()
    const endpoint = new URL('/api/clients/register', base)
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ machineId, machineName: hostname().slice(0, 100) }),
      signal: AbortSignal.timeout(8000)
    })
    if (!response.ok) throw new Error(`登记接口返回 HTTP ${response.status}`)
    console.info('中心服务端机器登记已发送；管理员仍需确认绑定。')
  } catch (error) {
    console.warn(`中心服务端机器登记暂未完成：${error instanceof Error ? error.message : '未知错误'}`)
  }
}
