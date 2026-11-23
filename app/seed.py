"""演示数据：覆盖最少退单、同值去重、无解回退、版本递增等场景。"""
from __future__ import annotations

from . import db
from .engine import solve

DEMO = [
    {
        "title": "深夜电台（可核定）",
        "declarations": [
            # D001/D004 区间重叠且同值 -> 同日同值只计一次
            ("D001", "2026-01-01", "2026-04-01", {"甲": 6000, "乙": 4000}),
            ("D002", "2026-04-01", "2026-07-01", {"甲": 7000, "乙": 3000}),
            # D003 与 D001 在重叠期对甲给出异值，必须整单退回
            ("D003", "2026-03-01", "2026-04-01", {"甲": 5000, "乙": 5000}),
            ("D004", "2026-02-01", "2026-03-01", {"甲": 6000, "乙": 4000}),
        ],
        "period": ("2026-01-01", "2026-07-01"),
        "publish_seed_version": False,
    },
    {
        "title": "清晨钟声（无解·回退上一版）",
        "declarations": [
            # 两单之间存在空档，整段结算期无法全覆盖 -> 无解
            ("D101", "2026-01-01", "2026-04-01", {"甲": 8000, "乙": 2000}),
            ("D102", "2026-05-01", "2026-07-01", {"甲": 5500, "乙": 4500}),
        ],
        "period": ("2026-01-01", "2026-07-01"),
        # 预置一份历史核定版，无解或写入失败时仍可取数
        "publish_seed_version": True,
    },
    {
        "title": "午后回廊（版本递增演示）",
        "declarations": [
            ("D201", "2026-01-01", "2026-07-01", {"丙": 10000}),
            ("D202", "2026-01-01", "2026-07-01", {"丙": 10000}),
        ],
        "period": ("2026-01-01", "2026-07-01"),
        "publish_seed_version": True,
    },
    {
        "title": "星光渡口（供数换版演示）",
        "declarations": [
            ("D301", "2026-01-01", "2026-07-01", {"丁": 7000, "戊": 3000}),
        ],
        "period": ("2026-01-01", "2026-07-01"),
        "publish_seed_version": False,
    },
]


def _seg(start: str, end: str, shares: dict) -> dict:
    return {"start_date": start, "end_date": end, "ok": True,
            "reason": None, "shares": shares}


def seed_supply_demo(recording_id: int) -> None:
    """供数换版演示：半年核定版 + 三月单月核定版，并预置首个供数批号。"""
    signature = "seed-supply-demo"
    # v1：覆盖整个上半年的季度核定版（两段份额）
    v1_payload = {
        "feasible": True,
        "retain": ["D301"],
        "returned": [],
        "segments": [
            _seg("2026-01-01", "2026-04-01", {"丁": 7000, "戊": 3000}),
            _seg("2026-04-01", "2026-07-01", {"丁": 7500, "戊": 2500}),
        ],
        "checked_order_count": 1,
        "reason": None,
    }
    v1_id = db.publish_version(
        recording_id, "2026-01-01", "2026-07-01", v1_payload, signature
    )
    # v2：只覆盖三月的单月核定版（入仓时只换一个月份额的场景）
    v2_payload = {
        "feasible": True,
        "retain": ["D301"],
        "returned": [],
        "segments": [_seg("2026-03-01", "2026-04-01", {"丁": 6000, "戊": 4000})],
        "checked_order_count": 1,
        "reason": None,
    }
    db.publish_version(
        recording_id, "2026-03-01", "2026-04-01", v2_payload, signature
    )

    # 预置批号 B0001：上半年整段先取 v1，随后入仓员可把三月切到 v2
    from .web import _current_snapshot_builder

    v1 = db.get_version(v1_id)
    db.perform_switch(
        recording_id, v1, "2026-01-01", "2026-07-01",
        _current_snapshot_builder(recording_id),
    )


def seed_demo_data() -> None:
    if db.list_recordings():
        return  # 已有数据，保持幂等

    for item in DEMO:
        recording_id = db.get_or_create_recording(item["title"])
        for order_no, start, end, shares in item["declarations"]:
            db.upsert_declaration(recording_id, order_no, start, end, shares)

        start, end = item["period"]
        declarations = db.list_declarations(recording_id)
        signature = db.input_signature(declarations, start, end)
        result = solve(declarations, start, end)
        if item["publish_seed_version"]:
            payload = {
                "feasible": result.feasible,
                "retain": result.retain,
                "returned": result.returned,
                "segments": result.segments,
                "checked_order_count": result.checked_order_count,
                "reason": result.reason,
            }
            # 即便当前资料无解，也保留一份历史版本用于回退取数演示
            if not result.feasible:
                payload = {
                    "feasible": True,
                    "retain": [d["order_no"] for d in declarations],
                    "returned": [],
                    "segments": [
                        {
                            "start_date": start,
                            "end_date": end,
                            "ok": True,
                            "reason": None,
                            "shares": {"甲": 8000, "乙": 2000},
                        }
                    ],
                    "checked_order_count": len(declarations),
                    "reason": None,
                }
            db.publish_version(recording_id, start, end, payload, signature)

        if item["title"].startswith("星光渡口"):
            seed_supply_demo(recording_id)
