"""柜面限量版流转的时点化匹配、指标区间与凭证优先级工具。

只使用 Python 3.11 标准库。所有历史回演都必须把 as_of 传给读取/快照函数，
避免用后来才收到的回执或记录解释更早的班次。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Iterable

UNKNOWN = "?"
NA = "-"

OUTBOUND_KINDS = {"sell", "fulfil", "handed"}
CLOSURE_KINDS = {"void", "return", "cancel"}
HOLD_KINDS = {"reserve", "held"}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _cast(key: str, value: str) -> Any:
    if value == NA:
        return None
    if value == UNKNOWN:
        return UNKNOWN
    if key in {"day", "received", "issued", "deadline", "created", "minutes", "capacity", "delay"}:
        try:
            return int(value)
        except ValueError:
            return value
    if key in {"max_false_hold", "inclusion"}:
        if value == UNKNOWN:
            return None
        try:
            return float(value)
        except ValueError:
            return value
    if key in {"capacity", "delay", "turnaround"}:
        # turnaround 形如 "1,2"，在专门函数中解析；这里保留字符串。
        return value
    return value


def load_packet(root: str | Path) -> dict[str, list[dict[str, Any]]]:
    root = Path(root)
    layout = json.loads((root / "layout.json").read_text(encoding="utf-8"))
    tables: dict[str, list[dict[str, Any]]] = {}
    for filename, fields in layout.items():
        rows: list[dict[str, Any]] = []
        with (root / filename).open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle, delimiter="\t", strict=True)
            header = next(reader, None)
            if header != fields:
                raise ValueError(f"{filename}: 表头不符: {header!r} != {fields!r}")
            for lineno, values in enumerate(reader, start=2):
                if len(values) != len(fields) or any(v.strip() == "" for v in values):
                    raise ValueError(f"{filename}:{lineno}: 列数不符或有空值")
                row = {field: _cast(field, value.strip()) for field, value in zip(fields, values)}
                rows.append(row)
        tables[filename.removesuffix(".tsv")] = rows
    return tables


def as_of_day(packet: dict[str, list[dict[str, Any]]]) -> int:
    candidates: list[int] = []
    for row in packet["transactions"]:
        candidates.append(int(row["received"]))
    for row in packet["actual_replies"]:
        candidates.append(int(row["received"]))
    for row in packet["reviews"]:
        candidates.append(int(row["day"]))
    for row in packet["shift_capacity"]:
        candidates.append(int(row["day"]))
    return max(candidates)


def visible(rows: Iterable[dict[str, Any]], at: int, field: str = "received") -> list[dict[str, Any]]:
    result = []
    for row in rows:
        value = row.get(field)
        if isinstance(value, int) and value <= at:
            result.append(row)
    return result


def turnaround_window(value: str) -> tuple[int, int]:
    lo, hi = value.split(",", 1)
    return int(lo), int(hi)


def split_set(value: Any) -> set[str]:
    if not value or value == UNKNOWN:
        return set()
    return {part.strip() for part in str(value).split(",") if part.strip()}


# ---------------------------------------------------------------------------
# 维度与时间线
# ---------------------------------------------------------------------------


def serial_dimensions(packet: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, str]]:
    """返回序列的稳定维度。

    grade 只在 register.tsv 中直接存在。资料中所有已登记样本都满足
    etched=standard、stone=premium；inferred_grade 使用这一观察到的对应，
    但 direct_grade 仍保留“未登记”，两个口径不混为一谈。
    """
    dims: dict[str, dict[str, str]] = {}
    editions: dict[str, Counter] = defaultdict(Counter)
    sites: dict[str, Counter] = defaultdict(Counter)
    first_row: dict[str, dict[str, Any]] = {}

    for row in packet["transactions"]:
        serial = row["serial"]
        editions[serial][row["edition"]] += 1
        sites[serial][row["site"]] += 1
        key = (int(row["day"]), int(row["received"]), row["event"])
        if serial not in first_row or key < (
            int(first_row[serial]["day"]),
            int(first_row[serial]["received"]),
            first_row[serial]["event"],
        ):
            first_row[serial] = row
        dims.setdefault(serial, {})

    register = {row["serial"]: row for row in packet["register"]}
    edition_grade: dict[str, Counter] = defaultdict(Counter)
    for serial, row in register.items():
        edition_grade[strict_most_common(editions.get(serial, Counter({row["edition"]: 1})))][row["grade"]] += 1

    inferred_map: dict[str, str] = {}
    for edition, grades in edition_grade.items():
        if len(grades) == 1:
            inferred_map[edition] = next(iter(grades))

    for serial in sorted(set(editions) | set(register)):
        edition = strict_most_common(editions[serial]) if editions[serial] else register[serial]["edition"]
        if serial in first_row:
            first_listing_site = first_row[serial]["site"]
            channel_basis = "first_transaction_day_site"
        else:
            first_listing_site = register[serial]["custody"]
            channel_basis = "register_custody"
        reg = register.get(serial)
        direct_grade = reg["grade"] if reg else "未登记"
        dims[serial] = {
            "edition": edition,
            # 稳定的一维汇总口径：首次可见业务日所在渠道。跨柜件另用
            # channel_count/custody/订单渠道展示，不能按事件站点重复加总。
            "origin_channel": first_listing_site,
            "channel_basis": channel_basis,
            "channel_count": len(sites[serial]),
            "direct_grade": direct_grade,
            "price_band": reg["grade"] if reg else inferred_map.get(edition, "未登记"),
            "grade_basis": "register" if reg else ("edition_inferred" if edition in inferred_map else "unknown"),
            "custody_channel": reg["custody"] if reg else first_listing_site,
            "custody_matches_first_listing": (reg is None or reg["custody"] == first_listing_site),
            "mark": reg["mark"] if reg else None,
        }
    return dims


def strict_most_common(counter: Counter) -> str:
    if not counter:
        raise ValueError("空 Counter")
    top = counter.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        # 业务资料中同一 serial 的 edition 始终一致；site 可能跨柜，不能在这里
        # 静默选边。跨柜 site 由事件/订单渠道单独处理。
        return sorted(counter)[0]
    return top[0][0]


def order_channel(events: list[dict[str, Any]], order: str | None, fallback: str | None = None) -> str | None:
    if order:
        real_sites = [
            e["site"] for e in events
            if e.get("order") == order and e.get("site") and not e.get("candidate")
        ]
        sites = real_sites or [e["site"] for e in events if e.get("order") == order and e.get("site")]
        if sites:
            return strict_most_common(Counter(sites))
    return fallback


def _event_sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
    return int(row["day"]), str(row.get("received", 0)), str(row.get("event", row.get("event_key", "")))


def serial_timeline(
    packet: dict[str, list[dict[str, Any]]],
    at: int | None = None,
    assignment: dict[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    assignment = assignment or {}
    by_serial: dict[str, list[dict[str, Any]]] = defaultdict(list)

    tx_rows = packet["transactions"]
    if at is not None:
        tx_rows = visible(tx_rows, at)
    for row in tx_rows:
        by_serial[row["serial"]].append({**row, "source": "transaction", "candidate": False})

    edge_rows = packet["unresolved_edges"]
    if at is not None:
        edge_rows = visible(edge_rows, at)
    edge_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in edge_rows:
        edge_by_key[row["event_key"]].append(row)
    for key, serial in assignment.items():
        rows = edge_by_key.get(key, [])
        if not rows:
            continue
        base = {k: rows[0][k] for k in ("event_key", "site", "day", "received", "kind", "order")}
        by_serial[serial].append(
            {
                "event": key,
                "serial": serial,
                "edition": None,
                **base,
                "source": "unresolved",
                "candidate": True,
                "evidence_ids": sorted({r["evidence"] for r in rows if r["candidate"] == serial}),
            }
        )

    for serial in by_serial:
        by_serial[serial].sort(key=_event_sort_key)
    return dict(by_serial)


# ---------------------------------------------------------------------------
# 可行匹配与互斥出货
# ---------------------------------------------------------------------------


def confirmed_replies(packet: dict[str, list[dict[str, Any]]], at: int | None = None) -> dict[str, dict[str, Any]]:
    rows = packet["actual_replies"]
    if at is not None:
        rows = visible(rows, at)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["outcome"] == "confirmed" and row["serial"] != UNKNOWN:
            result[row["event_key"]] = row
    return result


def pending_replies(packet, at: int, *, predecision: bool = False):
    """区分窗口内待回执、窗口届满无确认、已确认。

    actual_replies 的一行同时含 issued 和 received。时点 at 位于两者之间时，
    “已发出但未收到”本身是历史事实，因此按 issued<=at 纳入；只有回执内容
    才要求 received<=at。
    """
    requests = {r["request"]: r for r in packet["requests"]}
    issued_cut = at - 1 if predecision else at
    actual = [r for r in packet["actual_replies"] if int(r["issued"]) <= issued_cut]
    issued: dict[str, dict[str, Any]] = {}
    for row in actual:
        req = row["request"]
        # 同一调取可能有多行；保留最早 issued 和最新 received 状态。
        if req not in issued:
            issued[req] = {"request": req, "issued": int(row["issued"]), "rows": []}
        issued[req]["rows"].append(row)

    items = []
    for req_id, info in issued.items():
        req = requests[req_id]
        lo, hi = turnaround_window(req["turnaround"])
        due = info["issued"] + hi
        visible_rows = [
            r for r in info["rows"]
            if isinstance(r["received"], int) and r["received"] <= at
        ]
        confirmed = any(r["outcome"] == "confirmed" and r["serial"] != UNKNOWN for r in visible_rows)
        waiting = any(r["outcome"] == "waiting" for r in visible_rows)
        latest = max((int(r["received"]) for r in visible_rows), default=None)
        if confirmed:
            status = "confirmed"
        elif at < due:
            status = "receipt_window_open"
        elif at == due:
            status = "receipt_due_today"
        else:
            status = "expired_without_confirmation"
        items.append(
            {
                "request": req_id,
                "issued": info["issued"],
                "window_earliest": info["issued"] + lo,
                "window_latest": due,
                "latest_reply_day": latest,
                "waiting_notice": waiting,
                "status": status,
                "targets": sorted(split_set(req["targets"])),
            }
        )
    return sorted(items, key=lambda x: x["request"])


def candidate_groups(packet, at: int | None = None) -> dict[str, list[str]]:
    rows = packet["unresolved_edges"]
    if at is not None:
        rows = visible(rows, at)
    groups: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        groups[row["event_key"]].add(row["candidate"])
    return {key: sorted(values) for key, values in sorted(groups.items())}


def enumerate_worlds(
    packet,
    at: int | None = None,
    *,
    apply_exclusivity: bool = True,
    overrides: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    groups = candidate_groups(packet, at)
    confirmed = confirmed_replies(packet, at)
    if overrides:
        confirmed = {**confirmed, **{key: {"serial": serial} for key, serial in overrides.items()}}
    keys = list(groups)
    choices: list[list[str]] = []
    for key in keys:
        if key in confirmed:
            serial = confirmed[key]["serial"]
            choices.append([serial])
        else:
            choices.append(groups[key])

    worlds: list[dict[str, Any]] = []
    for combo in product(*choices) if choices else [()]:
        assignment = dict(zip(keys, combo))
        conflicts = shipment_conflicts(packet, at, assignment)
        candidate_conflicts = [c for c in conflicts if c["involves_candidate"]]
        feasible = not candidate_conflicts if apply_exclusivity else True
        worlds.append(
            {
                "assignment": assignment,
                "feasible": feasible,
                "conflicts": conflicts,
                "candidate_conflicts": candidate_conflicts,
            }
        )
    return worlds


def shipment_conflicts(packet, at, assignment) -> list[dict[str, Any]]:
    timelines = serial_timeline(packet, at, assignment)
    conflicts: list[dict[str, Any]] = []
    for serial, events in sorted(timelines.items()):
        # 按日聚合。同一天的撤销/退货可治愈同一天的两笔出库，因为资料没有
        # 提供日内先后；跨日仍未关闭的不同订单出库才算互斥冲突。
        by_day: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            by_day[int(event["day"])].append(event)

        active_order: str | None = None
        active_candidate_keys: set[str] = set()

        for day in sorted(by_day):
            day_events = by_day[day]
            closed_today = {
                e["order"]
                for e in day_events
                if e["kind"] in CLOSURE_KINDS and e.get("order") not in (None, NA)
            }
            if active_order in closed_today:
                active_order = None
                active_candidate_keys = set()

            outbound = [e for e in day_events if e["kind"] in OUTBOUND_KINDS and e.get("order")]
            distinct_orders: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for event in outbound:
                distinct_orders[event["order"]].append(event)

            for order, evs in distinct_orders.items():
                candidate_keys = {e["event"] for e in evs if e.get("candidate")}
                if active_order and active_order != order and active_order not in closed_today:
                    conflicts.append(
                        {
                            "serial": serial,
                            "day": day,
                            "active_order": active_order,
                            "conflicting_order": order,
                            "involves_candidate": bool(active_candidate_keys or candidate_keys),
                            "candidate_keys": sorted(active_candidate_keys | candidate_keys),
                            "events": [e["event"] for e in evs],
                        }
                    )
                if order not in closed_today:
                    active_order = order
                    active_candidate_keys = candidate_keys

            for order in closed_today:
                if order == active_order:
                    active_order = None
                    active_candidate_keys = set()
    return conflicts


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def _median_or_none(values: list[float | int]) -> float | None:
    return statistics.median(values) if values else None


def serial_summaries(packet, at=None, assignment=None) -> dict[str, dict[str, Any]]:
    timelines = serial_timeline(packet, at, assignment)
    dims = serial_dimensions(packet)
    summaries: dict[str, dict[str, Any]] = {}

    for serial, events in timelines.items():
        displays = [int(e["day"]) for e in events if e["kind"] == "display"]
        first_display = min(displays) if displays else None

        voided = {e["order"] for e in events if e["kind"] == "void" and e.get("order")}
        returned = {e["order"] for e in events if e["kind"] == "return" and e.get("order")}
        sales = [e for e in events if e["kind"] == "sell" and e.get("order") not in voided]
        active_sales = [e for e in sales if e.get("order") not in returned]
        fulfils = [e for e in events if e["kind"] == "fulfil"]
        holds = [e for e in events if e["kind"] in HOLD_KINDS]
        pauses = [e for e in events if e["kind"] == "pause"]

        first_sell_day = min((int(e["day"]) for e in sales), default=None)
        final_active_sell_day = max((int(e["day"]) for e in active_sales), default=None)
        last_state = events[-1]["kind"] if events else None

        # 唯一发行编号是库存单位。同一 serial 上两笔未撤销/未退货销售是冲突，
        # 不能相加为两件销量。
        duplicate_active_sale = len({e["order"] for e in active_sales}) > 1
        sold_terminal = bool(active_sales) and not duplicate_active_sale
        backlog = not active_sales and bool(pauses or holds) and not any(e["kind"] == "fulfil" for e in events)

        summaries[serial] = {
            "serial": serial,
            **dims.get(serial, {"edition": "?", "origin_channel": "?", "direct_grade": "未登记", "price_band": "未登记", "grade_basis": "unknown"}),
            "first_display_day": first_display,
            "first_sell_day": first_sell_day,
            "first_sale_turnaround": None if first_display is None or first_sell_day is None else first_sell_day - first_display,
            "final_sell_day": final_active_sell_day,
            "final_turnaround": None if first_display is None or final_active_sell_day is None else final_active_sell_day - first_display,
            "active_sale_orders": sorted({e["order"] for e in active_sales}),
            "active_sale_claim_count": len(active_sales),
            "duplicate_active_sale": duplicate_active_sale,
            "hold_orders": sorted({e["order"] for e in holds if e.get("order")}),
            "fulfilled_orders": sorted({e["order"] for e in fulfils if e.get("order")}),
            "last_event_kind": last_state,
            "backlog_candidate": backlog,
            "event_count": len(events),
        }
    return summaries


def _empty_stat() -> dict[str, Any]:
    return {"serials": [], "first_turnaround": [], "final_turnaround": [], "backlog": [], "duplicate_sale": []}


def _stat_block(bucket: dict[str, Any]) -> dict[str, Any]:
    first = bucket["first_turnaround"]
    final = bucket["final_turnaround"]
    return {
        "serial_count": len(bucket["serials"]),
        "serials": sorted(bucket["serials"]),
        "backlog_count": len(bucket["backlog"]),
        "backlog_serials": sorted(bucket["backlog"]),
        "duplicate_sale_count": len(bucket["duplicate_sale"]),
        "first_turnaround_days": {
            "n": len(first),
            "min": min(first) if first else None,
            "median": _median_or_none(first),
            "max": max(first) if first else None,
        },
        "final_turnaround_days": {
            "n": len(final),
            "min": min(final) if final else None,
            "median": _median_or_none(final),
            "max": max(final) if final else None,
        },
    }


def group_turnover(packet, at=None, assignment=None) -> dict[str, dict[str, dict[str, Any]]]:
    summaries = serial_summaries(packet, at, assignment)
    group_defs = {
        "edition": lambda s: s["edition"],
        "price_band": lambda s: s["price_band"],
        "direct_grade": lambda s: s["direct_grade"],
        "origin_channel": lambda s: s["origin_channel"],
        "edition_price_band_channel": lambda s: f'{s["edition"]}|{s["price_band"]}|{s["origin_channel"]}',
    }
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension, key_fn in group_defs.items():
        buckets: dict[str, dict[str, Any]] = defaultdict(_empty_stat)
        for serial, summary in summaries.items():
            key = key_fn(summary)
            bucket = buckets[key]
            bucket["serials"].append(serial)
            if summary["first_sale_turnaround"] is not None:
                bucket["first_turnaround"].append(summary["first_sale_turnaround"])
            if summary["final_turnaround"] is not None:
                bucket["final_turnaround"].append(summary["final_turnaround"])
            if summary["backlog_candidate"]:
                bucket["backlog"].append(serial)
            if summary["duplicate_active_sale"]:
                bucket["duplicate_sale"].append(serial)
        out[dimension] = {key: _stat_block(bucket) for key, bucket in sorted(buckets.items())}
    return out


def commitment_positions(packet, at=None, assignment=None) -> list[dict[str, Any]]:
    assignment = assignment or {}
    timelines = serial_timeline(packet, at, assignment)
    dims = serial_dimensions(packet)
    rows = packet["commitments"]
    if at is not None:
        # commitments 没有 received；作为事前约定始终可见。
        pass

    positions = []
    for commit in rows:
        order = commit["order"]
        serial = commit["serial"]
        events = timelines.get(serial, [])
        visible_fulfil = [e for e in events if e["kind"] == "fulfil" and e.get("order") == order]
        candidate_cancel_here = [
            e for e in events if e.get("candidate") and e["kind"] == "cancel" and e.get("order") == order
        ]
        any_cancel_order = []
        hold_serials: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for other_serial, other_events in timelines.items():
            any_cancel_order += [
                e for e in other_events if e["kind"] in CLOSURE_KINDS and e.get("order") == order
            ]
            for event in other_events:
                if event["kind"] in HOLD_KINDS and event.get("order") == order:
                    hold_serials[other_serial].append(event)

        hold_on_committed = serial in hold_serials
        hold_on_other = sorted(s for s in hold_serials if s != serial)
        if hold_on_committed and hold_on_other:
            hold_identity = "duplicate_hold_conflict"
        elif hold_on_committed:
            hold_identity = "matches_committed_serial"
        elif hold_on_other:
            hold_identity = "on_other_serial"
        else:
            hold_identity = "none_visible"

        channel = order_channel(events, order, None)
        if channel is None:
            channel = "north" if commit["personnel"] == "desk-N" else "south" if commit["personnel"] == "desk-S" else commit["personnel"]

        if visible_fulfil:
            status = "delivered_visible"
            undelivered = 0
            safe_terminal = True
        elif candidate_cancel_here:
            status = "cancelled_on_committed_serial"
            undelivered = 0
            safe_terminal = True
        elif any_cancel_order:
            status = "serial_mismatched_cancellation_open"
            undelivered = 1
            safe_terminal = False
        elif hold_identity == "on_other_serial":
            status = "hold_serial_mismatch_open"
            undelivered = 1
            safe_terminal = False
        else:
            status = "open_no_delivery_evidence"
            undelivered = 1
            safe_terminal = False

        positions.append(
            {
                "order": order,
                "committed_serial": serial,
                "assigned_serial_for_order_voucher": assignment.get(
                    next((e["event"] for e in any_cancel_order if e.get("candidate")), ""), None
                ),
                "deadline": int(commit["deadline"]),
                "personnel": commit["personnel"],
                "channel": channel,
                "edition": dims[serial]["edition"],
                "price_band": dims[serial]["price_band"],
                "status": status,
                "undelivered": undelivered,
                "safe_terminal": safe_terminal,
                "hold_identity": hold_identity,
                "hold_on_other_serials": hold_on_other,
                "fulfilment_known_on": max((int(e["received"]) for e in visible_fulfil), default=None),
                "actual_fulfil_day": min((int(e["day"]) for e in visible_fulfil), default=None),
            }
        )
    return positions


def world_snapshot(packet, at: int) -> dict[str, Any]:
    worlds = enumerate_worlds(packet, at)
    feasible = [w for w in worlds if w["feasible"]]
    if not feasible:
        feasible = worlds  # 保留冲突世界，不能静默吞掉。

    world_metrics = []
    for world in feasible:
        turnover = group_turnover(packet, at, world["assignment"])
        commitments = commitment_positions(packet, at, world["assignment"])
        world_metrics.append(
            {
                "assignment": world["assignment"],
                "turnover": turnover,
                "commitments": commitments,
                "undelivered_total": sum(c["undelivered"] for c in commitments),
            }
        )
        summaries = serial_summaries(packet, at, world["assignment"])
        world_metrics[-1]["backlog_total"] = sum(1 for x in summaries.values() if x["backlog_candidate"])
        world_metrics[-1]["backlog_serials"] = sorted(
            s for s, x in summaries.items() if x["backlog_candidate"]
        )
        world_metrics[-1]["duplicate_sale_serials"] = sorted(
            s for s, x in summaries.items() if x["duplicate_active_sale"]
        )

    metric_ranges = build_metric_ranges(world_metrics)
    hard_conflicts = {}
    for world in worlds:
        for conflict in world["conflicts"]:
            if not conflict["involves_candidate"]:
                key = (
                    conflict["serial"],
                    conflict["day"],
                    conflict["active_order"],
                    conflict["conflicting_order"],
                )
                hard_conflicts[key] = conflict
    return {
        "at": at,
        "world_count": len(worlds),
        "feasible_world_count": len(feasible),
        "worlds": world_metrics,
        "metric_ranges": metric_ranges,
        "all_world_assignments": [w["assignment"] for w in worlds],
        "hard_conflicts": list(hard_conflicts.values()),
    }


def build_metric_ranges(world_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    """从同一批可行世界聚合区间；不把不同世界的最优分量自行拼接。"""
    scalar_ranges = {
        "undelivered_total": sorted({w["undelivered_total"] for w in world_metrics}),
        "backlog_total": sorted({w["backlog_total"] for w in world_metrics}),
    }
    dimensions = ("edition", "price_band", "direct_grade", "origin_channel", "edition_price_band_channel")
    turnover_ranges: dict[str, dict[str, Any]] = {}
    for dimension in dimensions:
        keys = sorted({key for w in world_metrics for key in w["turnover"][dimension]})
        turnover_ranges[dimension] = {}
        for key in keys:
            stats = [w["turnover"][dimension][key] for w in world_metrics if key in w["turnover"][dimension]]
            if not stats:
                continue

            def envelope(field: str) -> list[float | int | None]:
                vals = [s[field] for s in stats if s[field]["n"]]
                if not vals:
                    return [None, None]
                return [min(v["min"] for v in vals), max(v["max"] for v in vals)]

            turnover_ranges[dimension][key] = {
                "serial_count": stats[0]["serial_count"],
                "backlog_count_range": sorted({s["backlog_count"] for s in stats}),
                "duplicate_sale_count_range": sorted({s["duplicate_sale_count"] for s in stats}),
                "first_turnaround_envelope": envelope("first_turnaround_days"),
                "final_turnaround_envelope": envelope("final_turnaround_days"),
                "first_median_range": sorted({s["first_turnaround_days"]["median"] for s in stats if s["first_turnaround_days"]["median"] is not None}),
                "final_median_range": sorted({s["final_turnaround_days"]["median"] for s in stats if s["final_turnaround_days"]["median"] is not None}),
            }
    return {"scalars": scalar_ranges, "turnover": turnover_ranges}


# ---------------------------------------------------------------------------
# 凭证评估
# ---------------------------------------------------------------------------


def case_graph(packet) -> dict[str, str]:
    """把共享候选 serial 的 event_key 连成同一矛盾主案。"""
    groups = candidate_groups(packet)
    parent = {key: key for key in groups}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    serial_to_keys: dict[str, list[str]] = defaultdict(list)
    for key, serials in groups.items():
        for serial in serials:
            serial_to_keys[serial].append(key)
    for keys in serial_to_keys.values():
        for key in keys[1:]:
            union(keys[0], key)
    return {key: find(key) for key in groups}


def request_board(packet, at: int | None = None, shift: dict[str, Any] | None = None, *, predecision: bool = False) -> list[dict[str, Any]]:
    groups = candidate_groups(packet, at)
    confirmed = confirmed_replies(packet, at)
    case_of = case_graph(packet)
    requests = packet["requests"]
    edge_by_key = defaultdict(list)
    visible_edges = packet["unresolved_edges"] if at is None else visible(packet["unresolved_edges"], at)
    for row in visible_edges:
        edge_by_key[row["event_key"]].append(row)

    open_requests = {
        p["request"]: p
        for p in pending_replies(
            packet,
            at if at is not None else as_of_day(packet),
            predecision=predecision,
        )
    }

    board = []
    for req in requests:
        targets = split_set(req["targets"])
        unresolved = sorted(k for k in targets if k not in confirmed)
        kinds = []
        order_ids = set()
        saleable_keys = []
        for key in targets:
            rows = edge_by_key.get(key, [])
            if rows:
                kind = rows[0]["kind"]
                kinds.append(kind)
                if kind == "display":
                    saleable_keys.append(key)
                if rows[0].get("order") not in (None, "-"):
                    order_ids.add(rows[0]["order"])
        lo, hi = turnaround_window(req["turnaround"])
        issue_day = int(req["available"]) if at is None else min(int(req["available"]), at)
        if at is not None and at < int(req["available"]):
            eligible_day = int(req["available"])
        else:
            eligible_day = at if at is not None else int(req["available"])

        access = split_set(req["access"])
        shift_access = split_set(shift["access"]) if shift else set()
        # requests.access 是可发询的权限口集合；当班至少有其中一个口即可。
        fits_access = bool(access & shift_access) if shift else True
        fits_minutes = int(req["minutes"]) <= int(shift["minutes"]) if shift else True

        board.append(
            {
                "request": req["request"],
                "available": int(req["available"]),
                "targets": sorted(targets),
                "unresolved_targets": unresolved,
                "resolved_targets": sorted(k for k in targets if k in confirmed),
                "kinds": sorted(set(kinds)),
                "orders": sorted(order_ids),
                "case_ids": sorted({case_of.get(k, k) for k in targets}),
                "saleable_event_units": len(set(saleable_keys)),
                "candidate_serials": sorted({s for k in targets for s in groups.get(k, [])}),
                "turnaround_low": lo,
                "turnaround_high": hi,
                "minutes": int(req["minutes"]),
                "access": sorted(access),
                "eligible_at": int(req["available"]),
                "earliest_reply_if_issued_now": eligible_day + lo,
                "latest_reply_if_issued_now": eligible_day + hi,
                "fits_selected_shift_access": fits_access,
                "fits_selected_shift_minutes": fits_minutes,
                "already_tracked": req["request"] in open_requests,
                "tracking": open_requests.get(req["request"]),
            }
        )
    return board


def voucher_priority(packet, at: int, shift_id: str | None = None, *, predecision: bool = False) -> dict[str, Any]:
    shifts = {row["shift"]: row for row in packet["shift_capacity"]}
    shift = shifts[shift_id] if shift_id else None
    board = request_board(packet, at, shift, predecision=predecision)

    commitments = packet["commitments"]
    deadline_by_order = {r["order"]: int(r["deadline"]) for r in commitments}
    tracked_requests = pending_replies(packet, at, predecision=predecision)
    for item in board:
        deadlines = [deadline_by_order[o] for o in item["orders"] if o in deadline_by_order]
        item["commitment_deadline"] = min(deadlines) if deadlines else None
        if item["commitment_deadline"] is not None:
            issue_day = at
            item["possible_before_deadline"] = issue_day + item["turnaround_low"] <= item["commitment_deadline"]
            item["guaranteed_before_deadline"] = issue_day + item["turnaround_high"] <= item["commitment_deadline"]
        else:
            item["possible_before_deadline"] = None
            item["guaranteed_before_deadline"] = None

        # 边际目标：其他“窗口仍开放”的在途请求已覆盖的 event_key 不重复计功。
        # 不能把 RQ3 与 RQ5 对 U19 的预计释放相加。
        covered_by_other_open: set[str] = set()
        for tracked in tracked_requests:
            if tracked["request"] == item["request"]:
                continue
            if tracked["status"] in {"receipt_window_open", "receipt_due_today"}:
                covered_by_other_open |= set(tracked["targets"])
        item["covered_by_other_open_requests"] = sorted(
            k for k in item["unresolved_targets"] if k in covered_by_other_open
        )
        item["marginal_unresolved_targets"] = sorted(
            k for k in item["unresolved_targets"] if k not in covered_by_other_open
        )
        item["marginal_saleable_units"] = sum(
            1 for k in item["marginal_unresolved_targets"] if candidate_kind(packet, k) == "display"
        )

        tracking = item["tracking"]
        if not item["unresolved_targets"]:
            item["action"] = "no_incremental_value"
            item["action_reason"] = "目标均已有确认回执"
        elif tracking and tracking["status"] in {"receipt_window_open", "receipt_due_today"}:
            item["action"] = "await_receipt_no_duplicate_request"
            item["action_reason"] = "本请求仍在回执窗/到期日；保留原单，不重派同一查询"
        elif tracking and tracking["status"] == "confirmed":
            item["action"] = "apply_receipt_and_merge_case"
            item["action_reason"] = "已有确认回执，应更新归属并合并核查项"
        elif tracking and tracking["status"] == "expired_without_confirmation":
            item["action"] = "decide_escalate_or_use_backup_voucher"
            item["action_reason"] = "回执窗已届满且无确认；缺回执不是无风险"
        elif item["marginal_unresolved_targets"]:
            item["action"] = "issue_candidate_request"
            item["action_reason"] = "存在未被其他在途请求覆盖的目标"
        else:
            item["action"] = "wait_for_other_open_request"
            item["action_reason"] = "矛盾已由另一在途请求覆盖，重复发询不能增加可加总库存"

    usable = [
        x
        for x in board
        if x["available"] <= at
        and (shift is None or (x["fits_selected_shift_access"] and x["fits_selected_shift_minutes"]))
    ]
    action_order = {
        "issue_candidate_request": 0,
        "decide_escalate_or_use_backup_voucher": 1,
        "await_receipt_no_duplicate_request": 2,
        "apply_receipt_and_merge_case": 3,
        "wait_for_other_open_request": 4,
        "no_incremental_value": 5,
    }
    ranked = sorted(
        usable,
        key=lambda x: (
            action_order[x["action"]],
            # 可保证在承诺截止前完成的优先；仅有机会但不保证次之。
            0 if x["guaranteed_before_deadline"] else 1 if x["possible_before_deadline"] else 2,
            -len(x["marginal_unresolved_targets"]),
            -x["marginal_saleable_units"],
            x["minutes"],
            x["request"],
        ),
    )
    decisions = [
        x for x in board
        if x["action"] in {"decide_escalate_or_use_backup_voucher", "apply_receipt_and_merge_case"}
    ]
    return {"at": at, "shift": shift_id, "ranked": ranked, "decisions": decisions, "all": board}


def request_scenarios(packet, at: int, request_id: str) -> dict[str, Any]:
    """列出一个凭证在不同合法编号回执下的覆盖结果。

    这里不使用概率：候选行不带概率，因此只给条件结果和区间，不算期望收益。
    """
    req = next(r for r in packet["requests"] if r["request"] == request_id)
    targets = sorted(split_set(req["targets"]))
    groups = candidate_groups(packet, at)
    confirmed = confirmed_replies(packet, at)
    unknown_targets = [k for k in targets if k not in confirmed]
    choices = [[s for s in groups.get(k, []) if k not in confirmed or s == confirmed[k]["serial"]] for k in unknown_targets]

    commitments = {r["order"]: r for r in packet["commitments"]}
    order_ids = sorted({
        row["order"]
        for key in targets
        for row in packet["unresolved_edges"]
        if row["event_key"] == key and row.get("order") not in (None, "-")
    })
    lo, hi = turnaround_window(req["turnaround"])
    deadline = min((int(commitments[o]["deadline"]) for o in order_ids if o in commitments), default=None)

    outcomes = []
    for combo in product(*choices) if choices else [()]:
        override = dict(zip(unknown_targets, combo))
        worlds = enumerate_worlds(packet, at, apply_exclusivity=False, overrides=override)
        order_results = {}
        for order in order_ids:
            statuses = Counter()
            hold_ids = Counter()
            undelivered = set()
            for world in worlds:
                for pos in commitment_positions(packet, at, world["assignment"]):
                    if pos["order"] == order:
                        statuses[pos["status"]] += 1
                        hold_ids[pos["hold_identity"]] += 1
                        undelivered.add(pos["undelivered"])
            order_results[order] = {
                "statuses": dict(statuses),
                "hold_identities": dict(hold_ids),
                "undelivered_range": sorted(undelivered),
                "unique_status": next(iter(statuses)) if len(statuses) == 1 else None,
            }
        target_set = set(targets)
        target_conflict = any(
            target_set & set(conflict.get("candidate_keys", []))
            for world in worlds
            for conflict in world["candidate_conflicts"]
        )
        outcomes.append(
            {
                "reply_assignment": override,
                "saleable_event_units_cleared": sum(
                    1 for k in targets if candidate_kind(packet, k) == "display"
                ),
                "target_keys_confirmed": len(targets),
                "commitment_orders": order_results,
                "commitment_units_at_stake": len(order_ids),
                "direct_reply_creates_hard_conflict": target_conflict,
            }
        )

    return {
        "request": request_id,
        "issued_if_sent_day": at,
        "receipt_earliest_day": at + lo,
        "receipt_latest_day": at + hi,
        "commitment_deadline": deadline,
        "possible_before_deadline": None if deadline is None else at + lo <= deadline,
        "guaranteed_before_deadline": None if deadline is None else at + hi <= deadline,
        "outcome_count": len(outcomes),
        "outcomes": outcomes,
    }


def request_set_value(packet, at: int, request_ids: list[str]) -> dict[str, Any]:
    """计算请求并集，防止两个重叠凭证的同一批库存被相加。"""
    requests = {r["request"]: r for r in packet["requests"]}
    case_of = case_graph(packet)
    target_union: set[str] = set()
    order_union: set[str] = set()
    minutes = 0
    for request_id in request_ids:
        req = requests[request_id]
        req_targets = split_set(req["targets"])
        target_union |= req_targets
        minutes += int(req["minutes"])
        for row in packet["unresolved_edges"]:
            if row["event_key"] in req_targets and row["order"] not in (None, "-"):
                order_union.add(row["order"])
    master_cases = {case_of.get(key, key) for key in target_union}
    saleable = sum(1 for key in target_union if candidate_kind(packet, key) == "display")
    overlaps = []
    seen: set[str] = set()
    for request_id in request_ids:
        targets = split_set(requests[request_id]["targets"])
        overlap = targets & seen
        if overlap:
            overlaps.append({"later_request": request_id, "shared_targets": sorted(overlap)})
        seen |= targets
    return {
        "requests": request_ids,
        "total_minutes_if_done_separately": minutes,
        "union_targets": sorted(target_union),
        "union_target_count": len(target_union),
        "union_saleable_event_units": saleable,
        "union_orders": sorted(order_union),
        "union_master_cases": sorted(master_cases),
        # 承诺按 order 计一个风险单位；主案按连通匹配组计一个，不能把重叠
        # 请求各自声称的“释放量”相加。
        "commitment_units": len(order_union & {c["order"] for c in packet["commitments"]}),
        "overlaps": overlaps,
    }


def candidate_kind(packet, key: str) -> str | None:
    for row in packet["unresolved_edges"]:
        if row["event_key"] == key:
            return row["kind"]
    return None


# ---------------------------------------------------------------------------
# 复核/班次
# ---------------------------------------------------------------------------


def review_board(packet, at: int, observation_days: int = 5) -> dict[str, Any]:
    reviews = visible(packet["reviews"], at, "day")
    reviewed_serials = {r["serial"]: r for r in reviews}
    summaries = serial_summaries(packet, at)
    items = []
    for serial, summary in summaries.items():
        due = None if summary["first_display_day"] is None else summary["first_display_day"] + observation_days
        review = reviewed_serials.get(serial)
        if review:
            state = "reviewed"
        elif due is None:
            state = "no_observation_window"
        elif at < due:
            state = "observation_not_due"
        else:
            state = "window_expired_unreviewed"
        items.append(
            {
                "serial": serial,
                "edition": summary["edition"],
                "channel": summary["origin_channel"],
                "price_band": summary["price_band"],
                "state": state,
                "review_outcome": review["outcome"] if review else None,
                "review_basis": review["basis"] if review else None,
                "review_day": int(review["day"]) if review else None,
                "observation_due_day": due,
                "terminal_sale": bool(summary["active_sale_orders"]) and not summary["duplicate_active_sale"],
                "backlog": summary["backlog_candidate"],
            }
        )
    counts = Counter(x["state"] for x in items)
    basis = defaultdict(Counter)
    for review in reviews:
        basis[review["basis"]][review["outcome"]] += 1
    return {
        "observation_days": observation_days,
        "counts": dict(counts),
        "basis_outcomes": {k: dict(v) for k, v in basis.items()},
        "items": items,
    }


def direction_board(packet, at: int) -> list[dict[str, Any]]:
    """对编号走向未明/有硬冲突的件，分别回答积压与核查价值。"""
    snap = world_snapshot(packet, at)
    candidate_serials = sorted({s for values in candidate_groups(packet, at).values() for s in values})
    conflict_serials = sorted({c["serial"] for c in snap["hard_conflicts"]})
    serials = sorted(set(candidate_serials) | set(conflict_serials))
    confirmed = confirmed_replies(packet, at)
    pending = {p["request"]: p for p in pending_replies(packet, at)}
    board = []

    for serial in serials:
        backlog_worlds = [w for w in snap["worlds"] if serial in w["backlog_serials"]]
        related_keys = [k for k, values in candidate_groups(packet, at).items() if serial in values]
        unresolved_keys = [k for k in related_keys if k not in confirmed]
        related_requests = [
            r["request"]
            for r in packet["requests"]
            if set(split_set(r["targets"])) & set(related_keys)
        ]
        request_states = {rid: pending[rid]["status"] for rid in related_requests if rid in pending}
        related_commitments = [c for c in snap["worlds"][0]["commitments"] if c["committed_serial"] == serial]
        has_hard_conflict = serial in conflict_serials

        future_deadline = any(c["deadline"] >= at for c in related_commitments)
        open_request_window = any(status in {"receipt_window_open", "receipt_due_today"} for status in request_states.values())
        expired_without_proof = any(status == "expired_without_confirmation" for status in request_states.values())

        if has_hard_conflict or any(not c["safe_terminal"] for c in related_commitments):
            action = "immediate_joint_case_decision"
            forward_value = future_deadline or open_request_window
            audit_value = True
            reason = "存在硬冲突或未安全终结的承诺/订单，需要联合主案处理"
        elif len(backlog_worlds) == len(snap["worlds"]):
            action = "confirmed_backlog_follow_operational_queue"
            forward_value = False
            audit_value = False
            reason = "所有可行匹配均确认积压；无需再为编号发询，应走促销/调拨/清柜处理"
        elif unresolved_keys and expired_without_proof:
            action = "backup_voucher_or_close_as_inference_only"
            forward_value = False
            audit_value = True
            reason = "直接凭证窗口已失效；匹配约束可能已收敛区间，但缺直接证据"
        elif unresolved_keys:
            action = "keep_identity_uncertain_pending_receipt"
            forward_value = True
            audit_value = True
            reason = "仍有未确认编号事件且回执/决定窗口未结束"
        else:
            action = "evidence_resolved_follow_normal_flow"
            forward_value = False
            audit_value = False
            reason = "编号已有确认回执且无未决承诺冲突"

        board.append(
            {
                "serial": serial,
                "backlog_confirmed_across_worlds": len(backlog_worlds) == len(snap["worlds"]),
                "backlog_possible_in_any_world": bool(backlog_worlds),
                "unresolved_event_keys": unresolved_keys,
                "related_requests": related_requests,
                "request_states": request_states,
                "hard_conflict": has_hard_conflict,
                "future_deadline_or_open_window_value": forward_value,
                "residual_audit_or_recovery_value": audit_value,
                "recommended_action": action,
                "reason": reason,
            }
        )
    return board


def policy_board(packet, at: int) -> dict[str, Any]:
    policies = {row["edition"]: row for row in packet["policies"]}
    dims = serial_dimensions(packet)
    reviews_by_serial = defaultdict(list)
    for review in visible(packet["reviews"], at, "day"):
        reviews_by_serial[review["serial"]].append(review)

    suggestions = []
    for row in packet["suggestions"]:
        serial = row["serial"]
        edition = dims[serial]["edition"]
        policy = policies[edition]
        issued = int(row["issued"])
        due = issued + int(policy["delay"])
        reviews = reviews_by_serial.get(serial, [])
        if row["state"] == "closed":
            status = "source_closed"
        elif reviews:
            status = "review_on_record_keep_case_state"
        elif at > due:
            status = "sla_expired_without_review"
        elif at == due:
            status = "sla_due_today"
        else:
            status = "within_review_sla"
        suggestions.append(
            {
                "serial": serial,
                "edition": edition,
                "issued": issued,
                "sla_due_day": due,
                "owner": row["owner"],
                "source_state": row["state"],
                "status": status,
                "reviews": [
                    {"check": r["check"], "day": int(r["day"]), "outcome": r["outcome"], "basis": r["basis"]}
                    for r in reviews
                ],
            }
        )
    return {
        "policies": packet["policies"],
        "suggestion_sla": suggestions,
        "false_hold_rate_computable": False,
        "false_hold_note": "缺少 review 是否导致 hold 的字段；legit 不能直接等同于错误搁置。",
    }


def shift_summary(packet) -> list[dict[str, Any]]:
    result = []
    for shift in packet["shift_capacity"]:
        access = split_set(shift["access"])
        result.append(
            {
                "shift": shift["shift"],
                "day": int(shift["day"]),
                "type": shift["sampled"],
                "access": sorted(access),
                "minutes": int(shift["minutes"]),
                "inclusion": shift["inclusion"],
            }
        )
    return result


def case_board(packet, at: int = 12) -> list[dict[str, Any]]:
    """把匹配组、请求、承诺、建议和复核合并成联合主案。"""
    case_of = case_graph(packet)
    cases: dict[str, dict[str, Any]] = {}

    def get_case(case_id: str) -> dict[str, Any]:
        return cases.setdefault(
            case_id,
            {
                "case_id": case_id,
                "event_keys": set(),
                "serials": set(),
                "requests": set(),
                "orders": set(),
                "commitments": set(),
                "suggestions": [],
                "reviews": [],
            },
        )

    for key, root in case_of.items():
        case = get_case(root)
        case["event_keys"].add(key)
        for serial in candidate_groups(packet, at).get(key, []):
            case["serials"].add(serial)

    for req in packet["requests"]:
        targets = split_set(req["targets"])
        roots = {case_of.get(k, k) for k in targets}
        for root in roots:
            case = get_case(root)
            case["requests"].add(req["request"])
            for key in targets:
                case["event_keys"].add(key)
            for row in packet["unresolved_edges"]:
                if row["event_key"] in targets and row["order"] not in (None, "-"):
                    case["orders"].add(row["order"])

    for commit in packet["commitments"]:
        # 通过 order 找匹配事件；找不到则以 serial 建独立主案。
        linked_roots = set()
        for row in packet["unresolved_edges"]:
            if row["order"] == commit["order"]:
                linked_roots.add(case_of.get(row["event_key"], row["event_key"]))
        if not linked_roots:
            linked_roots.add(f"serial:{commit['serial']}")
        for root in linked_roots:
            case = get_case(root)
            case["commitments"].add(commit["order"])
            case["orders"].add(commit["order"])
            case["serials"].add(commit["serial"])

    for suggestion in packet["suggestions"]:
        serial = suggestion["serial"]
        root = next((cid for cid, case in cases.items() if serial in case["serials"]), f"serial:{serial}")
        case = get_case(root)
        case["serials"].add(serial)
        case["suggestions"].append(
            {
                "serial": serial,
                "issued": int(suggestion["issued"]),
                "owner": suggestion["owner"],
                "source_state": suggestion["state"],
            }
        )

    reviews = visible(packet["reviews"], at, "day")
    for review in reviews:
        serial = review["serial"]
        root = next((cid for cid, case in cases.items() if serial in case["serials"]), f"serial:{serial}")
        case = get_case(root)
        case["serials"].add(serial)
        case["reviews"].append(
            {
                "check": review["check"],
                "day": int(review["day"]),
                "basis": review["basis"],
                "outcome": review["outcome"],
            }
        )

    result = []
    for case in cases.values():
        result.append(
            {
                "case_id": case["case_id"],
                "event_keys": sorted(case["event_keys"]),
                "serials": sorted(case["serials"]),
                "requests": sorted(case["requests"]),
                "orders": sorted(case["orders"]),
                "commitments": sorted(case["commitments"]),
                "suggestions": case["suggestions"],
                "reviews": case["reviews"],
            }
        )
    return sorted(result, key=lambda x: x["case_id"])


# ---------------------------------------------------------------------------
# 时点变化
# ---------------------------------------------------------------------------


def knowledge_changelog(packet) -> list[dict[str, Any]]:
    dates = sorted(
        {int(r["received"]) for r in packet["transactions"]}
        | {int(r["received"]) for r in packet["actual_replies"]}
    )
    previous = None
    changes = []
    for day in dates:
        snap = world_snapshot(packet, day)
        feasible = snap["worlds"]
        undelivered = sorted({w["undelivered_total"] for w in feasible})
        backlog = sorted({w["backlog_total"] for w in feasible})
        assignments = {k: sorted({w["assignment"].get(k) for w in feasible}) for k in candidate_groups(packet)}
        turnover_signature = json.dumps(snap["metric_ranges"]["turnover"], sort_keys=True, default=str)
        signature = (
            tuple(undelivered),
            tuple(backlog),
            json.dumps(assignments, sort_keys=True),
            turnover_signature,
        )
        events = []
        for row in packet["transactions"]:
            if int(row["received"]) == day and row["day"] != row["received"]:
                events.append(f"late-tx:{row['event']}({row['serial']},{row['kind']},{row['order']})")
        for row in packet["actual_replies"]:
            if int(row["received"]) == day:
                events.append(f"reply:{row['reply']}/{row['request']}/{row['outcome']}/{row['event_key']}->{row['serial']}")
        metric_changed = previous is None or signature != previous[0]
        changes.append(
            {
                "received_day": day,
                "metric_changed": metric_changed,
                "events": events,
                "undelivered_range": undelivered,
                "backlog_range": backlog,
                "possible_assignments": assignments,
            }
        )
        previous = (signature, day)
    return changes


def lookahead_audit(packet) -> dict[str, Any]:
    """列出后来才可见、禁止用于更早回演的关键事实。"""
    o1_at_9 = next(c for c in world_snapshot(packet, 9)["worlds"][0]["commitments"] if c["order"] == "O1")
    o1_at_12 = next(c for c in world_snapshot(packet, 12)["worlds"][0]["commitments"] if c["order"] == "O1")
    rq4 = next(p for p in pending_replies(packet, 12) if p["request"] == "RQ4")
    return {
        "section": "lookahead_guard",
        "rules": [
            "as_of=d 只纳入 received<=d 的交易/回执和 day<=d 的复核/班次。",
            "未实际发出的请求没有真实回执；只能给条件情景，不能假设其最佳结果。",
            "最终唯一匹配 E-U19=N-018 用到 day8 才收到的 N-019/B019 记录；day7 发 RQ3 时不能把它当作已知。",
        ],
        "facts_late_arrival": [
            {
                "fact": "O1 fulfilment",
                "actual_day": o1_at_12["actual_fulfil_day"],
                "known_on": o1_at_12["fulfilment_known_on"],
                "visible_at_day_9": o1_at_9["status"] != "delivered_visible",
            },
            {
                "fact": "RQ4 confirmation E-U35=N-036",
                "issued": rq4["issued"],
                "known_on": rq4["latest_reply_day"],
                "deadline": 9,
                "guaranteed_before_deadline": False,
            },
        ],
    }


# ---------------------------------------------------------------------------
# 文本报告
# ---------------------------------------------------------------------------


def format_range(values: list[int | float]) -> str:
    if not values:
        return "-"
    lo, hi = min(values), max(values)
    return str(lo) if lo == hi else f"{lo}..{hi}"


def text_report(packet, at: int) -> str:
    snap = world_snapshot(packet, at)
    review = review_board(packet, at)
    lines: list[str] = []
    lines.append(f"柜面流转值班看板（知识截止 received/day <= {at}）")
    lines.append("=" * 72)
    lines.append(
        f"可行匹配 {snap['feasible_world_count']}/{snap['world_count']}；"
        f"已知硬冲突 {len(snap['hard_conflicts'])} 组。"
    )

    # 指标区间
    undelivered = [w["undelivered_total"] for w in snap["worlds"]]
    backlog = [w["backlog_total"] for w in snap["worlds"]]
    lines.append(f"未交付约定量区间: {format_range(undelivered)}；可确认积压编号量区间: {format_range(backlog)}。")
    lines.append("")

    turnover_ranges = snap["metric_ranges"]["turnover"]
    for dimension, title in [
        ("edition", "发行档/批"),
        ("price_band", "价带（缺失值按已观察 edition-grade 对应推断）"),
        ("direct_grade", "登记价带原值（未登记不推断）"),
        ("origin_channel", "首次上架渠道"),
        ("edition_price_band_channel", "组合（同一口径，不跨维相加）"),
    ]:
        lines.append(f"[{title}]")
        for key, stat in turnover_ranges[dimension].items():
            first_env = stat["first_turnaround_envelope"]
            final_env = stat["final_turnaround_envelope"]
            lines.append(
                f"  {key}: n={stat['serial_count']}, "
                f"积压区间={format_range(stat['backlog_count_range'])}, "
                f"重复销售冲突区间={format_range(stat['duplicate_sale_count_range'])}, "
                f"首笔周转包络={first_env[0]}..{first_env[1]}, 中位区间={format_range(stat['first_median_range'])}, "
                f"最终有效周转包络={final_env[0]}..{final_env[1]}, 中位区间={format_range(stat['final_median_range'])}"
            )
        lines.append("")

    lines.append("[约定/交付状态：逐可行世界]")
    for world in snap["worlds"]:
        assignment = ",".join(f"{k}={v}" for k, v in sorted(world["assignment"].items())) or "无候选编号"
        lines.append(f"  世界 [{assignment}]，未交付合计={world['undelivered_total']}")
        for c in world["commitments"]:
            lines.append(
                f"    {c['order']} / {c['committed_serial']} / {c['edition']} / {c['price_band']} / {c['channel']}: "
                f"{c['status']}，hold={c['hold_identity']}，未交付={c['undelivered']}，截止日={c['deadline']}"
                + (f"，交付记录收到日={c['fulfilment_known_on']}" if c["fulfilment_known_on"] else "")
            )
    lines.append("")

    lines.append("[凭证回执状态]")
    for p in pending_replies(packet, at):
        lines.append(
            f"  {p['request']}: {p['status']}，issued={p['issued']}，"
            f"窗={p['window_earliest']}..{p['window_latest']}，targets={','.join(p['targets'])}"
        )
    lines.append("")

    pri = voucher_priority(packet, at)
    lines.append("[凭证优先级（按当前截止日重算；未发请求不能假设已有回执）]")
    for x in pri["ranked"]:
        if x["available"] > at:
            continue
        deadline = x.get("commitment_deadline")
        timing = "-"
        if deadline is not None:
            timing = (
                f"承诺D{deadline}前可能={'是' if x['possible_before_deadline'] else '否'},"
                f"保证={'是' if x['guaranteed_before_deadline'] else '否'}"
            )
        lines.append(
            f"  {x['request']}: [{x['action']}] {x['action_reason']}；"
            f"边际目标={','.join(x['marginal_unresolved_targets']) or '-'}，"
            f"未决目标={','.join(x['unresolved_targets']) or '-'}，工时={x['minutes']}，"
            f"权限候选={'+'.join(x['access'])}，{timing}"
        )
    lines.append("")

    lines.append("[复核状态]")
    lines.append(f"  {review['counts']}")
    for basis, outcomes in review["basis_outcomes"].items():
        lines.append(f"  basis={basis}: {outcomes}（选择样本，不能外推为同一总体精度）")
    policy = policy_board(packet, at)
    lines.append("  [建议单 SLA]")
    for item in policy["suggestion_sla"]:
        lines.append(
            f"    {item['serial']}: {item['status']}, issued={item['issued']}, "
            f"due={item['sla_due_day']}, owner={item['owner']}, source={item['source_state']}"
        )
    lines.append(f"  max_false_hold 合规率可计算: {policy['false_hold_rate_computable']}（{policy['false_hold_note']}）")
    lines.append("")
    lines.append("[时点变化]")
    for ch in knowledge_changelog(packet):
        marker = "指标变化" if ch["metric_changed"] else "仅回执/记录，指标区间不变"
        lines.append(
            f"  收到日 {ch['received_day']}（{marker}）: {'; '.join(ch['events']) or '-'} | "
            f"未交付区间={ch['undelivered_range']}, 积压区间={ch['backlog_range']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="限量版柜面流转时点分析")
    parser.add_argument("--root", type=Path, default=Path("data"), help="资料目录")
    sub = parser.add_subparsers(dest="command")

    p_report = sub.add_parser("report", help="输出文本或 JSON 看板")
    p_report.add_argument("--at", type=int)
    p_report.add_argument("--format", choices=["text", "json"], default="text")

    p_json = sub.add_parser("snapshot", help="输出完整 JSON 快照")
    p_json.add_argument("--at", type=int)

    p_item = sub.add_parser("item", help="查看单个编号的时点历程")
    p_item.add_argument("serial")
    p_item.add_argument("--at", type=int)

    p_v = sub.add_parser("vouchers", help="凭证优先级")
    p_v.add_argument("--at", type=int)
    p_v.add_argument("--shift")
    p_v.add_argument("--format", choices=["text", "json"], default="text")

    p_sc = sub.add_parser("scenarios", help="单个凭证的合法回执情景")
    p_sc.add_argument("request")
    p_sc.add_argument("--at", type=int)

    p_case = sub.add_parser("cases", help="联合主案")
    p_case.add_argument("--at", type=int)

    p_dir = sub.add_parser("direction", help="未明编号的积压确认与核查价值")
    p_dir.add_argument("--at", type=int)

    p_policy = sub.add_parser("policy", help="政策容量、SLA 与误搁率可计算性")
    p_policy.add_argument("--at", type=int)

    sub.add_parser("shifts", help="班次与复核口径 JSON")
    sub.add_parser("replay", help="输出历史时点回演 JSON")

    args = parser.parse_args(argv)
    packet = load_packet(args.root)
    at = getattr(args, "at", None) or as_of_day(packet)

    if args.command in (None, "report"):
        if getattr(args, "format", "text") == "json":
            print(json.dumps(world_snapshot(packet, at), ensure_ascii=False, indent=2))
        else:
            print(text_report(packet, at))
        return 0

    if args.command == "snapshot":
        print(json.dumps(world_snapshot(packet, at), ensure_ascii=False, indent=2))
        return 0

    if args.command == "item":
        summaries = serial_summaries(packet, at)
        timelines = serial_timeline(packet, at)
        print(json.dumps({"summary": summaries.get(args.serial), "events": timelines.get(args.serial, [])}, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "vouchers":
        result = voucher_priority(packet, at, args.shift)
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for x in result["ranked"]:
                print(f"{x['request']}\t{x['minutes']}min\ttargets={','.join(x['targets'])}\tunresolved={','.join(x['unresolved_targets'])}")
        return 0

    if args.command == "scenarios":
        print(json.dumps(request_scenarios(packet, at, args.request), ensure_ascii=False, indent=2))
        return 0

    if args.command == "cases":
        print(json.dumps(case_board(packet, at), ensure_ascii=False, indent=2))
        return 0

    if args.command == "direction":
        print(json.dumps(direction_board(packet, at), ensure_ascii=False, indent=2))
        return 0

    if args.command == "policy":
        print(json.dumps(policy_board(packet, at), ensure_ascii=False, indent=2))
        return 0

    if args.command == "shifts":
        print(
            json.dumps(
                {"shifts": shift_summary(packet), "reviews": review_board(packet, at)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "replay":
        days = sorted({int(s["day"]) for s in packet["shift_capacity"]})
        requests_by_id = {r["request"]: r for r in packet["requests"]}
        out = []
        for day in days:
            shift = next((s for s in packet["shift_capacity"] if int(s["day"]) == day), None)
            priority = voucher_priority(packet, day, shift["shift"], predecision=True)
            candidate_issues = [
                x["request"]
                for x in priority["ranked"]
                if x["action"] == "issue_candidate_request"
                and x["fits_selected_shift_access"]
                and x["fits_selected_shift_minutes"]
            ]
            logged_issued = sorted({
                r["request"] for r in packet["actual_replies"] if int(r["issued"]) == day
            })
            out.append(
                {
                    "day": day,
                    "shift": shift["shift"] if shift else None,
                    "logged_rule_issued": logged_issued,
                    "candidate_rule_first": candidate_issues[:1],
                    "candidate_rule_ranked_available": candidate_issues,
                    "same_first_action": bool(logged_issued and candidate_issues and logged_issued[0] == candidate_issues[0]),
                    "counterfactual_without_historical_reply": sorted(set(candidate_issues[:1]) - set(logged_issued)),
                    "predecision_top": priority["ranked"][:3],
                    "end_of_day_pending": pending_replies(packet, day),
                    "visible_review_outcomes_today": [
                        {"check": r["check"], "serial": r["serial"], "basis": r["basis"], "outcome": r["outcome"]}
                        for r in visible(packet["reviews"], day, "day")
                        if int(r["day"]) == day
                    ],
                    "snapshot": {
                        "feasible_world_count": world_snapshot(packet, day)["feasible_world_count"],
                        "undelivered_range": world_snapshot(packet, day)["metric_ranges"]["scalars"]["undelivered_total"],
                        "backlog_range": world_snapshot(packet, day)["metric_ranges"]["scalars"]["backlog_total"],
                    },
                }
            )
        out.append(lookahead_audit(packet))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    parser.error(f"未知命令 {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
