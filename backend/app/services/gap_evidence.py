"""缺测证据包领域服务：为时序质量报告生成可离线逐点复算的缺测证据包。

证据包把原告警（质量报告）、采样序列（原始时刻/标定单位/预期间隔）、
空缺区间与采集血缘（所用采样序列、上送批次）固化到同一版本目录，
接收方下载真实文件后可离线按同一口径逐点复算。

围绕证据包还维护：
- 领取签收（续领原检查 / 开始新核对，重领保留下载书签、不重复派单）；
- 检修处置回执（绑定实际核对的缺点与报告版本，已处理缺点不重派）；
- 异常复查确认（原范围外扩段，原未核查整段保持有效可复查）；
- 派工导出（实际派给现场的范围）。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 包内有效观测只允许来自同一个上送批次，跨批次混装直接拒绝。
TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def _parse_ts(value: str) -> datetime:
    for fmt in TS_FORMATS:
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    # 最后兜底 ISO 解析，兼容带时区偏移的时刻。
    return datetime.fromisoformat(str(value))


def _fmt_ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


class GapEvidenceService(BaseModuleService):
    MODULE = "gap_evidence"
    ENTITY = "缺测证据包"
    OPERATION_TERM = "核对"
    LIST_FIELDS = [
        "证据包编号", "报告编号", "测点编号", "包版本", "口径版本",
        "上送批次", "空缺区间数", "已处理缺口", "待派缺口", "领取方式", "包状态",
    ]
    REQUIRED_FIELDS = ["报告编号", "测点编号"]
    STATUS_ORDER = ["草稿", "已固化", "待签收", "已签收", "已回执"]
    ACTION_RULES = {
        "固化证据包": "已固化",
        "提交签收": "待签收",
        "确认签收": "已签收",
        "登记回执": "已回执",
    }
    NEGATIVE_ACTIONS: list[str] = []

    # ------------------------------------------------------------------
    # 证据包构建：按告警编号固化原始时刻、标定单位、预期间隔、空缺区间
    # ------------------------------------------------------------------

    def prepare_evidence_package(
        self,
        report_id: int,
        *,
        expected_interval_seconds: float | None = None,
        lineage_id: int | None = None,
        batch_no: str | None = None,
        extra_review_range: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """创建证据包草稿：完成序列核对与缺口复算，但等待固化确认。

        续领申请可以在草稿阶段创建，必须等固化确认之后才能发出。
        包内有效观测必须来自同一上送批次；序列由采集血缘核对。
        """
        report = store.find("series_quality", report_id)
        if report is None:
            return {"error": f"质量报告 {report_id} 不存在，无法关联原告警"}
        point_key = report["测点编号"]
        report_no = report["报告编号"]
        caliber_version = f"v{int(report_id)}"

        # 1. 采集血缘：核对本包所用采样序列及其上送批次。
        lineage = self._resolve_lineage(point_key, lineage_id)
        lineage_batch = lineage.get("上送批次") if lineage else None
        batch_no = batch_no or lineage_batch
        if not batch_no:
            return {"error": f"测点 {point_key} 缺少采集血缘/上送批次，无法保证包内观测同源"}

        # 2. 标定单位：取该测点最新生效标定记录。
        cal_row = self._latest_calibration(point_key)
        unit_info = {
            "标定编号": cal_row.get("标定编号") if cal_row else "N/A",
            "原始单位": cal_row.get("原始单位", "") if cal_row else "",
            "目标单位": cal_row.get("目标单位", "") if cal_row else "",
            "换算系数": cal_row.get("换算系数", "1") if cal_row else "1",
            "零点偏移": cal_row.get("零点偏移", "0") if cal_row else "0",
        }

        # 3. 读取原始采样序列，并强制同一批次校验。
        observations, batch_error = self._load_batched_observations(point_key, batch_no)
        if batch_error:
            return {"error": batch_error}
        if not observations:
            return {"error": f"测点 {point_key} 在批次 {batch_no} 下没有有效观测，证据包不能固化"}

        timestamps = sorted(_parse_ts(item["timestamp"]) for item in observations)
        interval = float(expected_interval_seconds) if expected_interval_seconds else self._infer_interval(timestamps)

        # 4. 按预期间隔逐点复算空缺区间（原检查口径）。
        gaps = self._compute_gaps(timestamps, interval)

        # 5. 异常复查确认：在原范围外增加一小段，原未核查整段仍保持可复查。
        review = self._build_review_scope(timestamps, extra_review_range)

        # 6. 版本与目录。
        version = self._next_version(report_no)
        package_no = f"EVID-{report_no}-{version}"
        package_dir = self._data_dir("gap_evidence_packages") / package_no
        package_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "证据包编号": package_no,
            "报告编号": report_no,
            "告警编号": report_no,
            "测点编号": point_key,
            "包版本": version,
            "口径版本": caliber_version,
            "检查口径": "相邻有效观测间隔大于 2 倍预期间隔即判定为空缺区间，缺失点数=round(间隔/预期间隔)-1",
            "预期间隔秒": interval,
            "标定单位": unit_info,
            "采样序列": {
                "序列文件": "observations.jsonl",
                "观测点数": len(observations),
                "起始时刻": _fmt_ts(timestamps[0]),
                "截止时刻": _fmt_ts(timestamps[-1]),
                "上送批次": batch_no,
                "血缘链路": lineage.get("链路编号") if lineage else "N/A",
            },
            "空缺区间": gaps,
            "复查范围": review,
            "生成时间": _fmt_ts(datetime.now()),
        }

        # 写真实文件：清单、观测序列、空缺区间、复算脚本、复算说明。
        (package_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(package_dir / "observations.jsonl", "w", encoding="utf-8") as f:
            for item in observations:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        with open(package_dir / "gaps.jsonl", "w", encoding="utf-8") as f:
            for gap in gaps:
                f.write(json.dumps(gap, ensure_ascii=False) + "\n")
        self._write_recompute_script(package_dir)
        readme = self._build_readme(manifest)
        (package_dir / "README.md").write_text(readme, encoding="utf-8")

        # 跨版本继承：同份报告包里已经处置的缺点，新版本不得再次登记为待办。
        handled_gap_ids = self._inherited_handled_gaps(report_no, gaps)

        entry = {
            "证据包编号": package_no,
            "报告编号": report_no,
            "测点编号": point_key,
            "包版本": version,
            "口径版本": caliber_version,
            "上送批次": batch_no,
            "空缺区间数": str(len(gaps)),
            "已处理缺口": str(len(handled_gap_ids)),
            "待派缺口": str(len(gaps) - len(handled_gap_ids)),
            "领取方式": "",
            "包状态": "草稿",
            "package_dir": str(package_dir),
            "download_bookmark": "",
            "gaps": gaps,
            "review_scope": review,
            "dispatched_gap_ids": [],
            "handled_gap_ids": handled_gap_ids,
            "receipts": [],
        }
        row = self._auto_create_seed(self.MODULE, entry)
        row["status"] = "草稿"
        row["pending"] = True
        return {
            "ok": True,
            "package_id": row["id"],
            "证据包编号": package_no,
            "报告编号": report_no,
            "测点编号": point_key,
            "包版本": version,
            "上送批次": batch_no,
            "观测点数": len(observations),
            "空缺区间数": len(gaps),
            "package_dir": str(package_dir),
            "复查范围": review,
            "已继承处置缺口": handled_gap_ids,
        }

    def freeze_evidence_package(self, package_id: int) -> dict[str, Any]:
        """固化确认证据包：写入清单校验和，草稿转已固化，续领申请此后才可发出。"""
        entry = store.find(self.MODULE, package_id)
        if entry is None:
            return {"error": f"缺测证据包 {package_id} 不存在"}
        if entry.get("包状态") != "草稿":
            return {"error": f"证据包 {entry['证据包编号']} 已固化，不能重复确认"}
        manifest_file = Path(entry["package_dir"]) / "manifest.json"
        checksum = self._checksum_file(manifest_file)
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["清单校验"] = checksum
        manifest_file.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        entry["包状态"] = "已固化"
        entry["status"] = "已固化"
        return {
            "ok": True,
            "证据包编号": entry["证据包编号"],
            "包版本": entry["包版本"],
            "清单校验": checksum,
            "包状态": "已固化",
        }

    def build_evidence_package(self, report_id: int, **kwargs: Any) -> dict[str, Any]:
        """便捷入口：建草稿并立即固化确认（一步到位的构建）。"""
        prepared = self.prepare_evidence_package(report_id, **kwargs)
        if prepared.get("error"):
            return prepared
        frozen = self.freeze_evidence_package(prepared["package_id"])
        if frozen.get("error"):
            return frozen
        return {**prepared, "清单校验": frozen["清单校验"], "包状态": frozen["包状态"]}

    # ------------------------------------------------------------------
    # 离线复算：接收方下载真实验证包文件后，按同一口径逐点重算
    # ------------------------------------------------------------------

    def recompute_package(self, package_id: int) -> dict[str, Any]:
        """从证据包目录重新逐点复算空缺区间，并与固化口径逐项核对。"""
        entry = store.find(self.MODULE, package_id)
        if entry is None:
            return {"error": f"缺测证据包 {package_id} 不存在"}
        package_dir = Path(entry["package_dir"])
        manifest_file = package_dir / "manifest.json"
        obs_file = package_dir / "observations.jsonl"
        if not manifest_file.exists() or not obs_file.exists():
            return {"error": "证据包文件不完整，无法离线复算"}

        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        interval = float(manifest["预期间隔秒"])
        observations = self._read_jsonl(obs_file)
        timestamps = sorted(_parse_ts(item["timestamp"]) for item in observations)
        recomputed = self._compute_gaps(timestamps, interval)
        frozen = manifest["空缺区间"]

        matched = len(recomputed) == len(frozen) and all(
            recomputed[i]["缺口编号"] == frozen[i]["缺口编号"]
            and recomputed[i]["缺失起点"] == frozen[i]["缺失起点"]
            and recomputed[i]["缺失终点"] == frozen[i]["缺失终点"]
            and recomputed[i]["缺失点数"] == frozen[i]["缺失点数"]
            for i in range(len(frozen))
        )

        # 复算结果落盘，保留可追溯的离线复算痕迹。
        recompute_record = {
            "证据包编号": entry["证据包编号"],
            "包版本": entry["包版本"],
            "复算时间": _fmt_ts(datetime.now()),
            "观测点数": len(observations),
            "预期间隔秒": interval,
            "复算空缺数": len(recomputed),
            "固化空缺数": len(frozen),
            "逐点一致": matched,
        }
        self._write_jsonl(package_dir / "recompute_results.jsonl", [recompute_record])
        return {
            "ok": matched,
            "证据包编号": entry["证据包编号"],
            "包版本": entry["包版本"],
            "观测点数": len(observations),
            "复算空缺区间": recomputed,
            "固化空缺区间": frozen,
            "逐点一致": matched,
        }

    # ------------------------------------------------------------------
    # 领取签收：续领原检查 / 开始新核对；重领保留下载书签、不重复派单
    # ------------------------------------------------------------------

    def create_claim_request(
        self, package_id: int, receiver: str, claim_mode: str
    ) -> dict[str, Any]:
        """创建领取申请（草稿态）。claim_mode: continuation 续领 / new_check 新核对。"""
        entry = store.find(self.MODULE, package_id)
        if entry is None:
            return {"error": f"缺测证据包 {package_id} 不存在"}
        if claim_mode not in ("continuation", "new_check"):
            return {"error": "领取方式必须是 continuation（续领原检查）或 new_check（开始新核对）"}
        request_no = f"CLAIM-{entry['证据包编号']}-{int(time.time() * 1000) % 100000}"
        claim_request = {
            "申请编号": request_no,
            "证据包编号": entry["证据包编号"],
            "报告编号": entry["报告编号"],
            "包版本": entry["包版本"],
            "接收方": receiver,
            "领取方式": "续领原检查" if claim_mode == "continuation" else "开始新核对",
            "claim_mode": claim_mode,
            "状态": "待发出",
            "创建时间": _fmt_ts(datetime.now()),
        }
        store.rows("gap_evidence_claims").append(claim_request)
        return {"ok": True, **claim_request}

    def issue_claim_request(self, request_no: str) -> dict[str, Any]:
        """发出领取申请；续领须在新证据包确认之后才允许发出。"""
        claim = self._find_claim(request_no)
        if claim is None:
            return {"error": f"领取申请 {request_no} 不存在"}
        if claim["状态"] != "待发出":
            return {"error": f"领取申请 {request_no} 已发出，不能重复发出"}
        entry = self._find_by_package_no(claim["证据包编号"])
        # 续领申请在新的证据包确认之前创建、之后发出：草稿态不允许发出。
        if entry.get("包状态") == "草稿":
            return {"error": "新证据包尚未固化确认，续领申请不能发出"}
        claim["状态"] = "已发出"
        claim["发出时间"] = _fmt_ts(datetime.now())
        return {"ok": True, **claim}

    def confirm_receipt(
        self, package_id: int, receiver: str, claim_mode: str
    ) -> dict[str, Any]:
        """接收方签收证据包，回执同时给出已处理缺口与待派出其余时段。"""
        entry = store.find(self.MODULE, package_id)
        if entry is None:
            return {"error": f"缺测证据包 {package_id} 不存在"}
        if entry.get("包状态") == "草稿":
            return {"error": f"证据包 {entry['证据包编号']} 尚未固化确认，不能签收"}
        if claim_mode not in ("continuation", "new_check"):
            return {"error": "领取方式必须是 continuation（续领原检查）或 new_check（开始新核对）"}

        handled = set(entry.get("handled_gap_ids", []))
        all_gaps = entry.get("gaps", [])
        # 已处理的首个缺口不能因同份报告包重领被再次登记为待办。
        pending_gaps = [g for g in all_gaps if g["缺口编号"] not in handled]

        bookmark = entry.get("download_bookmark") or self._download_package(package_id)
        entry["download_bookmark"] = bookmark
        entry["领取方式"] = "续领原检查" if claim_mode == "continuation" else "开始新核对"
        entry["包状态"] = "已签收"
        entry["status"] = "已签收"
        entry["待派缺口"] = str(len(pending_gaps))
        entry["已处理缺口"] = str(len(handled))

        receipt = {
            "签收单号": f"RCPT-{entry['证据包编号']}-{int(time.time()*1000) % 100000}",
            "证据包编号": entry["证据包编号"],
            "报告编号": entry["报告编号"],
            "包版本": entry["包版本"],
            "接收方": receiver,
            "领取方式": entry["领取方式"],
            "下载书签": bookmark,
            "已处理缺口": sorted(handled),
            "待派缺口": [g["缺口编号"] for g in pending_gaps],
            "待派时段": [
                {"缺口编号": g["缺口编号"], "缺失起点": g["缺失起点"], "缺失终点": g["缺失终点"]}
                for g in pending_gaps
            ],
            "签收时间": _fmt_ts(datetime.now()),
        }
        store.rows("gap_evidence_receipts").append(receipt)
        self._write_jsonl(
            Path(entry["package_dir"]) / "receipts.jsonl", [receipt])
        return {"ok": True, **receipt}

    # ------------------------------------------------------------------
    # 检修处置回执：绑定实际核对的缺点与报告版本，已处置缺口不重派
    # ------------------------------------------------------------------

    def register_maintenance_receipt(
        self, package_id: int, gap_ids: list[str], handler: str, conclusion: str
    ) -> dict[str, Any]:
        """登记正式检修处置回执，说明本次实际领用范围与版本。"""
        entry = store.find(self.MODULE, package_id)
        if entry is None:
            return {"error": f"缺测证据包 {package_id} 不存在"}
        if entry.get("包状态") == "草稿":
            return {"error": f"证据包 {entry['证据包编号']} 尚未固化确认，不能登记检修回执"}
        known = {g["缺口编号"] for g in entry.get("gaps", [])}
        unknown = [gid for gid in gap_ids if gid not in known]
        if unknown:
            return {"error": f"缺口 {unknown} 不属于证据包 {entry['证据包编号']} 的核对范围"}
        already = [gid for gid in gap_ids if gid in entry.get("handled_gap_ids", [])]
        if already:
            # 已处置首个缺口不能重派、不能重复登记。
            return {"error": f"缺口 {already} 已处置，不能重复登记为待办或再次回执"}

        handled = set(entry.get("handled_gap_ids", []))
        handled.update(gap_ids)
        entry["handled_gap_ids"] = sorted(handled)
        entry["已处理缺口"] = str(len(handled))
        entry["待派缺口"] = str(len(known) - len(handled))
        entry["包状态"] = "已回执"
        entry["status"] = "已回执"

        maintenance_receipt = {
            "检修回执号": f"MAINT-{entry['证据包编号']}-{int(time.time()*1000) % 100000}",
            "证据包编号": entry["证据包编号"],
            "报告编号": entry["报告编号"],
            "包版本": entry["包版本"],
            "口径版本": entry["口径版本"],
            "实际领用范围": [g for g in entry["gaps"] if g["缺口编号"] in gap_ids],
            "核对缺点": sorted(gap_ids),
            "检修人": handler,
            "处置结论": conclusion,
            "登记时间": _fmt_ts(datetime.now()),
        }
        entry.setdefault("receipts", []).append(maintenance_receipt)
        store.rows("gap_evidence_maintenance").append(maintenance_receipt)
        self._write_jsonl(
            Path(entry["package_dir"]) / "maintenance_receipts.jsonl",
            [maintenance_receipt])
        return {"ok": True, **maintenance_receipt}

    # ------------------------------------------------------------------
    # 异常复查确认：原范围外增加一小段，扩大后的范围有确认依据
    # ------------------------------------------------------------------

    def confirm_abnormal_review(
        self, package_id: int, extra_range: dict[str, str], basis: str
    ) -> dict[str, Any]:
        """核准异常复查：在原核查范围之外追加一小段可复查范围。"""
        entry = store.find(self.MODULE, package_id)
        if entry is None:
            return {"error": f"缺测证据包 {package_id} 不存在"}
        if entry.get("包状态") == "草稿":
            return {"error": f"证据包 {entry['证据包编号']} 尚未固化确认，不能核准复查扩段"}
        try:
            extra_start = _parse_ts(extra_range["start"])
            extra_end = _parse_ts(extra_range["end"])
        except (KeyError, ValueError):
            return {"error": "复查扩段需要合法的 start/end 时刻"}
        if extra_end <= extra_start:
            return {"error": "复查扩段结束时刻必须晚于开始时刻"}

        review = entry.get("review_scope", {})
        original = review.get("原核查范围", {})
        # 扩段必须落在原范围之外，不能覆盖原来未核查的整段。
        if original:
            orig_end = _parse_ts(original["end"])
            if extra_start < orig_end:
                return {"error": "复查扩段必须在原核查范围之外，不能侵入原未核查整段"}

        extra_segment = {
            "start": _fmt_ts(extra_start),
            "end": _fmt_ts(extra_end),
            "确认依据": basis,
            "核准时间": _fmt_ts(datetime.now()),
        }
        review.setdefault("扩段", []).append(extra_segment)
        review["扩大后范围"] = {
            "start": original.get("start", extra_segment["start"]),
            "end": extra_segment["end"],
        }
        entry["review_scope"] = review

        confirmation = {
            "复查确认号": f"REV-{entry['证据包编号']}-{int(time.time()*1000) % 100000}",
            "证据包编号": entry["证据包编号"],
            "报告编号": entry["报告编号"],
            "原核查范围": original,
            "新增扩段": extra_segment,
            "扩大后范围": review["扩大后范围"],
            "确认依据": basis,
        }
        store.rows("gap_evidence_reviews").append(confirmation)
        self._write_jsonl(
            Path(entry["package_dir"]) / "review_confirmations.jsonl", [confirmation])
        # 同步刷新清单中的复查范围。
        manifest_file = Path(entry["package_dir"]) / "manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["复查范围"] = review
        manifest_file.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, **confirmation}

    # ------------------------------------------------------------------
    # 派工导出：导出实际派给现场的范围，排除已处置缺口
    # ------------------------------------------------------------------

    def export_dispatch_scope(self, package_id: int) -> dict[str, Any]:
        entry = store.find(self.MODULE, package_id)
        if entry is None:
            return {"error": f"缺测证据包 {package_id} 不存在"}
        if entry.get("包状态") == "草稿":
            return {"error": f"证据包 {entry['证据包编号']} 尚未固化确认，不能导出派工范围"}
        handled = set(entry.get("handled_gap_ids", []))
        dispatched = [g for g in entry.get("gaps", []) if g["缺口编号"] not in handled]
        entry["dispatched_gap_ids"] = [g["缺口编号"] for g in dispatched]

        dispatch_record = {
            "证据包编号": entry["证据包编号"],
            "报告编号": entry["报告编号"],
            "包版本": entry["包版本"],
            "口径版本": entry["口径版本"],
            "导出时间": _fmt_ts(datetime.now()),
            "已排除已处置缺口": sorted(handled),
            "实际派工范围": dispatched,
        }
        out_file = Path(entry["package_dir"]) / "dispatch_scope.jsonl"
        written = self._write_jsonl(out_file, [dispatch_record])
        return {
            "ok": True,
            "导出文件": str(out_file),
            "派工缺口数": len(dispatched),
            "已排除缺口": sorted(handled),
            "written": written,
            "实际派工范围": dispatched,
        }

    # ------------------------------------------------------------------
    # 内部帮助器
    # ------------------------------------------------------------------

    def _resolve_lineage(self, point_key: str, lineage_id: int | None) -> dict[str, Any] | None:
        rows = store.rows("series_lineage")
        if lineage_id is not None:
            row = store.find("series_lineage", lineage_id)
            if row and row.get("测点编号") == point_key:
                return row
        for row in rows:
            if row.get("测点编号") == point_key and row.get("上送批次"):
                return row
        return None

    def _latest_calibration(self, point_key: str) -> dict[str, Any] | None:
        rows = [r for r in store.rows("unit_calibrate") if r.get("测点编号") == point_key]
        return rows[-1] if rows else None

    def _load_batched_observations(
        self, point_key: str, batch_no: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """读取测点原始观测，并保证包内有效观测全部来自同一上送批次。"""
        edge_file = self._data_dir("edge_samples") / f"{point_key}.jsonl"
        raw = self._read_jsonl(edge_file)
        if not raw:
            return [], None
        observations: list[dict[str, Any]] = []
        batches: set[str] = set()
        for item in raw:
            if item.get("point") not in (None, point_key):
                continue
            obs_batch = str(item.get("batch_no") or item.get("上送批次") or batch_no)
            batches.add(obs_batch)
            if obs_batch != batch_no:
                return [], (
                    f"测点 {point_key} 观测来自多个批次 {sorted(batches)}，"
                    "证据包内有效观测不能来自不同批次"
                )
            observations.append({
                "point": point_key,
                "timestamp": item["timestamp"],
                "raw_value": item.get("raw_value_mV", item.get("raw_value", "")),
                "batch_no": obs_batch,
            })
        return observations, None

    def _infer_interval(self, timestamps: list[datetime]) -> float:
        if len(timestamps) < 2:
            return 1.0
        deltas = [
            (timestamps[i] - timestamps[i - 1]).total_seconds()
            for i in range(1, len(timestamps))
        ]
        positive = [d for d in deltas if d > 0]
        return min(positive) if positive else 1.0

    def _compute_gaps(
        self, timestamps: list[datetime], interval: float
    ) -> list[dict[str, Any]]:
        """原检查口径：间隔 > 2 倍预期间隔判空缺，缺失点数 = round(间隔/间隔)-1。"""
        gaps: list[dict[str, Any]] = []
        seq = 1
        for i in range(1, len(timestamps)):
            delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
            if delta > interval * 2:
                missing = max(int(round(delta / interval)) - 1, 1)
                gaps.append({
                    "缺口编号": f"GAP-{seq:03d}",
                    "缺失起点": _fmt_ts(timestamps[i - 1]),
                    "缺失终点": _fmt_ts(timestamps[i]),
                    "实际间隔秒": round(delta, 3),
                    "预期间隔秒": interval,
                    "缺失点数": missing,
                })
                seq += 1
        return gaps

    def _build_review_scope(
        self,
        timestamps: list[datetime],
        extra_review_range: dict[str, str] | None,
    ) -> dict[str, Any]:
        original = {
            "start": _fmt_ts(timestamps[0]),
            "end": _fmt_ts(timestamps[-1]),
        }
        scope: dict[str, Any] = {"原核查范围": original, "扩段": []}
        if extra_review_range:
            start = _parse_ts(extra_review_range["start"])
            end = _parse_ts(extra_review_range["end"])
            scope["扩段"].append({
                "start": _fmt_ts(start),
                "end": _fmt_ts(end),
                "确认依据": extra_review_range.get("basis", "异常复查确认"),
            })
            scope["扩大后范围"] = {"start": original["start"], "end": _fmt_ts(end)}
        else:
            scope["扩大后范围"] = dict(original)
        return scope

    def _next_version(self, report_no: str) -> str:
        existing = [
            r for r in store.rows(self.MODULE) if r.get("报告编号") == report_no
        ]
        return f"v{len(existing) + 1}"

    def _inherited_handled_gaps(
        self, report_no: str, current_gaps: list[dict[str, Any]]
    ) -> list[str]:
        """同份报告包已处置缺点按缺口坐标（起/止时刻）继承到新版本。"""
        handled_coords: set[tuple[str, str]] = set()
        for row in store.rows(self.MODULE):
            if row.get("报告编号") != report_no:
                continue
            handled = set(row.get("handled_gap_ids", []))
            for gap in row.get("gaps", []):
                if gap["缺口编号"] in handled:
                    handled_coords.add((gap["缺失起点"], gap["缺失终点"]))
        return [
            gap["缺口编号"]
            for gap in current_gaps
            if (gap["缺失起点"], gap["缺失终点"]) in handled_coords
        ]

    def ensure_download_bookmark(self, package_id: int) -> str:
        """返回下载书签；重新领取时保留原书签，避免重复取得已核对缺口。"""
        entry = store.find(self.MODULE, package_id)
        if entry is None:
            return ""
        bookmark = entry.get("download_bookmark") or self._download_package(package_id)
        entry["download_bookmark"] = bookmark
        return bookmark

    def _download_package(self, package_id: int) -> str:
        """模拟接收方下载真实验证包文件，返回可复用的下载书签。"""
        entry = store.find(self.MODULE, package_id)
        package_dir = Path(entry["package_dir"])
        bookmark_dir = self._data_dir("gap_evidence_downloads")
        bookmark_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M%S")
        bookmark = str(bookmark_dir / f"{entry['证据包编号']}_{stamp}.marker")
        Path(bookmark).write_text(
            json.dumps({
                "证据包编号": entry["证据包编号"],
                "源目录": str(package_dir),
                "下载时间": _fmt_ts(datetime.now()),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return bookmark

    def _find_claim(self, request_no: str) -> dict[str, Any] | None:
        for row in store.rows("gap_evidence_claims"):
            if row.get("申请编号") == request_no:
                return row
        return None

    def _find_by_package_no(self, package_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("证据包编号") == package_no:
                return row
        return None

    def _checksum_file(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_recompute_script(self, package_dir: Path) -> None:
        """写一份独立离线复算脚本，接收方无需平台即可逐点复算。"""
        script = '''#!/usr/bin/env python3
"""离线逐点复算脚本：读取同目录 observations.jsonl，按 manifest.json 口径重算空缺。"""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def parse_ts(value):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


def main():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    interval = float(manifest["预期间隔秒"])
    timestamps = sorted(
        parse_ts(json.loads(line)["timestamp"])
        for line in (ROOT / "observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    gaps, seq = [], 1
    for i in range(1, len(timestamps)):
        delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
        if delta > interval * 2:
            gaps.append({
                "缺口编号": f"GAP-{seq:03d}",
                "缺失起点": timestamps[i - 1].strftime("%Y-%m-%dT%H:%M:%S"),
                "缺失终点": timestamps[i].strftime("%Y-%m-%dT%H:%M:%S"),
                "缺失点数": max(int(round(delta / interval)) - 1, 1),
            })
            seq += 1
    frozen = {(g["缺口编号"], g["缺失起点"], g["缺失终点"], g["缺失点数"])
              for g in manifest["空缺区间"]}
    recomputed = {(g["缺口编号"], g["缺失起点"], g["缺失终点"], g["缺失点数"]) for g in gaps}
    print(json.dumps({
        "证据包编号": manifest["证据包编号"],
        "复算空缺数": len(gaps),
        "固化空缺数": len(manifest["空缺区间"]),
        "逐点一致": frozen == recomputed,
        "空缺区间": gaps,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
'''
        (package_dir / "recompute.py").write_text(script, encoding="utf-8")

    def _build_readme(self, manifest: dict[str, Any]) -> str:
        return f"""# 缺测证据包 {manifest['证据包编号']}

- 关联原告警/质量报告：{manifest['报告编号']}（{manifest['口径版本']}）
- 测点：{manifest['测点编号']}，包版本：{manifest['包版本']}
- 上送批次（采集血缘核对）：{manifest['采样序列']['上送批次']}
- 标定单位：{manifest['标定单位']['原始单位']} -> {manifest['标定单位']['目标单位']}
- 预期间隔：{manifest['预期间隔秒']} 秒
- 观测点数：{manifest['采样序列']['观测点数']}
- 空缺区间数：{len(manifest['空缺区间'])}

## 离线复算

```bash
python3 recompute.py
```

脚本仅依赖标准库，读取 `observations.jsonl` 与 `manifest.json`，
按原检查口径逐点重算空缺区间并与固化结果核对。

## 文件说明

- manifest.json：证据清单（含校验和、复查范围）
- observations.jsonl：测点原始时刻与观测值（同一批次）
- gaps.jsonl：空缺区间
- recompute.py：离线复算脚本
- receipts.jsonl / maintenance_receipts.jsonl：签收与检修回执
- dispatch_scope.jsonl：实际派给现场的范围
"""
