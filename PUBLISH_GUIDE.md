# Publish Guide (GitHub)

## 1) Initialize Repo
Run in this folder:

```powershell
git init
git add .
git commit -m "Initial portfolio release"
```

## 2) Create Remote
Create an empty GitHub repository, then connect it:

```powershell
git branch -M main
git remote add origin <YOUR_GITHUB_REPO_URL>
git push -u origin main
```

## 3) Add Portfolio Signals
Before sharing, update:
- README screenshots section
- Short demo video link
- License file (MIT recommended)

## 4) Optional: Keep Data In Sync
To regenerate local datasets:

```powershell
powershell -ExecutionPolicy Bypass -File .\atlas_daily_refresh.ps1
```

## 5) Resume Bullet Starter
Built a local-first legal research platform that ingests and serves multi-court federal opinions with custom parsing, secure local APIs, and searchable court-specific UIs.
