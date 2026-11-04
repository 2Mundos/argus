-- A project's repository, the authoritative source workers resolve at claim
-- (worker launch spec). Null: unknown; the pools' explicit allowlist still applies.
ALTER TABLE projects ADD COLUMN repository_url TEXT;

-- Backfill from pool allowlists using the same repository *identity* as runtime
-- (parse_repository / pool_repository): lowercase host and path, optional .git suffix
-- stripped, HTTPS without credentials/query/fragment. Invalid legacy strings are
-- skipped; disagreeing identities stay null.
CREATE TEMP TABLE _0071_repo_rows AS
SELECT
    s3.slug AS slug,
    lower(s3.host) || '/' || lower(s3.owner) || '/' || lower(s3.repo_name) AS repo_key
FROM (
    SELECT
        s2.slug AS slug,
        s2.host AS host,
        s2.owner AS owner,
        CASE
            WHEN lower(s2.reporaw) LIKE '%.git'
            THEN substr(s2.reporaw, 1, length(s2.reporaw) - 4)
            ELSE s2.reporaw
        END AS repo_name
    FROM (
        SELECT
            s1.slug AS slug,
            substr(s1.rest, 1, s1.p0 - 1) AS host,
            substr(s1.tail, 1, s1.p1 - 1) AS owner,
            substr(s1.tail, s1.p1 + 1) AS reporaw
        FROM (
            SELECT
                s0.slug AS slug,
                s0.rest AS rest,
                s0.p0 AS p0,
                substr(s0.rest, s0.p0 + 1) AS tail,
                instr(substr(s0.rest, s0.p0 + 1), '/') AS p1
            FROM (
                SELECT
                    pr.slug AS slug,
                    substr(j.value, 9) AS rest,
                    instr(substr(j.value, 9), '/') AS p0
                FROM projects pr
                JOIN pools p
                JOIN json_each(p.config, '$.nanoclaw.repositories') j ON (
                    j.key = pr.slug AND j.type = 'text'
                )
                WHERE pr.slug <> 'global'
                    AND j.value LIKE 'https://%'
                    AND j.value NOT LIKE '%@%'
                    AND instr(j.value, '?') = 0
                    AND instr(j.value, '#') = 0
                    AND instr(substr(j.value, 9), '/') > 0
            ) s0
        ) s1
        WHERE s1.p0 > 0
            AND s1.p1 > 0
            AND length(substr(s1.rest, 1, s1.p0 - 1)) > 0
            AND length(substr(s1.tail, 1, s1.p1 - 1)) > 0
            AND length(substr(s1.tail, s1.p1 + 1)) > 0
            AND instr(substr(s1.tail, s1.p1 + 1), '/') = 0
    ) s2
) s3;

UPDATE projects
SET repository_url = (
    SELECT 'https://' || g.repo_key
    FROM (
        SELECT slug, MIN(repo_key) AS repo_key
        FROM _0071_repo_rows
        GROUP BY slug
        HAVING COUNT(DISTINCT repo_key) = 1
    ) g
    WHERE g.slug = projects.slug
)
WHERE slug <> 'global';

DROP TABLE _0071_repo_rows;
