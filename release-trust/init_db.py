import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS release_trust_runs (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || substr(lower(hex(randomblob(2))),2) || '-' || substr('89ab',abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6)))),
    client_id TEXT NOT NULL,
    application TEXT NOT NULL,
    release_id TEXT NOT NULL,
    commit_sha TEXT,
    image_digest TEXT,
    evidence_s3_prefix TEXT,
    status TEXT NOT NULL DEFAULT 'not_started',
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (client_id, application, release_id)
);

CREATE TABLE IF NOT EXISTS release_trust_evidence (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    release_run_id TEXT NOT NULL REFERENCES release_trust_runs(id),
    client_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    status TEXT NOT NULL,
    object_key TEXT,
    sha256 TEXT,
    schema_version TEXT,
    summary_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS release_trust_evaluations (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    release_run_id TEXT NOT NULL REFERENCES release_trust_runs(id),
    client_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    policy_version TEXT,
    manifest_digest TEXT,
    decision TEXT NOT NULL,
    evaluated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS release_trust_rule_results (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    evaluation_id TEXT NOT NULL REFERENCES release_trust_evaluations(id),
    client_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    result TEXT NOT NULL,
    severity TEXT,
    evidence_ref TEXT,
    message TEXT,
    remediation TEXT,
    exception_eligible INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS release_trust_exceptions (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    release_run_id TEXT NOT NULL REFERENCES release_trust_runs(id),
    client_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    release_scope TEXT,
    environment_scope TEXT,
    reason TEXT NOT NULL,
    compensating_control TEXT,
    requester TEXT NOT NULL,
    approver TEXT,
    issue_reference TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'requested',
    revocation_history TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS release_trust_promotions (
    id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    release_run_id TEXT NOT NULL REFERENCES release_trust_runs(id),
    client_id TEXT NOT NULL,
    source_environment TEXT,
    target_environment TEXT NOT NULL,
    deployed_digest TEXT NOT NULL,
    approved_digest TEXT,
    result TEXT NOT NULL,
    promoted_at TEXT NOT NULL DEFAULT (datetime('now')),
    promoted_by TEXT
);
"""

async def init():
    engine = create_async_engine("sqlite+aiosqlite:///./release_trust.db", echo=True)
    async with engine.begin() as conn:
        for statement in CREATE_TABLES.strip().split(";"):
            s = statement.strip()
            if s:
                await conn.execute(text(s))
    print("Tables created.")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(init())