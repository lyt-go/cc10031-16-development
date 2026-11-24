"""缺测证据包专用路由：构建、离线复算、领取签收、检修回执、复查扩段与派工下载。

通用资源路由仍承担列表/详情/动作；这里只暴露证据包特有的业务动作，
路由层不做业务判断，全部委派 GapEvidenceService。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services.gap_evidence import GapEvidenceService

router = APIRouter(prefix="/api/gap_evidence", tags=["缺测证据包"])


def _svc() -> GapEvidenceService:
    return GapEvidenceService()


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class BuildPayload(BaseModel):
    report_id: int
    expected_interval_seconds: float | None = None
    lineage_id: int | None = None
    batch_no: str | None = None
    extra_review_range: dict[str, str] | None = None


class ClaimCreatePayload(BaseModel):
    receiver: str
    claim_mode: str = Field(description="continuation 续领原检查 / new_check 开始新核对")


class ReceiptPayload(BaseModel):
    receiver: str
    claim_mode: str


class MaintenancePayload(BaseModel):
    gap_ids: list[str]
    handler: str
    conclusion: str


class ReviewPayload(BaseModel):
    extra_range: dict[str, str]
    basis: str


@router.post("/build")
def build_package(payload: BuildPayload) -> dict[str, Any]:
    """一步构建（建草稿并固化）。"""
    return _unwrap(_svc().build_evidence_package(
        payload.report_id,
        expected_interval_seconds=payload.expected_interval_seconds,
        lineage_id=payload.lineage_id,
        batch_no=payload.batch_no,
        extra_review_range=payload.extra_review_range,
    ))


@router.post("/prepare")
def prepare_package(payload: BuildPayload) -> dict[str, Any]:
    """创建证据包草稿，等待固化确认（续领申请可在此阶段先创建）。"""
    return _unwrap(_svc().prepare_evidence_package(
        payload.report_id,
        expected_interval_seconds=payload.expected_interval_seconds,
        lineage_id=payload.lineage_id,
        batch_no=payload.batch_no,
        extra_review_range=payload.extra_review_range,
    ))


@router.post("/{package_id}/freeze")
def freeze_package(package_id: int) -> dict[str, Any]:
    """固化确认新证据包，此后续领申请才允许发出。"""
    return _unwrap(_svc().freeze_evidence_package(package_id))


@router.post("/{package_id}/recompute")
def recompute(package_id: int) -> dict[str, Any]:
    return _unwrap(_svc().recompute_package(package_id))


@router.post("/{package_id}/claims")
def create_claim(package_id: int, payload: ClaimCreatePayload) -> dict[str, Any]:
    return _unwrap(_svc().create_claim_request(
        package_id, payload.receiver, payload.claim_mode))


@router.post("/claims/{request_no}/issue")
def issue_claim(request_no: str) -> dict[str, Any]:
    return _unwrap(_svc().issue_claim_request(request_no))


@router.post("/{package_id}/receipts")
def confirm_receipt(package_id: int, payload: ReceiptPayload) -> dict[str, Any]:
    return _unwrap(_svc().confirm_receipt(
        package_id, payload.receiver, payload.claim_mode))


@router.post("/{package_id}/maintenance")
def maintenance(package_id: int, payload: MaintenancePayload) -> dict[str, Any]:
    return _unwrap(_svc().register_maintenance_receipt(
        package_id, payload.gap_ids, payload.handler, payload.conclusion))


@router.post("/{package_id}/reviews")
def review(package_id: int, payload: ReviewPayload) -> dict[str, Any]:
    return _unwrap(_svc().confirm_abnormal_review(
        package_id, payload.extra_range, payload.basis))


@router.post("/{package_id}/dispatch")
def dispatch(package_id: int) -> dict[str, Any]:
    return _unwrap(_svc().export_dispatch_scope(package_id))


@router.get("/{package_id}/download")
def download_package(package_id: int) -> FileResponse:
    """把真实证据包目录打成 zip 供接收方离线下载，并保留下载书签。"""
    svc = _svc()
    entry = svc.get_entry(package_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"缺测证据包 {package_id} 不存在")
    package_dir = Path(entry["package_dir"])
    if not package_dir.exists():
        raise HTTPException(status_code=404, detail="证据包文件缺失，无法下载")

    # 重新领取时保留原下载书签，避免重复取得已核对的缺口。
    bookmark = svc.ensure_download_bookmark(package_id)
    assert bookmark

    archive_base = package_dir.parent / f"{package_dir.name}_archive"
    archive_path = shutil.make_archive(str(archive_base), "zip", root_dir=str(package_dir))
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"{package_dir.name}.zip",
    )
