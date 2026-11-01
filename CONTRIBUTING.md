# Contributing to molo

Thank you for your interest in improving molo! Contributions of all kinds are
welcome: bug reports, documentation, translations and code.

## Reporting bugs and proposing features

Please open an issue and describe

- what you expected and what happened instead,
- steps to reproduce, including relevant configuration (never include secrets),
- version or commit, Python/PostgreSQL version.

Please report security issues privately to [kontakt@molo.news](mailto:kontakt@molo.news)
or via GitHub's private vulnerability reporting instead of opening an issue.

## Development setup

Follow [docs/installation.md](docs/installation.md). The AI features need a local
[Ollama](https://ollama.com) server with `gpt-oss:20b`; everything else works
without it.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep changes focused; one topic per pull request.
3. Run the checks before you push:
   ```bash
   ruff check .
   python manage.py check
   python manage.py makemigrations --check --dry-run content
   python manage.py test
   ```
   [ruff](https://docs.astral.sh/ruff/) is configured in `ruff.toml`
   (`pip install ruff`).
4. Include migrations for model changes.
5. Describe what changed and why in the pull request.

## Dependencies

`requirements.in` lists the direct dependencies with allowed version ranges.
`requirements.txt` is a lock file generated from it with
[uv](https://docs.astral.sh/uv/) and pins every package, including transitive
ones. Do not edit it by hand.

```bash
# after changing requirements.in
uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt
# update all packages to the newest allowed versions
uv pip compile requirements.in --universal --python-version 3.12 -o requirements.txt --upgrade
```

Commit both files, run the tests with the new versions, and check for known
vulnerabilities with `pip-audit -r requirements.txt`.

## Guidelines

- **No secrets, personal data or instance-specific values in the code.** Everything
  that differs between installations belongs in `.env` / settings (see
  [docs/configuration.md](docs/configuration.md)). Do not commit database dumps,
  exports, logs or scraped pages.
- **AI only through `molo/llm.py`.** New AI features must use the local model client
  and must work with open-weight models. Do not add dependencies on proprietary
  cloud AI services.
- **Generic over specific.** Code for a single customer, city or source belongs in a
  configuration, a generated import script or a separate plugin, not in the core.
- Code comments and user-facing texts may be German or English; keep them
  consistent within a file. Translations live in `locale/`; update them with
  `python manage.py makemessages -l de` and `python manage.py compilemessages`.

## License

By contributing, you agree that your contributions are licensed under the
[AGPL-3.0](LICENSE).
