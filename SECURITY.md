# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for security problems. Use GitHub's
private reporting instead: **Security → Report a vulnerability** on this
repository. You'll get a response as soon as possible, and fixes are
released as a new tagged version.

## Verifying a download

Every release is built from this repository's source by GitHub Actions
(`.github/workflows/build.yml`), self-tested, and published with a
`SHA256SUMS.txt` file. To verify a download on Windows:

```powershell
Get-FileHash .\SDS-Reader.exe -Algorithm SHA256
```

The hash must match the line for that file in `SHA256SUMS.txt` on the
release page. Only download SDS Reader from this repository's Releases page
or its official website.

## What the app does and doesn't do

- Makes **no network connections** (the optional, off-by-default "local
  Ollama AI check" talks only to `localhost`).
- Only **reads** the PDFs and template you choose; never modifies or deletes
  them. Output files are never overwritten - a numbered name is used instead.
- Accepts only `.docx`, `.txt` and `.md` templates (macro-enabled Office
  formats are refused) and rejects files that aren't real PDFs or are over
  100 MB.
- Stores only its own preferences, in `%APPDATA%\SDS Reader\settings.json`.
