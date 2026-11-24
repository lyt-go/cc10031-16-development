import { request } from './client'

/** 缺测证据包工作流：构建/固化、离线复算、领取签收、检修回执、复查扩段、派工与下载。 */

export type BuildPayload = {
  report_id: number
  expected_interval_seconds?: number
  lineage_id?: number
  batch_no?: string
  extra_review_range?: Record<string, string>
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  const data = (await response.json()) as T & { error?: string; detail?: string }
  if (!response.ok) {
    throw new Error(data.detail ?? data.error ?? '证据包操作失败')
  }
  return data
}

export function prepareEvidencePackage(payload: BuildPayload) {
  return postJson('/api/gap_evidence/prepare', payload)
}

export function freezeEvidencePackage(packageId: number) {
  return postJson(`/api/gap_evidence/${packageId}/freeze`)
}

export function buildEvidencePackage(payload: BuildPayload) {
  return postJson('/api/gap_evidence/build', payload)
}

export function recomputeEvidencePackage(packageId: number) {
  return postJson(`/api/gap_evidence/${packageId}/recompute`)
}

export function createClaim(packageId: number, receiver: string, claimMode: string) {
  return postJson(`/api/gap_evidence/${packageId}/claims`, {
    receiver,
    claim_mode: claimMode,
  })
}

export function issueClaim(requestNo: string) {
  return postJson(`/api/gap_evidence/claims/${requestNo}/issue`)
}

export function confirmReceipt(packageId: number, receiver: string, claimMode: string) {
  return postJson(`/api/gap_evidence/${packageId}/receipts`, {
    receiver,
    claim_mode: claimMode,
  })
}

export function registerMaintenance(
  packageId: number,
  gapIds: string[],
  handler: string,
  conclusion: string,
) {
  return postJson(`/api/gap_evidence/${packageId}/maintenance`, {
    gap_ids: gapIds,
    handler,
    conclusion,
  })
}

export function confirmAbnormalReview(
  packageId: number,
  extraRange: Record<string, string>,
  basis: string,
) {
  return postJson(`/api/gap_evidence/${packageId}/reviews`, {
    extra_range: extraRange,
    basis,
  })
}

export function exportDispatchScope(packageId: number) {
  return postJson(`/api/gap_evidence/${packageId}/dispatch`)
}

/** 下载真实验证包 zip；重新领取时后端保留同一下载书签。 */
export async function downloadEvidencePackage(packageId: number): Promise<void> {
  const response = await request(`/api/gap_evidence/${packageId}/download`)
  if (!response.ok) throw new Error('证据包下载失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `gap-evidence-${packageId}.zip`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}
