"""双时态核心逻辑。

两个时间维度：
- 业务时间（valid time）  [valid_from, valid_to)：费率在现实中生效的区间，左闭右开；
- 系统时间（system time） [recorded_at, superseded_at)：该记录被系统认知为真的区间，左闭右开。

修订 = 追加新版本并把被覆盖旧版本的交集标记 superseded_at，交集之外的左、右
部分按原费率与 reason 追加补偿版本；历史行永不修改、永不删除。
"""

import math
import sqlite3
from datetime import datetime
from typing import Optional

from .db import connect
from .timeutil import iso, now_iso, parse

# 无限期区间的上界占位（仅用于 SQL 比较）
INF = "9999-12-31T23:59:59.999999+00:00"


class InvalidIntervalError(ValueError):
    """区间终点不晚于起点（valid_to <= valid_from，或查询窗口 from >= to）。"""


class NonMonotonicRecordTimeError(ValueError):
    """recorded_at 早于该合同已有记录，破坏系统时间单调性。"""


class InvalidAmountError(ValueError):
    """账单行基准金额为负数或非有限数值。"""


class FutureKnownAtError(ValueError):
    """known_at 晚于当前时间，认知时点取自未来。"""


class InvalidKnownAtOrderError(ValueError):
    """before_known_at 不早于 after_known_at，认知时点顺序非法。"""


def record_rate(
    contract_no: str,
    rate: float,
    valid_from: datetime,
    valid_to: Optional[datetime],
    recorded_at: Optional[datetime],
    reason: Optional[str],
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[sqlite3.Row, list[int], list[sqlite3.Row]]:
    """录入费率（追加式）。

    若新区间 [vf, vt) 与当前认知中（superseded_at IS NULL）的既有版本重叠，
    仅用新版本替换交集部分：每条被覆盖的旧版本在 recorded_at 时刻标记被取代，
    交集之外未被覆盖的左段 [old.vf, vf)、右段 [vt, old.vt)（含开放上界情形）
    按旧版本原费率与 reason 追加为补偿版本。因此修订窗口之外的报价保持不变，
    不会产生 gap。

    取代标记、新版本、补偿版本在同一事务内完成；版本号严格递增，新版本在前，
    补偿片段按 valid_from 排序（同点按被覆盖版本的原始顺序）在后。

    返回 (新版本行, 被取代的版本 id 列表, 补偿版本行列表)。
    """
    vf = iso(valid_from)
    vt = iso(valid_to) if valid_to is not None else None
    ra = iso(recorded_at) if recorded_at is not None else now_iso()

    if vt is not None and vt <= vf:
        raise InvalidIntervalError("valid_to 必须晚于 valid_from")

    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        state = conn.execute(
            "SELECT MAX(recorded_at) AS max_ra, COALESCE(MAX(version), 0) AS max_v "
            "FROM rate_versions WHERE contract_no = ?",
            (contract_no,),
        ).fetchone()
        if state["max_ra"] is not None and ra < state["max_ra"]:
            raise NonMonotonicRecordTimeError(
                f"recorded_at={ra} 早于合同 {contract_no} 已有记录时间 {state['max_ra']}，"
                "系统时间必须单调递增"
            )

        overlaps = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? AND superseded_at IS NULL "
            "  AND valid_from < COALESCE(?, ?) "
            "  AND COALESCE(valid_to, ?) > ? "
            "ORDER BY valid_from ASC, id ASC",
            (contract_no, vt, INF, INF, vf),
        ).fetchall()

        # 逐条计算交集之外的补偿片段：(valid_from, valid_to, 源顺序, 旧版本行)。
        # 左段严格以 vf 为终点、右段严格以 vt 为起点，故片段必非空；
        # 新区间开放上界（vt IS NULL）时不存在右段。
        pieces: list[tuple[str, Optional[str], int, sqlite3.Row]] = []
        for order, old in enumerate(overlaps):
            if old["valid_from"] < vf:
                pieces.append((old["valid_from"], vf, order, old))
            if vt is not None and (old["valid_to"] is None or vt < old["valid_to"]):
                pieces.append((vt, old["valid_to"], order, old))
        pieces.sort(key=lambda p: (p[0], p[2]))

        # 先创建新费率，version 严格递增；补偿片段按 valid_from 顺序紧随其后。
        next_version = state["max_v"] + 1
        cursor = conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contract_no, rate, vf, vt, ra, next_version, reason),
        )
        new_row = conn.execute(
            "SELECT * FROM rate_versions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()

        retained_rows: list[sqlite3.Row] = []
        for piece_vf, piece_vt, _, old in pieces:
            piece_cursor = conn.execute(
                "INSERT INTO rate_versions "
                "(contract_no, rate, valid_from, valid_to, recorded_at, version, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    contract_no,
                    old["rate"],
                    piece_vf,
                    piece_vt,
                    ra,
                    next_version + 1 + len(retained_rows),
                    old["reason"],
                ),
            )
            retained_rows.append(
                conn.execute(
                    "SELECT * FROM rate_versions WHERE id = ?",
                    (piece_cursor.lastrowid,),
                ).fetchone()
            )

        # 取代标记与新增行在同一事务提交，任一失败整体回滚。
        superseded_ids = [row["id"] for row in overlaps]
        for version_id in superseded_ids:
            conn.execute(
                "UPDATE rate_versions SET superseded_at = ? WHERE id = ?",
                (ra, version_id),
            )

        if own_conn:
            conn.commit()
        return new_row, superseded_ids, retained_rows
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def find_visible_rates(
    contract_no: str, valid_at: datetime, known_at: Optional[datetime]
) -> tuple[list[sqlite3.Row], str]:
    """查询在 (valid_at, known_at) 双时态坐标下可见且有效的所有版本。

    正常情况下结果应为 0 或 1 条；多于 1 条说明数据存在重叠冲突。
    返回 (命中行列表, 实际使用的 known_at)。
    """
    va = iso(valid_at)
    ka = iso(known_at) if known_at is not None else now_iso()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? "
            "  AND valid_from <= ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "  AND recorded_at <= ? "
            "  AND (superseded_at IS NULL OR superseded_at > ?)",
            (contract_no, va, va, ka, ka),
        ).fetchall()
    return list(rows), ka


