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



# ---------------------------------------------------------------------------
# 账单导入框架：每个来源实现一个 parser -> List[BillRow]，其余逻辑（跳过规则、
# 分类、去重、合并、写回）共用。后续 import-alipay 只需新增 parse_alipay_* 并注册。
# ---------------------------------------------------------------------------
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class BillRow:
    when: datetime            # 交易时间（上海时区，naive 即视为本地）
    kind: str                 # 交易类型
    counterparty: str         # 交易对方
    product: str              # 商品
    direction: str            # 收/支 原文：收入 / 支出 / /
    amount_fen: int
    method: str               # 支付方式
    status: str               # 当前状态
    txn_id: str               # 交易单号
    remark: str = ""
    raw_index: int = 0


@dataclass
class ImportReport:
    imported: list = field(default_factory=list)
    skipped: list = field(default_factory=list)      # (row, reason)
    already: list = field(default_factory=list)      # rows already in gist / tombstoned
    dups: list = field(default_factory=list)         # (row, existing entry)
    uncertain: list = field(default_factory=list)    # (entry, reason)


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_xlsx_rows(path: str | Path) -> list[list[Any]]:
    """极简 xlsx 读取（仅标准库）：返回第一个工作表的二维数组；日期序列号保留为 float。"""
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        # 第一个 sheet
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        first = wb.find("m:sheets/m:sheet", _NS)
        rid = first.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = None
        for r in rels:
            if r.get("Id") == rid:
                target = r.get("Target")
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        sheet = ET.fromstring(z.read(target))
    rows: list[list[Any]] = []
    for row in sheet.iter(f"{{{_NS['m']}}}row"):
        cells: dict[int, Any] = {}
        for c in row.findall("m:c", _NS):
            ref = c.get("r", "")
            letters = "".join(ch for ch in ref if ch.isalpha())
            col = 0
            for ch in letters:
                col = col * 26 + (ord(ch.upper()) - 64)
            t = c.get("t")
            v = c.find("m:v", _NS)
            if t == "s" and v is not None:
                val: Any = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
            elif t in ("str", "b", "e"):
                val = v.text if v is not None else None
            elif v is not None and v.text is not None:
                try:
                    f = float(v.text)
                    val = int(f) if f.is_integer() and "." not in v.text and "E" not in v.text.upper() else f
                except ValueError:
                    val = v.text
            else:
                val = None
            cells[col - 1] = val
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i) for i in range(width)])
        else:
            rows.append([])
    return rows


def read_table_file(path: str | Path) -> list[list[Any]]:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return read_xlsx_rows(path)
    for enc in ("utf-8-sig", "gb18030"):
        try:
            with path.open(encoding=enc, newline="") as fp:
                return [list(r) for r in csv.reader(fp)]
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"无法读取文件编码: {path}")


def excel_serial_to_dt(v: float) -> datetime:
    return datetime(1899, 12, 30) + timedelta(days=float(v))


def parse_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        return excel_serial_to_dt(v).replace(microsecond=0)
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"无法解析时间: {v}")


def money_to_fen(v: Any) -> int:
    if isinstance(v, (int, float)):
        return int(round(float(v) * 100))
    s = str(v or "").strip().replace("¥", "").replace("￥", "").replace(",", "")
    return yuan_to_fen(s) if s else 0


def find_header(rows: list[list[Any]], required: list[str]) -> tuple[int, dict[str, int]]:
    for i, r in enumerate(rows[:60]):
        cells = [str(c).strip() if c is not None else "" for c in r]
        if all(any(cell == k for cell in cells) for k in required):
            return i, {cell: j for j, cell in enumerate(cells) if cell}
    raise SystemExit(f"未找到表头（需要列: {', '.join(required)}）")


def _cell(r: list[Any], idx: dict[str, int], *names: str) -> Any:
    for n in names:
        j = idx.get(n)
        if j is not None and j < len(r):
            return r[j]
    return None


