# Contributing to indico-mcp

Thanks for helping out! Bug reports, feature requests, documentation fixes and
code are all welcome.

## Development setup

```bash
uv sync --group dev
uv run pytest
```

The test suite runs offline and needs no Indico token. It mocks HTTP with
[respx](https://lundberg.github.io/respx/) and replaces the Indico client in
tool-level tests (see `tests/test_server.py`). `tests/conftest.py` clears all
`INDICO_*` environment variables for each test, so your local `.env` doesn't
affect results.

Indico instances vary in what they export and which token scopes they accept.
If your change touches API calls, please also try it against a real instance,
and say in the pull request which instance and what you checked.

## Pull requests

- Open a pull request against `main`. Direct pushes to `main` are blocked.
- CI (`.github/workflows/tests.yml`) must pass on Python 3.11–3.13.
- Every pull request needs approval from a code owner (see
  `.github/CODEOWNERS`) before it can be merged. Reviews from other
  contributors are very welcome and help get changes ready, but for now the
  maintainer gives the final approval.
- New pushes after an approval dismiss it, so the approved version is the one
  that gets merged.
- Add or update tests for behaviour you change, and update `README.md` when
  tools, parameters or configuration change.
- Agenda data can be confidential (see "Data Sensitivity & Privacy" in the
  README). Don't put real tokens, private event IDs or restricted content into
  tests, fixtures or issues.

## Credit

Every contributor is listed in the README's Contributors section, using
[all-contributors](https://allcontributors.org). This covers code, docs,
reviews, bug reports, ideas and testing, not only merged code. After your
first contribution lands, a maintainer comments on the pull request or issue:

```
@all-contributors please add @your-username for code, doc
```

The bot then opens a small pull request updating the list. Feel free to ask if
you think you've been missed, or to suggest additional contribution types
([emoji key](https://allcontributors.org/docs/en/emoji-key)).

## Licence

This project is MIT licensed. By submitting a contribution you agree that it is
licensed under the same terms (see [LICENSE](LICENSE)).