def get_timeline(contract_no: str) -> list[sqlite3.Row]:
    """合同的全部版本，按版本号升序（即录入顺序）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions WHERE contract_no = ? ORDER BY version ASC, id ASC",
            (contract_no,),
        ).fetchall()
    return list(rows)


def _visible_clipped(
    contract_no: str, f: str, t: str, ka: str
) -> list[tuple[str, str, sqlite3.Row]]:
    """ka 时点可见版本的业务区间裁剪到窗口 [f, t)，返回 (起点, 终点, 版本行) 列表。

    f / t / ka 均须为已规范化的等宽 ISO 字符串；NULL valid_to 用 INF 占位参与比较。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? "
            "  AND recorded_at <= ? "
            "  AND (superseded_at IS NULL OR superseded_at > ?) "
            "  AND valid_from < ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY version ASC, id ASC",
            (contract_no, ka, ka, t, f),
        ).fetchall()
    return [
        (max(row["valid_from"], f), min(row["valid_to"] or INF, t), row)
        for row in rows
    ]


def _candidates_at(
    clipped: list[tuple[str, str, sqlite3.Row]], seg_from: str, seg_to: str
) -> list[sqlite3.Row]:
    """完整覆盖片段 [seg_from, seg_to) 的裁剪区间对应版本（保持查询顺序）。"""
    return [row for s, e, row in clipped if s <= seg_from and e >= seg_to]


def _coverage_status(candidates: list[sqlite3.Row]) -> str:
    """按候选版本数分类：0 → gap，1 → covered，>1 → conflict。"""
    if not candidates:
        return "gap"
    if len(candidates) == 1:
        return "covered"
    return "conflict"


def _visible_segments(contract_no: str, f: str, t: str, ka: str) -> list[dict]:
    """以 ka 时点可见版本的业务区间边界把窗口 [f, t) 切为连续片段。

    f / t / ka 均须为已规范化的等宽 ISO 字符串。先筛出 ka 可见的版本
    （recorded_at <= ka 且未被取代），将其业务区间裁剪到窗口后切分；
    每段按命中的候选版本数分类：0 → gap，1 → covered，>1 → conflict；
    分类与候选完全相同的相邻段合并为一段。

    返回 {"from", "to", "status", "candidates": [Row]} 字典列表，连续铺满整个窗口；
    无可见版本时自然得到覆盖整窗的单个 gap 段。
    """
    clipped = _visible_clipped(contract_no, f, t, ka)

    # 以所有区间边界（含窗口端点）切分 [f, t)
    bounds = sorted({f, t, *(p for s, e, _ in clipped for p in (s, e))})

    segments = []
    last_key = None  # 上一段的 (分类, 候选 id 元组)，用于相邻段合并
    for seg_from, seg_to in zip(bounds, bounds[1:]):
        # 区间边界全部参与切分，故与片段相交的裁剪区间必完整覆盖该片段
        candidates = _candidates_at(clipped, seg_from, seg_to)
        seg_status = _coverage_status(candidates)
        key = (seg_status, tuple(row["id"] for row in candidates))
        if key == last_key:
            segments[-1]["to"] = seg_to
        else:
            segments.append(
                {"from": seg_from, "to": seg_to, "status": seg_status, "candidates": candidates}
            )
            last_key = key
    return segments


