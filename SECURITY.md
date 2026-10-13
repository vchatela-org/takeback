# Security Policy

## Supported versions

Only the latest `main` and the most recent tagged release receive fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately via GitHub's [private vulnerability reporting](https://github.com/vchatela-org/takeback/security/advisories/new).

Please include the affected version or commit, reproduction steps, and the impact you believe it has. Expect an acknowledgement within 7 days and an assessment within 30 days.

## Scope

Takeback has no authentication of its own. It is meant to run locally, or behind an auth proxy — see "Running it" in the README. A deployment that exposes it directly to a network should be treated as a misconfiguration rather than a vulnerability, though a report is welcome if the README makes that unclear.

An uploaded archive is untrusted input: extraction, file naming and the JSON/CSV/HTML parsers are all written to treat it that way. Anything that gets an uploaded file to escape the data directory, exhaust memory or disk, or execute code is in scope.

## Handling of secrets

Takeback stores no credentials: it reads archives you provide and, optionally, writes numbers to InfluxDB. The one secret-bearing setting is `TAKEBACK_INFLUXDB_URL`, which may carry credentials in its userinfo — it is read from the environment and is never written to disk or into a rendered page.

If you believe a secret has been committed here, report it privately using the link above rather than opening an issue.

## Automated security tooling

- **CodeQL** code scanning (`python` and `actions`, `security-extended` queries) on every push and PR, plus a weekly scan
- **Secret scanning** with **push protection**
- **Dependabot** alerts, security updates, and weekly version updates for pip, Docker and GitHub Actions
- **Dependency review** on every pull request
- **Privacy deny-list** scan, working tree and full history, on every push and PR (`scripts/privacy-scan.sh`)
