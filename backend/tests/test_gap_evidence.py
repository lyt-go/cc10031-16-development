"""缺测证据包集成测试：覆盖离线复算、续领签收、检修回执、复查扩段与派工全链路。

叙事对应业务口径：
- 证据包关联原告警（质量报告）、采样序列、采集血缘与签收结果；
- 接收方下载真实验证包文件，重领保留下载书签；
- 检修人员处理第一个缺口后，新上送把第二个原始缺口缩短一半；
- 上一版仍能按原检查口径复算，续领/新核对必须显式声明；
- 已处理首个缺口不重派、不重复登记；异常复查在原范围外扩段。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.base import DATA_ROOT


@pytest.fixture(autouse=True)
def setup_data_dir():
    from app.seed import SEED_ROWS
    from app.store import store
    with tempfile.TemporaryDirectory() as tmp:
        # 每个用例从干净种子仓库开始，保证证据包版本号断言稳定。
        store._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        with patch("app.services.base.DATA_ROOT", Path(tmp)):
            import app.services.gap_evidence as ge
            ge.DATA_ROOT = Path(tmp)
            yield Path(tmp)


POINT = "SENS-EVID"
BATCH = "UPLO-EVID-1"
INTERVAL = 60.0


def _write_samples(data_root: Path, point: str, records: list[dict]) -> None:
    edge_dir = data_root / "edge_samples"
    edge_dir.mkdir(parents=True, exist_ok=True)
    with open(edge_dir / f"{point}.jsonl", "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _seed_report_and_lineage(point: str, batch_no: str) -> int:
    from app.store import store
    report_rows = store.rows("series_quality")
    report_id = max((int(r.get("id", 0)) for r in report_rows), default=0) + 1
    report_rows.append({
        "id": report_id, "报告编号": f"QUAL-{point}", "测点编号": point,
        "统计窗口": "2026-11-24", "完整率": "0", "重复点数": "0",
        "越界点数": "0", "校验规则": "完整性+空缺", "质检结论": "待质检",
        "status": "待质检", "pending": True, "abnormal": True,
    })
    store.rows("series_lineage").append({
        "id": max((int(r.get("id", 0)) for r in store.rows("series_lineage")), default=0) + 1,
        "链路编号": f"LINEAGE-{point}", "测点编号": point, "上送批次": batch_no,
        "标定版本": f"UNIT-{point}", "降采样任务": "N/A", "归并窗口": "N/A",
        "血缘版本": "v1.0", "采集时间": "2026-11-24",
        "status": "已生效", "pending": False, "abnormal": False,
    })
    store.rows("unit_calibrate").append({
        "id": max((int(r.get("id", 0)) for r in store.rows("unit_calibrate")), default=0) + 1,
        "标定编号": f"UNIT-{point}", "测点编号": point,
        "原始单位": "mV", "目标单位": "V", "换算系数": "0.001", "零点偏移": "0",
        "标定方式": "线性换算", "标定时间": "2026-11-24",
        "status": "已标定", "pending": False, "abnormal": False,
    })
    return report_id


def _v1_samples(point: str, batch_no: str) -> list[dict]:
    # 10:00、10:01、10:02、10:05（GAP-001：缺 2 点）、10:15（GAP-002：缺 9 点）、10:16
    minutes = [0, 1, 2, 5, 15, 16]
    return [
        {"point": point, "timestamp": f"2026-11-24T10:{m:02d}:00",
         "raw_value_mV": 20.0 + m, "batch_no": batch_no}
        for m in minutes
    ]


class TestGapEvidencePackage:
    """证据包构建、同源批次校验与离线逐点复算。"""

    def test_build_package_links_report_series_lineage_and_recomputes(self, setup_data_dir):
        from app.services.gap_evidence import GapEvidenceService
        _write_samples(setup_data_dir, POINT, _v1_samples(POINT, BATCH))
        report_id = _seed_report_and_lineage(POINT, BATCH)

        svc = GapEvidenceService()
        result = svc.build_evidence_package(report_id, expected_interval_seconds=INTERVAL)
        assert result["ok"] is True
        assert result["报告编号"] == f"QUAL-{POINT}"
        assert result["上送批次"] == BATCH
        assert result["空缺区间数"] == 2

        package_dir = Path(result["package_dir"])
        # 真实文件产物齐备，接收方可以整包下载离线复算。
        for name in ("manifest.json", "observations.jsonl", "gaps.jsonl",
                     "recompute.py", "README.md"):
            assert (package_dir / name).exists()
        manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["告警编号"] == f"QUAL-{POINT}"
        assert manifest["标定单位"]["原始单位"] == "mV"
        assert manifest["预期间隔秒"] == INTERVAL
        assert manifest["采样序列"]["血缘链路"] == f"LINEAGE-{POINT}"
        assert len(manifest["空缺区间"]) == 2
        assert manifest["空缺区间"][0]["缺失点数"] == 2
        assert manifest["空缺区间"][1]["缺失点数"] == 9
        assert "清单校验" in manifest

        # 服务层离线复算：逐点一致。
        recompute = svc.recompute_package(result["package_id"])
        assert recompute["逐点一致"] is True
        assert (package_dir / "recompute_results.jsonl").exists()

    def test_recompute_script_runs_offline_point_by_point(self, setup_data_dir):
        from app.services.gap_evidence import GapEvidenceService
        _write_samples(setup_data_dir, POINT, _v1_samples(POINT, BATCH))
        report_id = _seed_report_and_lineage(POINT, BATCH)
        svc = GapEvidenceService()
        result = svc.build_evidence_package(report_id, expected_interval_seconds=INTERVAL)

        # 在独立解释器进程里跑包内脚本，证明脱离平台也能逐点复算。
        proc = subprocess.run(
            [sys.executable, "-I", "recompute.py"],
            cwd=result["package_dir"], capture_output=True, text=True, check=True,
        )
        output = json.loads(proc.stdout)
        assert output["逐点一致"] is True
        assert output["复算空缺数"] == 2

    def test_observations_from_multiple_batches_are_rejected(self, setup_data_dir):
        from app.services.gap_evidence import GapEvidenceService
        mixed = _v1_samples(POINT, BATCH)
        mixed.append({"point": POINT, "timestamp": "2026-11-24T10:20:00",
                      "raw_value_mV": 42.0, "batch_no": "UPLO-OTHER-9"})
        _write_samples(setup_data_dir, POINT, mixed)
        report_id = _seed_report_and_lineage(POINT, BATCH)

        result = GapEvidenceService().build_evidence_package(
            report_id, expected_interval_seconds=INTERVAL)
        assert "error" in result
        assert "不同批次" in result["error"]


class TestClaimAndReceiptLifecycle:
    """领取签收：续领/新核对显式声明、下载书签保留、已处理缺口不重派。"""

    def test_full_continuation_lifecycle(self, setup_data_dir):
        from app.services.gap_evidence import GapEvidenceService
        from app.store import store
        _write_samples(setup_data_dir, POINT, _v1_samples(POINT, BATCH))
        report_id = _seed_report_and_lineage(POINT, BATCH)
        svc = GapEvidenceService()

        # ---- 第一版证据包 ----
        v1 = svc.build_evidence_package(report_id, expected_interval_seconds=INTERVAL)
        v1_id = v1["package_id"]

        # 签收：回执同时给出已处理缺口与待派出其余时段。
        receipt = svc.confirm_receipt(v1_id, "检修班-甲", "new_check")
        assert receipt["领取方式"] == "开始新核对"
        assert receipt["已处理缺口"] == []
        assert receipt["待派缺口"] == ["GAP-001", "GAP-002"]
        assert len(receipt["待派时段"]) == 2
        bookmark_v1 = receipt["下载书签"]
        assert Path(bookmark_v1).exists()

        # 检修人员只处理第一个缺口；回执绑定实际核对缺点与报告版本。
        maint = svc.register_maintenance_receipt(
            v1_id, ["GAP-001"], "检修班-甲", "终端重启恢复采接")
        assert maint["包版本"] == "v1"
        assert maint["口径版本"] == f"v{report_id}"
        assert maint["核对缺点"] == ["GAP-001"]

        # 已处置首个缺口不能重复登记。
        again = svc.register_maintenance_receipt(
            v1_id, ["GAP-001"], "检修班-甲", "重复尝试")
        assert "error" in again and "已处置" in again["error"]

        # ---- 新上送把第二个原始缺口缩短一半：10:05 之后补齐到 10:10~10:14 ----
        # 原缺口 10:05->10:15（缺 9 点）缩短为 10:05->10:10（缺 4 点），仍是单段。
        late = _v1_samples(POINT, BATCH) + [
            {"point": POINT, "timestamp": f"2026-11-24T10:{m:02d}:00",
             "raw_value_mV": 30.0 + m, "batch_no": BATCH}
            for m in (10, 11, 12, 13, 14)
        ]
        _write_samples(setup_data_dir, POINT, late)

        # 续领申请在新证据包确认（固化）之前创建。
        prepared = svc.prepare_evidence_package(
            report_id, expected_interval_seconds=INTERVAL)
        v2_id = prepared["package_id"]
        assert prepared["包版本"] == "v2"
        claim = svc.create_claim_request(v2_id, "检修班-甲", "continuation")
        assert claim["领取方式"] == "续领原检查"
        assert claim["状态"] == "待发出"

        # 固化前发出必须被拒绝。
        blocked = svc.issue_claim_request(claim["申请编号"])
        assert "error" in blocked and "固化确认" in blocked["error"]

        # 固化确认之后才能发出。
        frozen = svc.freeze_evidence_package(v2_id)
        assert frozen["包状态"] == "已固化"
        issued = svc.issue_claim_request(claim["申请编号"])
        assert issued["状态"] == "已发出"

        # 上一版仍能按原检查口径复算（原 GAP-002 仍是 9 个缺失点）。
        v1_recompute = svc.recompute_package(v1_id)
        assert v1_recompute["逐点一致"] is True
        assert v1_recompute["固化空缺区间"][1]["缺失点数"] == 9

        # 新版本：第二个缺口被缩短一半（9 -> 4），首个缺口坐标继承已处置、不再待派。
        v2 = store.find("gap_evidence", v2_id)
        v2_gaps = v2["gaps"]
        assert v2_gaps[0]["缺失点数"] == 2
        assert v2_gaps[1]["缺失点数"] == 4
        assert v2["handled_gap_ids"] == ["GAP-001"]
        assert v2["待派缺口"] == "1"

        # 续领签收：保留下载书签语义，待派只剩 GAP-002，首个缺口不重派。
        receipt2 = svc.confirm_receipt(v2_id, "检修班-甲", "continuation")
        assert receipt2["领取方式"] == "续领原检查"
        assert receipt2["已处理缺口"] == ["GAP-001"]
        assert receipt2["待派缺口"] == ["GAP-002"]
        assert len(receipt2["待派时段"]) == 1

        # 第二个缺口仍未接单时，派工导出只含未处置缺口。
        dispatch = svc.export_dispatch_scope(v2_id)
        assert dispatch["派工缺口数"] == 1
        assert dispatch["实际派工范围"][0]["缺口编号"] == "GAP-002"
        assert dispatch["已排除缺口"] == ["GAP-001"]
        dispatch_file = Path(dispatch["导出文件"])
        assert dispatch_file.exists()
        exported = json.loads(dispatch_file.read_text(encoding="utf-8").splitlines()[0])
        assert exported["实际派工范围"][0]["缺口编号"] == "GAP-002"

    def test_draft_package_is_blocked_until_frozen(self, setup_data_dir):
        """草稿（未固化）证据包不能签收、回执、复查或派工。"""
        from app.services.gap_evidence import GapEvidenceService
        _write_samples(setup_data_dir, POINT, _v1_samples(POINT, BATCH))
        report_id = _seed_report_and_lineage(POINT, BATCH)
        svc = GapEvidenceService()
        prepared = svc.prepare_evidence_package(
            report_id, expected_interval_seconds=INTERVAL)
        pkg_id = prepared["package_id"]
        assert prepared["包版本"] == "v1"

        blocked_receipt = svc.confirm_receipt(pkg_id, "检修班-丙", "new_check")
        assert "error" in blocked_receipt and "固化确认" in blocked_receipt["error"]
        blocked_maint = svc.register_maintenance_receipt(
            pkg_id, ["GAP-001"], "检修班-丙", "提前处置")
        assert "error" in blocked_maint and "固化确认" in blocked_maint["error"]
        blocked_review = svc.confirm_abnormal_review(
            pkg_id, {"start": "2026-11-24T10:20:00", "end": "2026-11-24T10:22:00"}, "x")
        assert "error" in blocked_review and "固化确认" in blocked_review["error"]
        blocked_dispatch = svc.export_dispatch_scope(pkg_id)
        assert "error" in blocked_dispatch and "固化确认" in blocked_dispatch["error"]

        # 固化后同样操作恢复正常。
        frozen = svc.freeze_evidence_package(pkg_id)
        assert frozen["ok"] is True
        receipt = svc.confirm_receipt(pkg_id, "检修班-丙", "new_check")
        assert receipt["ok"] is True

    def test_redownload_keeps_bookmark(self, setup_data_dir):
        from app.services.gap_evidence import GapEvidenceService
        _write_samples(setup_data_dir, POINT, _v1_samples(POINT, BATCH))
        report_id = _seed_report_and_lineage(POINT, BATCH)
        svc = GapEvidenceService()
        pkg = svc.build_evidence_package(report_id, expected_interval_seconds=INTERVAL)

        first = svc.confirm_receipt(pkg["package_id"], "检修班-乙", "new_check")
        second = svc.confirm_receipt(pkg["package_id"], "检修班-乙", "new_check")
        # 重新领取保留同一下载书签，避免重复取得已核对缺口。
        assert first["下载书签"] == second["下载书签"]


class TestAbnormalReviewExpansion:
    """异常复查确认：原范围外扩段，原未核查整段保持有效可复查。"""

    def test_review_expands_beyond_original_scope(self, setup_data_dir):
        from app.services.gap_evidence import GapEvidenceService
        _write_samples(setup_data_dir, POINT, _v1_samples(POINT, BATCH))
        report_id = _seed_report_and_lineage(POINT, BATCH)
        svc = GapEvidenceService()
        pkg = svc.build_evidence_package(report_id, expected_interval_seconds=INTERVAL)
        entry = svc.get_entry(pkg["package_id"])

        original_end = entry["review_scope"]["原核查范围"]["end"]
        assert original_end == "2026-11-24T10:16:00"

        # 扩段落在原范围之外（10:20-10:22）。
        review = svc.confirm_abnormal_review(
            pkg["package_id"],
            {"start": "2026-11-24T10:20:00", "end": "2026-11-24T10:22:00"},
            basis="异常复查核准单 REV-APPLY-1",
        )
        assert review["ok"] is True
        assert review["扩大后范围"]["end"] == "2026-11-24T10:22:00"
        # 原未核查整段仍是有效可复查范围。
        assert review["原核查范围"]["end"] == original_end

        # 侵入原范围的扩段必须被拒绝。
        invalid = svc.confirm_abnormal_review(
            pkg["package_id"],
            {"start": "2026-11-24T10:07:00", "end": "2026-11-24T10:09:00"},
            basis="越界申请",
        )
        assert "error" in invalid and "原核查范围之外" in invalid["error"]

        # 确认依据已同步进包内清单与确认文件。
        manifest = json.loads(
            (Path(entry["package_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["复查范围"]["扩段"][0]["确认依据"] == "异常复查核准单 REV-APPLY-1"
        assert (Path(entry["package_dir"]) / "review_confirmations.jsonl").exists()


class TestPackageDownload:
    """接收方下载真实验证包 zip 文件。"""

    def test_download_returns_real_zip_with_all_artifacts(self, setup_data_dir):
        from app.routers.gap_evidence import download_package
        from app.services.gap_evidence import GapEvidenceService
        _write_samples(setup_data_dir, POINT, _v1_samples(POINT, BATCH))
        report_id = _seed_report_and_lineage(POINT, BATCH)
        svc = GapEvidenceService()
        pkg = svc.build_evidence_package(report_id, expected_interval_seconds=INTERVAL)

        response = download_package(pkg["package_id"])
        archive_path = Path(response.path)
        assert archive_path.exists()
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            assert "manifest.json" in names
            assert "observations.jsonl" in names
            assert "gaps.jsonl" in names
            assert "recompute.py" in names
            # zip 内观测与原始采样点数一致。
            obs_lines = [ln for ln in zf.read("observations.jsonl").decode().splitlines() if ln]
            assert len(obs_lines) == len(_v1_samples(POINT, BATCH))

        # 下载已写入书签。
        entry = svc.get_entry(pkg["package_id"])
        assert entry["download_bookmark"]
