# Security Policy

This application processes clinical documents containing sensitive personal data.
Security reports are taken seriously and handled with priority.

## Supported Versions

Only the latest version on the `main` branch is supported. Please update before
reporting an issue.

## Reporting a Vulnerability

- **Preferred**: use GitHub's private vulnerability reporting on this repository
  (*Security → Report a vulnerability*), if enabled.
- **Otherwise**: email **poltorprogrammer@gmail.com** with subject `[SECURITY]`.

Please include steps to reproduce, the affected file/component, and the potential
impact. You should receive a response within a few days.

## Rules for reports and issues

- **Never** include real patient data, clinical documents, or fragments of them
  in an issue, report, screenshot or log excerpt. Use synthetic data.
- **Never** include Google Cloud service account keys, project IDs of production
  environments, or `config.json` contents.

## Security design of this app (summary)

- Documents are processed transiently in memory; nothing is stored in the cloud.
- DLP inspection is pinned to the region configured in `config.json`
  (default `europe-west6`); OCR uses the matching regional Vision endpoint.
- The optional translation feature sends **already-redacted** copies to Google
  Translation in `us-central1` (US) and is clearly disclosed in the UI.
- The UI (pywebview) opens no network ports and enforces a strict CSP; only the
  Python backend talks to Google APIs.
- Service account keys are stored outside the app folder in a per-user directory
  with restricted permissions.
- Outputs are validated (page counts included) and written atomically; an audit
  trail records hashes and settings, never content or keywords.
