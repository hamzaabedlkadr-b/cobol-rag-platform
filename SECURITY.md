# Security policy

## Supported use

The included API and UI are development services intended to run on the local
machine. They do not provide authentication or authorization. Docker Compose
therefore binds port 8000 to `127.0.0.1` by default.

Do not expose the API directly to the internet. For a shared deployment, place
an authenticated, TLS-enabled reverse proxy in front of it and restrict access
to trusted users.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting or security-advisory
feature for this repository. Do not publish credentials, proprietary COBOL
source, copybooks, generated evidence, or exploit details in a public issue.
