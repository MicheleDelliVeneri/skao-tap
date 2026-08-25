# Security policy

## Supported versions

This project is pre-release: only the `main` branch and the container images
published from it (`ghcr.io/ska-telescope/egernia/*:latest`) receive
security fixes. There are no maintained release branches yet.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's private vulnerability
reporting: open
<https://github.com/ska-telescope/egernia/security/advisories/new>
("Security" tab → "Report a vulnerability"). That channel keeps the report
non-public until a fix ships.

Please do not open a public issue or pull request for a suspected
vulnerability, and do not test against a deployment you do not operate.

Useful detail in a report:

- affected component (`tap-api`, `tap-executor`, `tap-db`, the Helm chart) and
  the commit or image digest you saw it on;
- the request or ADQL query that triggers it, and what an attacker gains
  (unauthenticated write, cross-user job access, SQL reaching the server
  unsanitised, resource exhaustion);
- whether authentication was enabled (`auth.enabled`) and, if so, which plugin.

Expect an acknowledgement within 5 working days and a status update at least
every 10 working days until the report is closed.

## Scope

In scope: the TAP service and its jobs/metadata endpoints, the ADQL
translation layer, the authentication plugins and role mapping, the container
images, and the Helm chart's default configuration.

Out of scope: findings that require cluster-admin or database-superuser access
the attacker should not have in the first place, denial of service from a
deliberately unlimited query budget, and vulnerabilities in third-party
dependencies with no exploitable path through this code (report those
upstream — `pip-audit`, Trivy and Dependabot already track them here).

## What this project does to keep the supply chain honest

- every GitHub Action and container base image is pinned by digest or commit
  SHA, and Dependabot moves those pins;
- CI publishes an SPDX SBOM and a signed build-provenance attestation for each
  image;
- CodeQL, Trivy, zizmor, pip-audit and OpenSSF Scorecard run on every push to
  `main` and on pull requests.