def compute_coverage(
    contract_no: str,
    from_dt: datetime,
    to_dt: datetime,
    known_at: Optional[datetime],
) -> tuple[list[dict], str]:
    """计算查询窗口 [from, to) 在 known_at 认知时点下的覆盖率分段。

    分段规则见 _visible_segments。返回 (segments, 实际使用的 known_at)。
    """
    f = iso(from_dt)
    t = iso(to_dt)
    if f >= t:
        raise InvalidIntervalError("from 必须早于 to")
    ka = iso(known_at) if known_at is not None else now_iso()
    return _visible_segments(contract_no, f, t, ka), ka


def _duration_us(f: str, t: str) -> int:
    """两个等宽 ISO 字符串之间的时长（微秒）。"""
    delta = parse(t) - parse(f)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _classify_change(
    before_status: str,
    before_candidates: list[sqlite3.Row],
    after_status: str,
    after_candidates: list[sqlite3.Row],
) -> str:
    """双侧覆盖状态 → 变更分类（规则见 compute_changes）。"""
    if "conflict" in (before_status, after_status):
        return "conflict"
    if before_status == "gap":
        return "added" if after_status == "covered" else "unchanged"
    if after_status == "gap":
        return "removed"
    # 两侧均唯一覆盖：费率相同为 unchanged，不同为 rate_changed
    if before_candidates[0]["rate"] == after_candidates[0]["rate"]:
        return "unchanged"
    return "rate_changed"


def compute_changes(
    contract_no: str,
    from_dt: datetime,
    to_dt: datetime,
    before_known_at: datetime,
    after_known_at: datetime,
) -> dict:
    """对比两个认知时点下窗口 [from, to) 的费率覆盖差异。

    分别按 before_known_at / after_known_at 筛出可见版本，以两侧业务区间边界的
    并集把窗口切为连续片段，逐段给出 before/after 两侧的覆盖状态（gap / covered /
    conflict 及候选版本），并分类：

    - 任一侧 conflict                   → conflict；
    - gap → covered                     → added；
    - covered → gap                     → removed；
    - 两侧均 gap，或均唯一覆盖且费率相同 → unchanged；
    - 其余两侧唯一覆盖（费率不同）       → rate_changed。

    分类与两侧候选完全一致的相邻片段合并为一段；summary 汇总各分类的持续微秒数。
    返回 {"before_known_at", "after_known_at", "segments", "summary"}。
    """
    f = iso(from_dt)
    t = iso(to_dt)
    if f >= t:
        raise InvalidIntervalError("from 必须早于 to")
    bka = iso(before_known_at)
    aka = iso(after_known_at)
    if bka >= aka:
        raise InvalidKnownAtOrderError(
            f"before_known_at={bka} 必须早于 after_known_at={aka}"
        )

    before_clipped = _visible_clipped(contract_no, f, t, bka)
    after_clipped = _visible_clipped(contract_no, f, t, aka)

    # 两侧可见版本业务区间边界的并集（含窗口端点）切分 [f, t)
    bounds = sorted({
        f,
        t,
        *(p for s, e, _ in before_clipped for p in (s, e)),
        *(p for s, e, _ in after_clipped for p in (s, e)),
    })

    segments = []
    summary = {"added_us": 0, "removed_us": 0, "unchanged_us": 0,
               "rate_changed_us": 0, "conflict_us": 0}
    last_key = None  # 上一段的 (分类, before 候选 id 元组, after 候选 id 元组)
    for seg_from, seg_to in zip(bounds, bounds[1:]):
        before_candidates = _candidates_at(before_clipped, seg_from, seg_to)
        after_candidates = _candidates_at(after_clipped, seg_from, seg_to)
        change = _classify_change(
            _coverage_status(before_candidates), before_candidates,
            _coverage_status(after_candidates), after_candidates,
        )
        duration = _duration_us(seg_from, seg_to)
        summary[f"{change}_us"] += duration
        key = (
            change,
            tuple(row["id"] for row in before_candidates),
            tuple(row["id"] for row in after_candidates),
        )
        if key == last_key:
            segments[-1]["to"] = seg_to
            segments[-1]["duration_us"] += duration
        else:
            segments.append(
                {
                    "from": seg_from,
                    "to": seg_to,
                    "change": change,
                    "duration_us": duration,
                    "before": {"status": _coverage_status(before_candidates),
                               "candidates": before_candidates},
                    "after": {"status": _coverage_status(after_candidates),
                              "candidates": after_candidates},
                }
            )
            last_key = key
    return {
        "before_known_at": bka,
        "after_known_at": aka,
        "segments": segments,
        "summary": summary,
    }


