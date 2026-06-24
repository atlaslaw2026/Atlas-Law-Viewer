# Contributing

Thanks for your interest in improving Atlas Law Viewer.

## Setup
1. Create and activate a virtual environment.
2. Install runtime and dev dependencies:
   - `pip install -r requirements.txt`
   - `pip install -r requirements-dev.txt`

## Quality Checks
Run these before opening a PR:
- `ruff check .`
- `black --check .`
- `pytest`

## Commit Style
Use concise, descriptive commit messages. Example:
- `Fix Supreme authority extraction fallback`
- `Add CI for lint and test checks`

## Pull Requests
- Keep changes scoped to one concern when possible.
- Include a short summary of behavior changes.
- Add or update tests when changing logic.
