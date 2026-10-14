"""选股结果入库（供外部 agent 查阅；截面统计由外部完成）。

后端按 URI 选择：sqlite:///path.db 或 mongodb[+srv]://.../<db>（库名必填）。两张表（Mongo 为同名集合）：

  screener_versions  (strategy, ver) → fields + thresholds
      策略版本 = 输出列 + 门槛快照，两者都由策略代码派生；ver 是其规范 JSON 的哈希，
      即内容自校验码：重算哈希 ≠ ver 说明记录被外部改过。同一版本跨日期/市场只存一份。
  screener_runs      一次运行（日期 × 策略 × 市场）一条，候选压成行数组，行序即排名
      id "20260820:pr:A", date, strategy, market, ver, generated_at,
      l1_count, returned, stats {min_listing_days, l2_reject_reasons, ...}, rows [[...], ...]

SQLite 中 JSON 列为文本（可用 json_each 展开）；Mongo 中 _id 即 id，版本 _id 为 "<strategy>:<ver>"。
外部读取：按 (strategy, ver) 关联取 fields，DataFrame(rows, columns=fields)，按列名取值。

一致性：写入时版本记录只插不改，已存在则校验哈希，不符抛 ConsistencyError；
--verify 全量检查版本哈希、运行的版本引用、id 与键列、行宽与条数。

本地 output/screener/<date>/*.json 是完整结果，库里只放精简投影；
同一运行重跑整条替换，可用命令行回填历史。

兼容约定：
  - 同名策略调门槛（大写常量）或增减输出列 → ver 自动变化；跨期比较按 (strategy, ver) 分段，
    两版本 thresholds 相同则选股条件未变、仅输出列不同。
  - 改了常量以外的判据逻辑 → 在策略模块里递增 VERSION 常量（它也进快照，ver 随之变化）。
  - 选股理念变了 → 起新 NAME，不复用旧名。
  - FIELDS 只追加新列，不改已有列名的含义。
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pymongo import IndexModel, ReplaceOne, UpdateOne

from db_engine.conn import is_mongo_uri, mongo_database

RUNS, VERSIONS = "screener_runs", "screener_versions"

# (输出列, 取值, 保留小数位；None 不取整)。候选字段各策略一致（L1 + snapshot）。
FIELDS: tuple[tuple[str, str, int | None], ...] = (
    ("code", "code", None),
    ("name", "name", None),
    ("mv", "total_market_val", 0),
    ("pe_ttm", "pe_ttm_ratio", 2),
    ("pb", "pb_ratio", 2),
    ("roe", "return_on_equity_rate", 2),
    ("dy", "dividend_ratio_ttm", 2),
    ("price", "last_price", 3),
    ("chg_pct", "change_pct", 2),
    ("turnover", "turnover", 0),
)
FIELD_NAMES = [name for name, _, _ in FIELDS]

# 运行级统计：除 candidates / thresholds 外的结果元数据
_STAT_KEYS = ("min_listing_days", "filtered_new_listing", "snapshot_enriched",
              "l2_refined", "l2_filtered", "l2_reject_reasons")


class ConsistencyError(RuntimeError):
    """库内记录与其内容哈希/引用不一致（被外部改动或写入方违反约定）。"""


def _cell(value, digits: int | None):
    if digits is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    return int(round(value)) if digits == 0 else round(value, digits)


def version_hash(fields: list, thresholds: dict | None) -> str:
    blob = json.dumps({"fields": fields, "thresholds": thresholds},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


def to_records(result: dict) -> tuple[dict, dict]:
    """screen() 结果（已含 date/generated_at）→ (运行记录, 版本记录)。"""
    date, strategy, market = result["date"], result["strategy"], result["market"]
    thresholds = result.get("thresholds")  # 无快照时版本仅由 fields 决定
    ver = version_hash(FIELD_NAMES, thresholds)
    run = {
        "id": f"{date}:{strategy}:{market}",
        "date": date,
        "strategy": strategy,
        "market": market,
        "ver": ver,
        "generated_at": result.get("generated_at"),
        "l1_count": result.get("l1_count"),
        "returned": result.get("returned"),
        "stats": {k: result[k] for k in _STAT_KEYS if k in result},
        "rows": [[_cell(c.get(key), digits) for _, key, digits in FIELDS]
                 for c in result.get("candidates", [])],
    }
    version = {"strategy": strategy, "ver": ver,
               "fields": FIELD_NAMES, "thresholds": thresholds}
    return run, version


def _version_problem(v: dict) -> str | None:
    if version_hash(v["fields"], v["thresholds"]) != v["ver"]:
        return f"{VERSIONS}[{v['strategy']}:{v['ver']}] 内容与哈希不符（被外部改动）"
    return None


def _check_existing(existing: dict, versions: list[dict]) -> None:
    for v in versions:
        stored = existing.get((v["strategy"], v["ver"]))
        if stored is not None and (problem := _version_problem(stored)):
            raise ConsistencyError(problem)


def check(runs: list[dict], versions: list[dict]) -> list[str]:
    """全量一致性检查，返回问题描述（空列表即一致）。"""
    problems = [p for v in versions if (p := _version_problem(v))]
    widths = {(v["strategy"], v["ver"]): len(v["fields"]) for v in versions}
    for r in runs:
        rid = r["id"]
        if rid != f"{r['date']}:{r['strategy']}:{r['market']}":
            problems.append(f"{RUNS}[{rid}] id 与 date/strategy/market 不符")
        width = widths.get((r["strategy"], r["ver"]))
        if width is None:
            problems.append(f"{RUNS}[{rid}] 引用的版本 {r['strategy']}:{r['ver']} 不存在")
        elif any(len(row) != width for row in r["rows"]):
            problems.append(f"{RUNS}[{rid}] 行宽与版本 fields 不符")
        if r["returned"] is not None and r["returned"] != len(r["rows"]):
            problems.append(f"{RUNS}[{rid}] returned={r['returned']} 与行数 {len(r['rows'])} 不符")
    return problems


# ---- SQLite ----

_SQLITE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {VERSIONS} (
    strategy   TEXT NOT NULL,
    ver        TEXT NOT NULL,   -- fields + thresholds 的哈希
    fields     JSON NOT NULL,
    thresholds JSON,            -- 无门槛快照时为 NULL
    PRIMARY KEY (strategy, ver)
);
CREATE TABLE IF NOT EXISTS {RUNS} (
    id           TEXT PRIMARY KEY,   -- <date>:<strategy>:<market>
    date         TEXT NOT NULL,
    strategy     TEXT NOT NULL,
    market       TEXT NOT NULL,
    ver          TEXT NOT NULL,      -- → {VERSIONS}(strategy, ver)
    generated_at TEXT,
    l1_count     INTEGER,
    returned     INTEGER,
    stats        JSON NOT NULL,
    rows         JSON NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_{RUNS}_smd ON {RUNS} (strategy, market, date);
CREATE INDEX IF NOT EXISTS ix_{RUNS}_ver ON {RUNS} (strategy, ver);
"""
_JSON_COLS = {"fields", "thresholds", "stats", "rows"}


