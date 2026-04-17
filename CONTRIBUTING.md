# Contributing to Watch with Fox

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

See [README.md](README.md#getting-started) for full setup instructions. In short:

```bash
cd server && uv sync && uv run python src/podcast_commentary/agent/main.py download-files
cd ../web && npm install
```

Then run three terminals: API server, agent, and web app.

## Making Changes

1. Fork the repo and create a branch from `main`.
2. Make your changes. Follow the existing code style:
   - **Python:** Ruff, 100-char line length, full type annotations (Python 3.11+ `X | Y` syntax).
   - **TypeScript:** ESLint with Next.js config, strict mode, `@/` imports.
3. Test your changes locally (all three services running).
4. Open a pull request against `main`.

## Pull Requests

- Keep PRs focused — one feature or fix per PR.
- Describe what changed and why in the PR description.
- Include steps to test if applicable.

## Reporting Bugs

Open a [GitHub issue](https://github.com/lemonsliceai/watch-with-fox/issues) with:

- Steps to reproduce
- Expected vs. actual behavior
- Browser/OS and relevant environment details

## Security

To report a security vulnerability, see [SECURITY.md](SECURITY.md).
