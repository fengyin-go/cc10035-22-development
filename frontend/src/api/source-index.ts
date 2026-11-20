import { request } from './client'

/** 实体来源索引、重放清单与配置版本快照接口。 */
export type SourceHit = {
  实体ID: string
  实体名称?: string
  字段: string
  页面URI: string
  解析签名: string
  配置ID: number | null
  配置版本: string
  字段签名?: string
  签名状态?: string
  索引ID?: string
}

export type ReplayItem = {
  重放ID: string
  页面URI: string
  字段: string
  解析签名: string
  配置版本: string
  状态: string
  命中数: number
  命中实体: SourceHit[]
  配置快照: Record<string, unknown> | null
  复验?: ReplayResult | null
}

export type ReplayDetail = {
  实体ID: string
  字段: string
  解析签名: string
  结果: string
  原因?: string
  实际读取值?: string
}

export type ReplayResult = {
  重放ID: string
  状态: string
  实际读取字段: string[]
  字段核对: ReplayDetail[]
  配置已更正?: boolean
  配置更正说明?: string
  无法复验版本?: string[]
  无法复验明细?: { 版本: string; 实体ID: string; 原因: string }[]
  不能复验说明?: string
}

export type ConfigSnapshot = {
  配置ID: number
  配置版本: string
  页面URI: string
  解析引擎: string
  选择器规则: string
  输出Schema: string
  解析签名: string
  字段列表: string[]
  快照时间: string
}

async function jsonRequest(path: string, init?: RequestInit) {
  const response = await request(path, init)
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? '实体来源索引请求失败')
  }
  return payload
}

export async function searchSourceIndex(
  query: Record<string, string | boolean | undefined>,
): Promise<{ hits: SourceHit[]; total: number; replay?: ReplayItem; replays?: ReplayItem[] }> {
  const params = Object.fromEntries(
    Object.entries(query)
      .filter(([, value]) => value !== undefined && value !== '' && value !== false)
      .map(([key, value]) => [key, String(value)]),
  )
  return await jsonRequest(`/api/entity-source-index?${new URLSearchParams(params).toString()}`)
}

export async function searchAndWriteback(query: Record<string, string | boolean | undefined>) {
  return await jsonRequest('/api/entity-source-index/search', {
    method: 'POST',
    body: JSON.stringify({ ...query, writeback: true }),
  })
}

export async function writebackHits(
  query: Record<string, string | boolean | undefined>,
  entityIds: string[],
): Promise<{ ok: boolean; total?: number; replays?: ReplayItem[]; reason?: string }> {
  return await jsonRequest('/api/entity-source-index/writeback', {
    method: 'POST',
    body: JSON.stringify({ query, entity_ids: entityIds }),
  })
}

export async function listReplays(filters: Record<string, string> = {}): Promise<{
  items: ReplayItem[]
  total: number
}> {
  const query = new URLSearchParams(
    Object.fromEntries(Object.entries(filters).filter(([, value]) => value)),
  ).toString()
  return await jsonRequest(`/api/replay-manifest?${query}`)
}

export async function submitReplayToDom(replayId: string): Promise<ReplayResult> {
  return await jsonRequest(`/api/replay-manifest/${replayId}/submit-to-dom`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export async function domReplay(replayId: string): Promise<ReplayResult> {
  return await jsonRequest(`/api/replay-manifest/${replayId}/dom-replay`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export async function listConfigSnapshots(configId: number): Promise<{
  items: ConfigSnapshot[]
  current_version: string
}> {
  return await jsonRequest(`/api/dom-parser-configs/${configId}/snapshots`)
}

export async function reviseConfig(configId: number, changes: Record<string, string>) {
  return await jsonRequest(`/api/dom-parser-configs/${configId}/revise`, {
    method: 'POST',
    body: JSON.stringify({ changes, operator: '当前值班员' }),
  })
}

export async function pushSignature(configId: number) {
  return await jsonRequest(`/api/dom-parser-configs/${configId}/push-signature`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export async function listUnsigned(pageUri: string = '') {
  const query = pageUri ? `?page_uri=${encodeURIComponent(pageUri)}` : ''
  return await jsonRequest(`/api/entity-source-index/unsigned${query}`)
}
