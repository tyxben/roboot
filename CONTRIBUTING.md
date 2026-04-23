_[中文版](CONTRIBUTING.zh.md)_

# Contributing to Roboot

Thanks for looking. Roboot is a personal AI agent hub; maintainer bandwidth is one human, so this guide is short and honest about what gets in.

## Scope

**In scope:** bug fixes, security hardening, new Arcana tools that fit the "personal agent on my Mac" shape, new I/O adapters (voice / chat platforms), docs.

**Out of scope by default:** multi-tenant features, plugin frameworks, cloud-hosted deployment modes, alternative agent frameworks, Windows support. Not forbidden — just pitch in an issue first before spending a weekend on them.

If you're not sure, open a lightweight issue and ask. Much cheaper than a rejected PR.

## Getting set up

```bash
git clone git@github.com:tyxben/roboot.git
cd roboot
./scripts/setup.sh            # installs deps, ffmpeg, prewarms Whisper
cp config.example.yaml config.yaml
# fill in at least one LLM provider API key
python server.py              # http://localhost:8765
```

See [docs/USAGE.md](docs/USAGE.md) for what each surface does.

## Running tests

```bash
python -m pytest tests/        # unit tests, ~6s, no network
cd relay && npm run typecheck  # TypeScript for the Cloudflare Worker + pair-page
```

Before a release, also walk [docs/TESTING.md](docs/TESTING.md) — the checklist that covers everything the automated suite can't reach (phone pairing, JARVIS voice, iTerm2 bridge).

## Submitting a PR

1. Fork, branch off `main`, keep the branch focused on one concern.
2. Add tests when the change is testable. New pure-Python modules get a `tests/test_<module>.py`; adapter-level changes get a note in `docs/TESTING.md`.
3. Run `python -m pytest tests/` and `npm run typecheck` before pushing.
4. Commit style: imperative, sentence-case title, body explains *why* and references the motivating bug/issue. Examples from the log:
   - `Harden LAN API auth, self-upgrade, and cert bootstrap`
   - `Refactor STT into pluggable backend package`
   - `Fix CI: restrict setuptools package discovery`
5. Open the PR against `main`. Fill in the template (summary + test plan).
6. CI runs pytest on push. Review latency is best-effort — if it's been a week, ping the PR.

No CLA. No sign-off required. No squash vs. merge preference — the maintainer will pick when merging.

## Changing `soul.md`

Don't commit experimental soul edits. `soul.md` is the live identity file; end users' local copies already diverge. If you're changing the *schema* (what sections exist, how they're parsed), update `tools/soul.py` and add a pytest case. If you're changing the default personality, flag it loudly in the PR — it changes everyone's agent behavior on their next pull.

## Security issues

**Don't open a public issue for vulnerabilities.** See [SECURITY.md](SECURITY.md) for the private disclosure path. Anything that could expose a user's machine, API keys, chat history, or relay traffic counts.

## License

By contributing you agree your changes ship under the project's [MIT license](LICENSE).
