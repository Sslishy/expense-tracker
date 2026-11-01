#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记账本 CLI：通过 GitHub Gist 读写同一份 expense-data.json。"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import string
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

GIST_ID_DEFAULT = "5c4adbfa0ec1c294f3a63854ff998ee0"
GIST_FILE = "expense-data.json"
CSV_PATH = Path("/workspace/ledger/expenses.csv")
TZ = timezone(timedelta(hours=8))
API = "https://api.github.com"

DEFAULT_EXPENSE = ["餐饮", "交通", "购物", "日用", "娱乐", "居住", "通讯", "医疗", "学习", "人情", "投资", "其他"]
DEFAULT_INCOME = ["工资", "兼职", "副业", "理财", "其他"]


def now_local() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now_local().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def to_ms(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def yuan_to_fen(amount: str | float) -> int:
    s = str(amount).strip().replace(",", "")
    if not s or not all(c.isdigit() or c == "." for c in s):
        raise ValueError(f"无效金额: {amount}")
    if s.count(".") > 1:
        raise ValueError(f"无效金额: {amount}")
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    return int(whole) * 100 + int(frac)


def fen_to_yuan(fen: int) -> str:
    n = int(fen)
    sign = "-" if n < 0 else ""
    n = abs(n)
    return f"{sign}{n // 100}.{n % 100:02d}"


def new_id(prefix: str = "cli") -> str:
    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{prefix}-{stamp}-{suffix}"


def get_token() -> str:
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env:
        return env.strip()
    try:
        out = subprocess.check_output(["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL)
        return out.strip()
    except Exception as e:
        raise SystemExit(f"无法获取 GitHub token，请先 gh auth login 或设置 GITHUB_TOKEN: {e}")


def api_request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "expense-tracker-ledger-cli",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API {method} {path} 失败 HTTP {e.code}: {err[:400]}")


def empty_doc() -> dict:
    return {
        "version": 1,
        "updatedAt": now_iso(),
        "entries": [],
        "categories": {"expense": DEFAULT_EXPENSE[:], "income": DEFAULT_INCOME[:]},
        "budget": None,
        "deleted": [],
    }


def normalize_entry(e: dict) -> dict | None:
    if not e or not e.get("id"):
        return None
    amount = e.get("amount")
    if amount is None and "amountFen" in e:
        amount = e["amountFen"]
    if not isinstance(amount, (int, float)):
        return None
    typ = e.get("type") or "expense"
    if typ not in ("expense", "income"):
        typ = "expense"
    return {
        "id": str(e["id"]),
        "type": typ,
        "category": str(e.get("category") or "其他"),
        "amount": int(amount),
        "note": str(e.get("note") or ""),
        "date": str(e.get("date") or today_str()),
        "time": str(e.get("time") or ""),
        "createdAt": e.get("createdAt") or now_iso(),
        "updatedAt": e.get("updatedAt") or e.get("createdAt") or now_iso(),
        "source": str(e.get("source") or ""),
    }


def normalize_doc(doc: dict | None) -> dict:
    base = empty_doc()
    if not doc or not isinstance(doc, dict):
        return base
    entries = []
    for e in doc.get("entries") or []:
        ne = normalize_entry(e)
        if ne:
            entries.append(ne)
    cats = doc.get("categories")
    if isinstance(cats, dict):
        exp = cats.get("expense") or cats.get("expenseCategories") or DEFAULT_EXPENSE
        inc = cats.get("income") or cats.get("incomeCategories") or DEFAULT_INCOME
    else:
        exp = doc.get("expenseCategories") or DEFAULT_EXPENSE
        inc = doc.get("incomeCategories") or DEFAULT_INCOME
    budget = doc.get("budget")
    if budget is None and isinstance(doc.get("monthlyBudgetFen"), (int, float)):
        budget = doc["monthlyBudgetFen"]
    deleted = [str(x) for x in (doc.get("deleted") or []) if x]
    return {
        "version": 1,
        "updatedAt": doc.get("updatedAt") or now_iso(),
        "entries": entries,
        "categories": {
            "expense": list(dict.fromkeys([str(x) for x in exp if x])),
            "income": list(dict.fromkeys([str(x) for x in inc if x])),
        },
        "budget": int(budget) if isinstance(budget, (int, float)) else None,
        "deleted": list(dict.fromkeys(deleted)),
    }


def merge_docs(local: dict, remote: dict) -> dict:
    local = normalize_doc(local)
    remote = normalize_doc(remote)
    deleted = list(dict.fromkeys(local["deleted"] + remote["deleted"]))
    deleted_set = set(deleted)
    by_id: dict[str, dict] = {}
    for e in remote["entries"] + local["entries"]:
        if e["id"] in deleted_set:
            continue
        prev = by_id.get(e["id"])
        if prev is None or to_ms(e["updatedAt"]) >= to_ms(prev["updatedAt"]):
            by_id[e["id"]] = e
    for did in deleted_set:
        by_id.pop(did, None)
    # categories union
    expense = list(dict.fromkeys(remote["categories"]["expense"] + local["categories"]["expense"]))
    income = list(dict.fromkeys(remote["categories"]["income"] + local["categories"]["income"]))
    # budget: newer document wins
    if to_ms(local["updatedAt"]) >= to_ms(remote["updatedAt"]):
        budget = local["budget"]
    else:
        budget = remote["budget"]
    entries = sorted(
        by_id.values(),
        key=lambda e: (e["date"], e.get("time") or "", to_ms(e["updatedAt"])),
        reverse=True,
    )
    return {
        "version": 1,
        "updatedAt": now_iso(),
        "entries": entries,
        "categories": {"expense": expense, "income": income},
        "budget": budget,
        "deleted": deleted,
    }


def pull_gist(token: str, gist_id: str) -> dict:
    data = api_request("GET", f"/gists/{gist_id}", token)
    files = data.get("files") or {}
    f = files.get(GIST_FILE)
    if not f:
        return empty_doc()
    content = f.get("content")
    if f.get("truncated") or not content:
        raw_url = f.get("raw_url")
        if not raw_url:
            return empty_doc()
        req = urllib.request.Request(
            raw_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.raw",
                "User-Agent": "expense-tracker-ledger-cli",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
    try:
        return normalize_doc(json.loads(content))
    except Exception:
        return empty_doc()


def push_gist(token: str, gist_id: str, doc: dict) -> None:
    doc = normalize_doc(doc)
    doc["updatedAt"] = now_iso()
    body = {"files": {GIST_FILE: {"content": json.dumps(doc, ensure_ascii=False, indent=2) + "\n"}}}
    api_request("PATCH", f"/gists/{gist_id}", token, body)


def write_csv_mirror(doc: dict) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(doc["entries"], key=lambda e: (e["date"], e.get("time") or "", e["id"]))
    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["date", "type", "category", "amount", "note", "id", "time", "source"])
        for e in rows:
            w.writerow(
                [
                    e["date"],
                    "支出" if e["type"] == "expense" else "收入",
                    e["category"],
                    fen_to_yuan(e["amount"]),
                    e.get("note") or "",
                    e["id"],
                    e.get("time") or "",
                    e.get("source") or "",
                ]
            )


def load_and_merge(token: str, gist_id: str, local_patch: dict | None = None) -> dict:
    remote = pull_gist(token, gist_id)
    if local_patch is None:
        return remote
    return merge_docs(local_patch, remote)


def filter_entries(entries: list[dict], date: str | None, date_from: str | None, date_to: str | None) -> list[dict]:
    out = entries
    if date:
        out = [e for e in out if e["date"] == date]
    if date_from:
        out = [e for e in out if e["date"] >= date_from]
    if date_to:
        out = [e for e in out if e["date"] <= date_to]
    return out


def print_entry(e: dict) -> None:
    kind = "支出" if e["type"] == "expense" else "收入"
    sign = "-" if e["type"] == "expense" else "+"
    note = f" · {e['note']}" if e.get("note") else ""
    t = e.get("time") or "--:--"
    print(f"  {e['date']} {t}  {kind}  {e['category']}{note}  {sign}¥{fen_to_yuan(e['amount'])}  [{e['id']}]")


def today_totals(doc: dict, day: str | None = None) -> tuple[int, int]:
    day = day or today_str()
    exp = sum(e["amount"] for e in doc["entries"] if e["date"] == day and e["type"] == "expense")
    inc = sum(e["amount"] for e in doc["entries"] if e["date"] == day and e["type"] == "income")
    return exp, inc


def cmd_add(args: argparse.Namespace) -> None:
    token = get_token()
    gist_id = args.gist
    fen = yuan_to_fen(args.amount)
    entry = {
        "id": new_id("bot"),
        "type": args.type,
        "category": args.category,
        "amount": fen,
        "note": args.note or "",
        "date": args.date or today_str(),
        "time": args.time or "",
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
        "source": "记账专家",
    }
    remote = pull_gist(token, gist_id)
    # ensure category exists
    key = "expense" if entry["type"] == "expense" else "income"
    if entry["category"] not in remote["categories"][key]:
        remote["categories"][key].append(entry["category"])
    local = {
        "version": 1,
        "updatedAt": now_iso(),
        "entries": [entry],
        "categories": remote["categories"],
        "budget": remote["budget"],
        "deleted": [],
    }
    merged = merge_docs(local, remote)
    push_gist(token, gist_id, merged)
    write_csv_mirror(merged)
    exp, inc = today_totals(merged)
    kind = "支出" if entry["type"] == "expense" else "收入"
    print(f"已记账：{kind} ¥{fen_to_yuan(fen)} · {entry['category']}" + (f" · {entry['note']}" if entry["note"] else ""))
    print(f"id={entry['id']}")
    print(f"今日支出 ¥{fen_to_yuan(exp)} · 今日收入 ¥{fen_to_yuan(inc)} · 结余 ¥{fen_to_yuan(inc - exp)}")


def cmd_list(args: argparse.Namespace) -> None:
    token = get_token()
    doc = pull_gist(token, args.gist)
    rows = filter_entries(doc["entries"], args.date, args.date_from, args.date_to)
    rows = sorted(rows, key=lambda e: (e["date"], e.get("time") or "", e["id"]))
    if not rows:
        print("没有符合条件的记录")
        return
    print(f"共 {len(rows)} 笔：")
    for e in rows:
        print_entry(e)


def cmd_summary(args: argparse.Namespace) -> None:
    token = get_token()
    doc = pull_gist(token, args.gist)
    if args.month:
        rows = [e for e in doc["entries"] if e["date"].startswith(args.month)]
        label = args.month
    elif args.date:
        rows = [e for e in doc["entries"] if e["date"] == args.date]
        label = args.date
    else:
        rows = [e for e in doc["entries"] if e["date"] == today_str()]
        label = today_str()
    exp = sum(e["amount"] for e in rows if e["type"] == "expense")
    inc = sum(e["amount"] for e in rows if e["type"] == "income")
    print(f"汇总 {label}")
    print(f"  支出 ¥{fen_to_yuan(exp)}  ·  收入 ¥{fen_to_yuan(inc)}  ·  结余 ¥{fen_to_yuan(inc - exp)}")
    by_cat: dict[str, int] = {}
    for e in rows:
        if e["type"] != "expense":
            continue
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + e["amount"]
    if by_cat:
        print("  支出分类：")
        for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
            print(f"    {cat}: ¥{fen_to_yuan(amt)}")


def cmd_delete(args: argparse.Namespace) -> None:
    token = get_token()
    gist_id = args.gist
    remote = pull_gist(token, gist_id)
    eid = args.id
    local = {
        "version": 1,
        "updatedAt": now_iso(),
        "entries": [],
        "categories": remote["categories"],
        "budget": remote["budget"],
        "deleted": [eid],
    }
    # also drop from entries if present
    remote_entries = [e for e in remote["entries"] if e["id"] != eid]
    remote = {**remote, "entries": remote_entries, "deleted": list(dict.fromkeys(remote["deleted"] + [eid]))}
    merged = merge_docs(local, remote)
    push_gist(token, gist_id, merged)
    write_csv_mirror(merged)
    print(f"已删除（墓碑）: {eid}")
    exp, inc = today_totals(merged)
    print(f"今日支出 ¥{fen_to_yuan(exp)} · 今日收入 ¥{fen_to_yuan(inc)}")


def cmd_pull(args: argparse.Namespace) -> None:
    token = get_token()
    doc = pull_gist(token, args.gist)
    write_csv_mirror(doc)
    print(f"已拉取 {len(doc['entries'])} 笔，CSV → {CSV_PATH}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="记账本 Gist CLI")
    p.add_argument("--gist", default=os.environ.get("EXPENSE_GIST_ID", GIST_ID_DEFAULT), help="Gist ID")
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="新增一笔")
    add.add_argument("--type", choices=["expense", "income"], required=True)
    add.add_argument("--category", required=True)
    add.add_argument("--amount", required=True, help="金额（元）")
    add.add_argument("--note", default="")
    add.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天（上海时区）")
    add.add_argument("--time", default=None, help="HH:MM")
    add.set_defaults(func=cmd_add)

    lst = sub.add_parser("list", help="列出记录")
    lst.add_argument("--date", default=None)
    lst.add_argument("--from", dest="date_from", default=None)
    lst.add_argument("--to", dest="date_to", default=None)
    lst.set_defaults(func=cmd_list)

    sm = sub.add_parser("summary", help="汇总")
    sm.add_argument("--date", default=None)
    sm.add_argument("--month", default=None, help="YYYY-MM")
    sm.set_defaults(func=cmd_summary)

    dele = sub.add_parser("delete", help="删除（写入墓碑）")
    dele.add_argument("--id", required=True)
    dele.set_defaults(func=cmd_delete)

    pull = sub.add_parser("pull", help="拉取并刷新本地 CSV")
    pull.set_defaults(func=cmd_pull)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
