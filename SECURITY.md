# Security Policy

AI Workbench is currently a local-only development project. It is not approved for LAN or public internet exposure.

## Supported boundary

- One user on one trusted Windows workstation.
- Web, API, and PostgreSQL bind to loopback.
- Repository access is read-only and must be explicitly authorized.
- Shell, code execution, Git writes, arbitrary HTTP tools, and user-configured MCP servers are unavailable in the MVP.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that may expose credentials or private source code. Contact the repository owner privately with:

- the affected version or commit;
- a minimal reproduction;
- the expected and observed security boundary;
- any evidence of credential or data exposure, with secrets redacted.

## Secret handling

Never commit `.env` files, provider keys, access tokens, private source samples, database dumps, or captured model payloads. If a secret is committed, remove it from active use and rotate it immediately; deleting the file in a later commit is not sufficient.

## Known unsupported configurations

The current threat model does not cover reverse proxies, public hosting, multiple users, remote agents, untrusted execution, or shared workstations. See [the threat model](docs/security/threat-model.md) before changing the deployment boundary.
