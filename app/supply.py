"""供数换版纯函数模块。

业务规则
--------
* 一首录音可以发布多份核定版（各自覆盖一个含起日、不含止日的结算期）。
* 入仓员可把某个日期段 [start, end) 的供数来源换成一份已发布核定版；
  换版只改变所选日期的来源，落在原供数段中间时两侧沿用原版。
* 每次换版成功生成一个供数批号；批号对应的完整供数结果在换版瞬间固化，
  后续换版不影响旧批复查结果。
* 现行供数按“来源段”物化保存（见 supply_assignment 表），查现行份额直接
  读取并按核定版自身的分段边界细化，无需重放换版历史。
* 没有任何来源覆盖的日期标为“缺档”。

本模块只处理普通 dict / list，不接触数据库与 Flask，便于自测。
区间一律为 ISO 日期字符串、半开区间 [start_date, end_date)。
"""
from __future__ import annotations

from typing import Iterable


def _clamp(value: str, low: str, high: str) -> str:
    return max(low, min(high, value))


def merge_adjacent(segments: list[dict]) -> list[dict]:
    """合并相邻且来源核定版相同的来源段（updated_batch_id 保留较早写入者）。"""
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: (s["start_date"], s["end_date"]))
    merged = [dict(ordered[0])]
    for seg in ordered[1:]:
        last = merged[-1]
        if (
            seg["start_date"] == last["end_date"]
            and seg["source_version_id"] == last["source_version_id"]
        ):
            last["end_date"] = seg["end_date"]
            # 合并段保留最早写入它的批号
            if seg["updated_batch_id"] < last["updated_batch_id"]:
                last["updated_batch_id"] = seg["updated_batch_id"]
        else:
            merged.append(dict(seg))
    return merged


def replace_interval(
    assignments: Iterable[dict],
    switch_start: str,
    switch_end: str,
    source_version_id: int,
    batch_id: int,
) -> list[dict]:
    """把 [switch_start, switch_end) 的来源替换为指定核定版。

    与原来源段重叠时，只在重叠处改写，区间两侧保留原来源；
    原本没有来源的日子直接建立来源段。返回排序、互不相交的新来源段列表。
    """
    result: list[dict] = []
    for seg in assignments:
        start, end = seg["start_date"], seg["end_date"]
        if end <= switch_start or start >= switch_end:
            result.append(dict(seg))  # 完全不相交，原样保留
            continue
        # 左侧沿用原版
        if start < switch_start:
            result.append({**dict(seg), "start_date": start, "end_date": switch_start})
        # 右侧沿用原版
        if end > switch_end:
            result.append({**dict(seg), "start_date": switch_end, "end_date": end})
    result.append({
        "start_date": switch_start,
        "end_date": switch_end,
        "source_version_id": source_version_id,
        "updated_batch_id": batch_id,
    })
    return merge_adjacent(sorted(result, key=lambda s: (s["start_date"], s["end_date"])))


def find_version_segment(version: dict, day: str) -> dict | None:
    """核定版中覆盖某一天的份额段。"""
    if not (version["period_start"] <= day < version["period_end"]):
        return None
    for seg in version["segments"]:
        if seg["start_date"] <= day < seg["end_date"]:
            return seg
    return None


def build_effective_view(
    query_start: str,
    query_end: str,
    assignments: list[dict],
    versions_by_id: dict[int, dict],
) -> list[dict]:
    """按 [query_start, query_end) 计算现行（或某快照下的）供数结果。

    返回逐段结果，每段形如：
      {start_date, end_date, shares: {权利人: 万分比}|{},
       source_version_id: int|None, missing: bool, reason: str|None}
    无来源覆盖的日期以 missing=True 的缺档段返回；份额段按来源核定版
    自身的分段边界细化，保证同段份额恒定。
    """
    if query_start >= query_end:
        return []

    bounds = {query_start, query_end}
    clipped: list[dict] = []
    for seg in assignments:
        start = max(query_start, seg["start_date"])
        end = min(query_end, seg["end_date"])
        if start < end:
            clipped.append({
                "start_date": start,
                "end_date": end,
                "source_version_id": seg["source_version_id"],
                "updated_batch_id": seg.get("updated_batch_id"),
            })
            bounds.add(start)
            bounds.add(end)
            version = versions_by_id.get(seg["source_version_id"])
            if version is not None:
                for vseg in version["segments"]:
                    bound = _clamp(vseg["start_date"], start, end)
                    bounds.add(bound)
                    bound = _clamp(vseg["end_date"], start, end)
                    bounds.add(bound)

    ordered = sorted(bounds)
    rows: list[dict] = []
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        cover = next(
            (a for a in clipped if a["start_date"] <= left and a["end_date"] >= right),
            None,
        )
        if cover is None:
            rows.append({
                "start_date": left,
                "end_date": right,
                "shares": {},
                "source_version_id": None,
                "missing": True,
                "reason": "该日期段未指定供数来源（缺档）",
            })
            continue
        version = versions_by_id.get(cover["source_version_id"])
        vseg = find_version_segment(version, left) if version is not None else None
        if vseg is None:
            rows.append({
                "start_date": left,
                "end_date": right,
                "shares": {},
                "source_version_id": cover["source_version_id"],
                "missing": True,
                "reason": "来源核定版未覆盖该日期段（缺档）",
            })
            continue
        rows.append({
            "start_date": left,
            "end_date": right,
            "shares": dict(vseg["shares"]),
            "source_version_id": cover["source_version_id"],
            "missing": False,
            "reason": None,
        })
    return rows


def assignment_domain(assignments: list[dict]) -> tuple[str, str] | None:
    """全部来源段的最小外包区间。"""
    if not assignments:
        return None
    return (
        min(a["start_date"] for a in assignments),
        max(a["end_date"] for a in assignments),
    )
