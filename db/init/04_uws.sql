-- UWS job persistence shared by the tap-api frontend (job CRUD over HTTP)
-- and the tap-executor worker (query execution).
CREATE SCHEMA IF NOT EXISTS uws;

CREATE TABLE uws.jobs (
    job_id              text PRIMARY KEY,
    phase               text NOT NULL DEFAULT 'PENDING',
    run_id              text,
    owner_id            text,
    quote               timestamptz,
    creation_time       timestamptz NOT NULL DEFAULT now(),
    start_time          timestamptz,
    end_time            timestamptz,
    execution_duration  integer NOT NULL DEFAULT 600,      -- seconds, 0 = unlimited
    destruction         timestamptz,
    parameters          jsonb NOT NULL DEFAULT '{}'::jsonb, -- uppercased TAP params
    query_sql           text,                               -- translated PostgreSQL
    error_type          text,                               -- 'transient' | 'fatal'
    error_message       text,
    result_mime         text,
    result_size         bigint
);

CREATE INDEX jobs_phase_creation ON uws.jobs (phase, creation_time);
CREATE INDEX jobs_destruction ON uws.jobs (destruction);
