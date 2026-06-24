# Publishing Atlas Law Viewer to GitHub

## 1) Run the release checks

```powershell
pip install -r requirements.txt
pip install -r requirements-dev.txt
ruff check .
black --check .
pytest
```

## 2) Review what will be published

The local database, PDF corpus, virtual environments, bundled runtime, logs, installer
builds, secrets, and Windows shortcuts are excluded by `.gitignore`.

```powershell
git status --short
git diff --check
git diff
```

## 3) Commit the release

```powershell
git add .
git commit -m "Prepare Atlas Law Viewer for publication"
```

## 4) Connect and publish

Create an empty GitHub repository without generated starter files, then run:

```powershell
git branch -M main
git remote add origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```

If `origin` already exists, verify it with `git remote -v` instead of adding it again.

## 5) Verify GitHub

- Confirm the CI workflow passes on `main`.
- Confirm the README screenshots render correctly.
- Confirm no local-only files appear in the repository.
- Add a repository description, topics, and optional demo video link.

## 6) Optional: Keep Data In Sync

To regenerate local datasets:

```powershell
powershell -ExecutionPolicy Bypass -File .\atlas_daily_refresh.ps1
```

## Portfolio bullet starter

Built a local-first legal research tool that ingests and serves multi-court federal opinions with custom parsing, a secure local server, and searchable court-specific UIs.