def _txt(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s == "/" else s


def parse_wechat(path: str | Path) -> tuple[list[BillRow], str]:
    rows = read_table_file(path)
    h, idx = find_header(rows, ["交易时间", "交易类型", "交易对方", "收/支", "当前状态", "交易单号"])
    period = ""
    for r in rows[:h]:
        line = " ".join(str(c) for c in r if c)
        m = re.search(r"起始时间：\[(.+?)\].*终止时间：\[(.+?)\]", line)
        if m:
            period = f"{m.group(1)} ~ {m.group(2)}"
    out: list[BillRow] = []
    for k, r in enumerate(rows[h + 1:], start=h + 2):
        if not r or _cell(r, idx, "交易时间") in (None, ""):
            continue
        out.append(BillRow(
            when=parse_dt(_cell(r, idx, "交易时间")),
            kind=_txt(_cell(r, idx, "交易类型")),
            counterparty=_txt(_cell(r, idx, "交易对方")),
            product=_txt(_cell(r, idx, "商品")),
            direction=str(_cell(r, idx, "收/支") or "").strip(),
            amount_fen=money_to_fen(_cell(r, idx, "金额(元)", "金额（元）", "金额")),
            method=_txt(_cell(r, idx, "支付方式")),
            status=_txt(_cell(r, idx, "当前状态")),
            txn_id=str(_cell(r, idx, "交易单号") or "").strip(),
            remark=_txt(_cell(r, idx, "备注")),
            raw_index=k,
        ))
    return out, period


# 中性/内部转移 关键字（出现在 交易类型 / 交易对方 / 商品）
NEUTRAL_KEYWORDS = ["零钱提现", "零钱充值", "零钱通", "理财通", "信用卡还款", "转入零钱", "转出零钱",
                    "余额宝", "余利宝", "转账到银行卡", "提现", "充值-零钱"]
SKIP_STATUS = ["失败", "已关闭", "已全额退款", "对方已退还", "已撤销", "已退回", "交易关闭", "退款成功"]

EXPENSE_RULES: list[tuple[str, list[str]]] = [
    ("居住", ["电费", "水费", "燃气", "房租", "物业", "生活缴费", "暖气", "宽带"]),
    ("通讯", ["话费", "流量", "中国移动", "中国联通", "中国电信", "手机充值"]),
    ("交通", ["充电", "滴滴", "嘀嗒", "地铁", "公交", "加油", "停车", "ETCP", "高德打车", "曹操", "T3出行",
             "12306", "铁路", "华铁", "高速", "顺风车", "出行", "打车", "哈啰", "青桔", "中石化", "中石油"]),
    ("医疗", ["药房", "药店", "医院", "诊所", "大药房", "医疗", "体检"]),
    ("学习", ["书店", "课程", "教育", "培训", "知识", "得到", "极客"]),
    ("娱乐", ["电影", "影城", "KTV", "游戏", "沐浴", "洗浴", "足浴", "体育", "健身", "网吧", "景区", "门票", "演出", "腾讯视频", "爱奇艺", "B站"]),
    ("餐饮", ["美团", "饿了么", "餐", "饭", "面", "咖啡", "coffee", "奶茶", "茶饮", "肯德基", "KFC", "麦当劳",
             "汉堡", "汤", "烧肉", "烤", "火锅", "小厨", "食堂", "智盘", "bakery", "面包", "蛋糕", "手抓饼",
             "海鲜", "果", "泡菜", "瑞幸", "星巴克", "喜茶", "蜜雪", "丹香", "饺", "粥", "包子", "早点", "7-ELEVEn", "便利"]),
    ("日用", ["超市", "便利店", "生活用品", "日用", "洗衣", "华兴"]),
    ("购物", ["淘宝", "天猫", "京东", "拼多多", "唯品会", "抖音电商", "得物", "闲鱼", "商城"]),
]
INCOME_RULES: list[tuple[str, list[str]]] = [
    ("工资", ["工资", "薪", "代发"]),
    ("理财", ["收益", "利息", "理财", "基金"]),
    ("兼职", ["兼职"]),
    ("副业", ["稿费", "副业", "佣金"]),
]
PERSON_NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{1,4}$")


def categorize(row: BillRow, typ: str) -> tuple[str, str | None]:
    """返回 (分类, 不确定原因或 None)。"""
    hay = f"{row.counterparty} {row.product} {row.kind}"
    if typ == "income":
        for cat, kws in INCOME_RULES:
            if any(k.lower() in hay.lower() for k in kws):
                return cat, None
        if "红包" in row.kind or "转账" in row.kind:
            return "其他", None
        return "其他", "收入未匹配关键词"
    if "红包" in row.kind or row.kind in ("转账", "群收款"):
        return "人情", None
    for cat, kws in EXPENSE_RULES:
        if any(k.lower() in hay.lower() for k in kws):
            return cat, None
    # 个人收款码（菜市场/小摊）：多为早餐、买菜
    if row.kind == "扫二维码付款" or "二维码收款" in row.product or PERSON_NAME_RE.match(row.counterparty or ""):
        return "餐饮", "个人收款码，按小摊/早餐推测"
    return "其他", "未匹配关键词"


def short_note(row: BillRow) -> str:
    cp = row.counterparty
    prod = row.product
    if not prod or prod == cp or "二维码收款" in prod or prod.startswith("商户单号"):
        prod = ""
    prod = re.sub(r"(美团/大众点评点餐订单)-\d+", r"\1", prod)
    prod = re.sub(r"_?[A-Z]*WX\d{10,}", "", prod)
    prod = prod.replace(cp, "").strip(" -_")
    note = cp + (f" · {prod}" if prod else "")
    return note[:40]


def partial_refund_fen(status: str) -> int:
    m = re.search(r"已退款[（(]?\s*[￥¥]?\s*([\d.,]+)", status or "")
    return money_to_fen(m.group(1)) if m else 0


def bill_rows_to_entries(rows: list[BillRow], source: str, id_prefix: str, report: ImportReport) -> list[dict]:
    entries = []
    ts = now_iso()
    for row in rows:
        hay = f"{row.kind} {row.counterparty} {row.product}"
        if row.direction not in ("收入", "支出"):
            report.skipped.append((row, "中性交易（收/支为 /）"))
            continue
        if any(k in hay for k in NEUTRAL_KEYWORDS):
            report.skipped.append((row, "中性/自有账户间转移"))
            continue
        if any(k in row.status for k in SKIP_STATUS):
            report.skipped.append((row, f"状态：{row.status}"))
            continue
        if row.direction == "收入" and (row.kind.endswith("-退款") or "退款" in row.kind):
            report.skipped.append((row, "退款入账（已在原支出中处理）"))
            continue
        amount = row.amount_fen
        refund = partial_refund_fen(row.status)
        if refund:
            amount -= refund
            if amount <= 0:
                report.skipped.append((row, f"退款后金额为 0（{row.status}）"))
                continue
        typ = "expense" if row.direction == "支出" else "income"
        cat, why = categorize(row, typ)
        e = {
            "id": f"{id_prefix}-{row.txn_id}",
            "type": typ,
            "category": cat,
            "amount": amount,
            "note": short_note(row) + (f"（已扣退款¥{fen_to_yuan(refund)}）" if refund else ""),
            "date": row.when.strftime("%Y-%m-%d"),
            "time": row.when.strftime("%H:%M"),
            "createdAt": ts,
            "updatedAt": ts,
            "source": source,
        }
        if why:
            report.uncertain.append((e, why))
        entries.append(e)
    return entries


def run_import(args: argparse.Namespace, parser_fn, source: str, id_prefix: str) -> None:
    src = Path(args.file)
    if not src.exists():
        raise SystemExit(f"文件不存在: {src}")
    rows, period = parser_fn(src)
    report = ImportReport()
    candidates = bill_rows_to_entries(rows, source, id_prefix, report)

    token = get_token()
    remote = pull_gist(token, args.gist)
    existing_ids = {e["id"] for e in remote["entries"]}
    tomb = set(remote["deleted"])
    manual = [e for e in remote["entries"] if e.get("source") != source]

    to_add = []
    for e in candidates:
        if e["id"] in existing_ids or e["id"] in tomb:
            report.already.append(e)
            continue
        dup = next((m for m in manual if m["date"] == e["date"] and m["amount"] == e["amount"] and m["type"] == e["type"]), None)
        if dup and not args.allow_dups:
            report.dups.append((e, dup))
            continue
        to_add.append(e)
    report.imported = to_add
    report.uncertain = [(e, w) for e, w in report.uncertain if e in to_add]

    print(f"来源：{source}  文件：{src.name}")
    if period:
        print(f"账单区间：{period}")
    print(f"账单行数 {len(rows)}；可导入 {len(candidates)}；新增 {len(to_add)}；已存在 {len(report.already)}；"
          f"疑似重复 {len(report.dups)}；跳过 {len(report.skipped)}")

    exp = sum(e["amount"] for e in to_add if e["type"] == "expense")
    inc = sum(e["amount"] for e in to_add if e["type"] == "income")
    print(f"新增支出 ¥{fen_to_yuan(exp)} · 新增收入 ¥{fen_to_yuan(inc)}")
    by_cat: dict[tuple[str, str], int] = {}
    for e in to_add:
        by_cat[(e["type"], e["category"])] = by_cat.get((e["type"], e["category"]), 0) + e["amount"]
    for (t, c), v in sorted(by_cat.items(), key=lambda x: (x[0][0] != "expense", -x[1])):
        n = sum(1 for e in to_add if e["type"] == t and e["category"] == c)
        print(f"  {'支出' if t == 'expense' else '收入'} {c}: ¥{fen_to_yuan(v)}（{n} 笔）")
    top = sorted([e for e in to_add if e["type"] == "expense"], key=lambda e: -e["amount"])[:5]
    if top:
        print("最大 5 笔支出：")
        for e in top:
            print(f"  {e['date']} {e['note']}  ¥{fen_to_yuan(e['amount'])}  [{e['category']}]")
    if report.skipped:
        print("跳过：")
        for row, why in report.skipped:
            print(f"  {row.when:%Y-%m-%d %H:%M} {row.direction} ¥{fen_to_yuan(row.amount_fen)} {row.counterparty} — {why}")
    if report.dups:
        print("疑似与已有手工记录重复（未导入，可加 --allow-dups）：")
        for e, m in report.dups:
            print(f"  {e['date']} ¥{fen_to_yuan(e['amount'])} {e['note']}  ≈  [{m['id']}] {m.get('note')}")
    if report.uncertain:
        print("分类不确定：")
        for e, why in report.uncertain:
            print(f"  {e['date']} {e['note']} ¥{fen_to_yuan(e['amount'])} → {e['category']}（{why}）")

    if args.dry_run:
        print("（dry-run，未写入）")
        return
    if not to_add:
        print("没有新记录需要写入")
        write_csv_mirror(remote)
        return
    for e in to_add:
        key = e["type"]
        if e["category"] not in remote["categories"][key]:
            remote["categories"][key].append(e["category"])
    local = {**remote, "entries": to_add, "deleted": [], "updatedAt": now_iso()}
    merged = merge_docs(local, remote)
    push_gist(token, args.gist, merged)
    write_csv_mirror(merged)
    print(f"已写入 Gist：共 {len(merged['entries'])} 笔；CSV → {CSV_PATH}")


def cmd_import_wechat(args: argparse.Namespace) -> None:
    run_import(args, parse_wechat, source="微信账单", id_prefix="wx")


# 预留：支付宝账单（CSV，GBK，表头含 交易时间/交易分类/交易对方/商品说明/收/支/金额/交易状态/交易订单号）
# def parse_alipay(path): ...  -> list[BillRow]
# def cmd_import_alipay(args): run_import(args, parse_alipay, source="支付宝账单", id_prefix="ali")


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

    iw = sub.add_parser("import-wechat", help="导入微信支付账单（xlsx/csv）")
    iw.add_argument("file")
    iw.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    iw.add_argument("--allow-dups", action="store_true", help="与手工记录同日同额也导入")
    iw.set_defaults(func=cmd_import_wechat)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
