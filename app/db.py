"""SQLite 连接与建表。数据库路径由环境变量 DATABASE_PATH 指定，默认 ./rates.db。"""

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no   TEXT NOT NULL,          -- 合同号
    rate          REAL NOT NULL,          -- 费率值
    valid_from    TEXT NOT NULL,          -- 业务生效起点（含），UTC ISO 字符串
    valid_to      TEXT,                   -- 业务生效终点（不含），NULL 表示无限期
    recorded_at   TEXT NOT NULL,          -- 系统记录时间（认知时间起点，含）
    superseded_at TEXT,                   -- 被修订时间（认知时间终点，不含），NULL 表示当前认知
    version       INTEGER NOT NULL,       -- 同一合同内单调递增的版本号
    reason        TEXT                    -- 录入/修订说明
);
CREATE INDEX IF NOT EXISTS idx_rate_versions_contract ON rate_versions (contract_no);

-- 版本血缘边：方向为新版本 → 旧版本（祖先方向）。
--   supersedes    ：新费率 → 本次修订被它取代的每个旧版本；
--   retained_from ：补偿片段 → 其费率/区间所来源的旧版本。
-- 一次录入（含补偿片段）产生的全部边与版本行在同一事务写入；
-- 没有出边的版本（含全部历史行）视为血缘根。
CREATE TABLE IF NOT EXISTS rate_version_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_rate_id INTEGER NOT NULL,        -- 新版本（后代）
    to_rate_id   INTEGER NOT NULL,        -- 指向的旧版本（祖先）
    relation    TEXT NOT NULL,            -- supersedes / retained_from
    created_at  TEXT NOT NULL,            -- 边建立时间（= 修订 recorded_at，UTC ISO）
    UNIQUE (from_rate_id, to_rate_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_rve_from ON rate_version_edges (from_rate_id);
CREATE INDEX IF NOT EXISTS idx_rve_to ON rate_version_edges (to_rate_id);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", "rates.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """建表（幂等），应用启动时自动调用。"""
    with connect() as conn:
        conn.executescript(SCHEMA)