def _sql_insert(conn, table: str, records: list[dict], verb: str) -> None:
    if not records:
        return
    cols = list(records[0])
    conn.executemany(
        f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(json.dumps(r[c], ensure_ascii=False)
               if c in _JSON_COLS and r[c] is not None else r[c]
               for c in cols) for r in records])


def _sql_load(conn, table: str) -> list[dict]:
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    return [{c: (json.loads(v) if c in _JSON_COLS and v is not None else v)
             for c, v in zip(cols, row)} for row in cur]


def _save_sqlite(path: str, runs: list[dict], versions: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(_SQLITE_SCHEMA)
        existing = {}
        for v in versions:
            row = conn.execute(f"SELECT fields, thresholds FROM {VERSIONS} "
                               "WHERE strategy = ? AND ver = ?",
                               (v["strategy"], v["ver"])).fetchone()
            if row:
                existing[(v["strategy"], v["ver"])] = dict(
                    v, fields=json.loads(row[0]),
                    thresholds=json.loads(row[1]) if row[1] is not None else None)
        _check_existing(existing, versions)
        # 版本只插不改：已存在且校验通过即与本次内容相同
        _sql_insert(conn, VERSIONS, versions, "INSERT OR IGNORE")
        _sql_insert(conn, RUNS, runs, "INSERT OR REPLACE")


def _check_sqlite(path: str) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(_SQLITE_SCHEMA)
        return check(_sql_load(conn, RUNS), _sql_load(conn, VERSIONS))


# ---- MongoDB ----

def _vid(v: dict) -> str:
    return f"{v['strategy']}:{v['ver']}"


def _from_mongo(doc: dict) -> dict:
    out = dict(doc)
    out.pop("_id")
    return out


def _save_mongo(db_uri: str, runs: list[dict], versions: list[dict]) -> None:
    db = mongo_database(db_uri)
    db[RUNS].create_indexes([IndexModel([("strategy", 1), ("market", 1), ("date", -1)]),
                             IndexModel([("strategy", 1), ("ver", 1)])])
    existing = {(d["strategy"], d["ver"]): _from_mongo(d) for d in
                db[VERSIONS].find({"_id": {"$in": [_vid(v) for v in versions]}})}
    _check_existing(existing, versions)
    missing = [v for v in versions if (v["strategy"], v["ver"]) not in existing]
    if missing:
        # 只插不改；$setOnInsert 使同一版本被多个进程并发写入时幂等
        db[VERSIONS].bulk_write(
            [UpdateOne({"_id": _vid(v)}, {"$setOnInsert": v}, upsert=True) for v in missing],
            ordered=False)
    db[RUNS].bulk_write(
        [ReplaceOne({"_id": r["id"]}, {k: v for k, v in r.items() if k != "id"}, upsert=True)
         for r in runs], ordered=False)


def _check_mongo(db_uri: str) -> list[str]:
    db = mongo_database(db_uri)
    versions = [_from_mongo(d) for d in db[VERSIONS].find()]
    runs = [dict(_from_mongo(d), id=d["_id"]) for d in db[RUNS].find()]
    return check(runs, versions)


# ---- 入口 ----

def _backend(db_uri: str, sqlite_fn, mongo_fn, *args):
    if db_uri.startswith("sqlite:///"):
        return sqlite_fn(db_uri.removeprefix("sqlite:///"), *args)
    if is_mongo_uri(db_uri):
        return mongo_fn(db_uri, *args)
    raise ValueError(f"不支持的数据库 URI: {db_uri.split(':', 1)[0]}://...")


def save(db_uri: str, results: list[dict]) -> list[str]:
    """写入若干 screen() 结果，返回运行 id；同一运行整条替换。"""
    runs, versions = [], {}
    for result in results:
        run, version = to_records(result)
        runs.append(run)
        versions[(version["strategy"], version["ver"])] = version
    _backend(db_uri, _save_sqlite, _save_mongo, runs, list(versions.values()))
    return [r["id"] for r in runs]


def config_uri(config: configparser.ConfigParser) -> str:
    """[CONFIG] SCREENER_DB（raw 读取，URI 中百分号编码的密码不受插值影响）。"""
    uri = config.get("CONFIG", "SCREENER_DB", raw=True, fallback="").strip()
    if not uri:
        raise ValueError("配置缺少 [CONFIG] SCREENER_DB")
    return uri


def upload(db_uri: str, result: dict) -> str:
    return save(db_uri, [result])[0]


def verify(db_uri: str) -> list[str]:
    return _backend(db_uri, _check_sqlite, _check_mongo)


def main() -> None:
    ap = argparse.ArgumentParser(description="回填 output/screener 下的选股结果到数据库 / 校验一致性")
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--config", help="读取其中 [CONFIG] SCREENER_DB 作为数据库 URI")
    target.add_argument("--db", help="sqlite:///sync/screener.db 或 mongodb+srv://user:pass@host/<db>")
    ap.add_argument("--root", default="output/screener")
    ap.add_argument("--date", help="只回填该日期 YYYYMMDD，默认全部")
    ap.add_argument("--verify", action="store_true", help="只做全量一致性检查，不写入")
    args = ap.parse_args()

    db_uri = args.db
    if args.config:
        config = configparser.ConfigParser()
        if not config.read(args.config, encoding="utf-8"):
            sys.exit(f"配置文件读取失败: {args.config}")
        try:
            db_uri = config_uri(config)
        except ValueError as e:
            sys.exit(f"{e}: {args.config}")

    if args.verify:
        problems = verify(db_uri)
        print("\n".join(problems) or "一致", file=sys.stderr)
        sys.exit(1 if problems else 0)

    files = sorted(Path(args.root).glob(f"{args.date or '[0-9]' * 8}/*_*.json"))
    results = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    if not results:
        sys.exit(f"{args.root} 下没有结果")
    save(db_uri, results)
    rows = sum(len(r.get("candidates", [])) for r in results)
    print(f"已上传 {len(results)} 个运行，{rows} 行", file=sys.stderr)


if __name__ == "__main__":
    main()
