-- All tables include client_id for tenant isolation
-- Every query MUST include WHERE client_id = :client_id

CREATE TABLE release_trust_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id VARCHAR(255) NOT NULL,
    application VARCHAR(255) NOT NULL,
    release_id VARCHAR(255) NOT NULL,
    commit_sha VARCHAR(64),
    image_digest VARCHAR(100),
    evidence_s3_prefix VARCHAR(500),
    status VARCHAR(50) NOT NULL DEFAULT 'not_started',
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_release_runs UNIQUE (client_id, application, release_id)
);

CREATE TABLE release_trust_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    release_run_id UUID NOT NULL REFERENCES release_trust_runs(id),
    client_id VARCHAR(255) NOT NULL,
    evidence_type VARCHAR(100) NOT NULL,
    status VARCHAR(50) NOT NULL,
    object_key VARCHAR(500),
    sha256 VARCHAR(100),
    schema_version VARCHAR(50),
    summary_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE release_trust_evaluations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    release_run_id UUID NOT NULL REFERENCES release_trust_runs(id),
    client_id VARCHAR(255) NOT NULL,
    environment VARCHAR(50) NOT NULL,
    policy_version VARCHAR(50),
    manifest_digest VARCHAR(100),
    decision VARCHAR(20) NOT NULL,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE release_trust_rule_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    evaluation_id UUID NOT NULL REFERENCES release_trust_evaluations(id),
    client_id VARCHAR(255) NOT NULL,
    rule_id VARCHAR(100) NOT NULL,
    result VARCHAR(20) NOT NULL,
    severity VARCHAR(20),
    evidence_ref VARCHAR(500),
    message TEXT,
    remediation TEXT,
    exception_eligible BOOLEAN DEFAULT TRUE
);

CREATE TABLE release_trust_exceptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    release_run_id UUID NOT NULL REFERENCES release_trust_runs(id),
    client_id VARCHAR(255) NOT NULL,
    rule_id VARCHAR(100) NOT NULL,
    release_scope VARCHAR(255),
    environment_scope VARCHAR(50),
    reason TEXT NOT NULL,
    compensating_control TEXT,
    requester VARCHAR(255) NOT NULL,
    approver VARCHAR(255),
    issue_reference VARCHAR(255),
    expires_at TIMESTAMPTZ,
    status VARCHAR(30) NOT NULL DEFAULT 'requested',
    revocation_history JSONB DEFAULT '[]'
);

CREATE TABLE release_trust_promotions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    release_run_id UUID NOT NULL REFERENCES release_trust_runs(id),
    client_id VARCHAR(255) NOT NULL,
    source_environment VARCHAR(50),
    target_environment VARCHAR(50) NOT NULL,
    deployed_digest VARCHAR(100) NOT NULL,
    approved_digest VARCHAR(100),
    result VARCHAR(20) NOT NULL,
    promoted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_by VARCHAR(255)
);

-- Indexes for tenant-scoped queries
CREATE INDEX idx_rt_runs_client ON release_trust_runs(client_id);
CREATE INDEX idx_rt_runs_client_app ON release_trust_runs(client_id, application);
CREATE INDEX idx_rt_runs_client_release ON release_trust_runs(client_id, release_id);
CREATE INDEX idx_rt_evidence_run ON release_trust_evidence(release_run_id, client_id);
CREATE INDEX idx_rt_evaluations_run ON release_trust_evaluations(release_run_id, client_id);
CREATE INDEX idx_rt_exceptions_run ON release_trust_exceptions(release_run_id, client_id);
CREATE INDEX idx_rt_promotions_run ON release_trust_promotions(release_run_id, client_id);