# 费用与时长占比的小数精度（预览口径）
CHARGE_SCALE = 6


def compute_charge_preview(
    contract_no: str,
    lines: list[tuple[datetime, datetime, float]],
    known_at: Optional[datetime],
) -> dict:
    """计费预览：把每条账单行 [from, to) 按 known_at 可见费率的业务边界拆分，
    每段费用 = base_amount × (段时长 / 行时长) × 命中费率，行内汇总为 line_total，
    全部行再汇总为 total_amount。

    任一行出现 gap（无费率）或 conflict（重叠冲突）片段时，该行 line_total 置 None，
    且整体拒绝汇总（ok=False、total_amount=None）；未受影响的行仍正常计价。

    lines 为 (from, to, base_amount) 三元组。返回
    {"known_at", "ok", "total_amount", "lines": [...]}；每个 line 为
    {"from", "to", "base_amount", "status", "line_total", "segments"}，
    行状态为 ok / gap / conflict / mixed（同时含 gap 与 conflict 段）；
    每个 segment 为 {"from", "to", "status", "portion", "rate_id", "rate",
    "charge", "candidates"}。
    """
    ka = iso(known_at) if known_at is not None else now_iso()
    if ka > now_iso():
        raise FutureKnownAtError(f"known_at={ka} 晚于当前时间，认知时点不能取自未来")

    # 先校验全部账单行，再统一计价
    normalized = []
    for from_dt, to_dt, base_amount in lines:
        if not math.isfinite(base_amount) or base_amount < 0:
            raise InvalidAmountError(f"base_amount 必须是非负有限数值，收到 {base_amount!r}")
        f, t = iso(from_dt), iso(to_dt)
        if f >= t:
            raise InvalidIntervalError(f"账单行区间非法：from={f} 必须早于 to={t}")
        normalized.append((f, t, base_amount))

    out_lines = []
    total = 0.0
    all_ok = True
    for f, t, base_amount in normalized:
        line_us = _duration_us(f, t)
        segments = []
        flags = set()
        line_total = 0.0
        for seg in _visible_segments(contract_no, f, t, ka):
            portion = _duration_us(seg["from"], seg["to"]) / line_us
            view = {
                "from": seg["from"],
                "to": seg["to"],
                "status": seg["status"],
                "portion": round(portion, CHARGE_SCALE),
                "rate_id": None,
                "rate": None,
                "charge": None,
                "candidates": [],
            }
            if seg["status"] == "covered":
                row = seg["candidates"][0]
                charge = round(base_amount * portion * row["rate"], CHARGE_SCALE)
                line_total += charge
                view.update(rate_id=row["id"], rate=row["rate"], charge=charge)
            else:
                flags.add(seg["status"])
                if seg["status"] == "conflict":
                    view["candidates"] = [
                        {"rate_id": r["id"], "rate": r["rate"], "version": r["version"]}
                        for r in seg["candidates"]
                    ]
            segments.append(view)

        if flags:
            all_ok = False
            line_status = "mixed" if len(flags) > 1 else next(iter(flags))
            line_total_view = None
        else:
            line_status = "ok"
            line_total_view = round(line_total, CHARGE_SCALE)
            total += line_total_view
        out_lines.append(
            {
                "from": f,
                "to": t,
                "base_amount": base_amount,
                "status": line_status,
                "line_total": line_total_view,
                "segments": segments,
            }
        )

    return {
        "known_at": ka,
        "ok": all_ok,
        "total_amount": round(total, CHARGE_SCALE) if all_ok else None,
        "lines": out_lines,
    }
