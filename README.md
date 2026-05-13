# Paper Radar

Paper Radar is a GitHub-hosted paper watcher. It searches papers by topic, avoids sending duplicates, temporarily downloads PDFs for email delivery, deletes PDFs after each run, and keeps only configuration, sent-paper records, state, and Markdown reports in the repository.

## What It Includes

- Static web UI in `app/web/`
- GitHub Actions workflow in `.github/workflows/paper-radar.yml`
- Python worker in `app/worker/paper_radar.py`
- Editable configuration in `data/config.json`
- Sent-paper history in `data/papers.json`
- Run state in `data/state.json`
- Saved reports in `reports/`

## Setup

1. Create a GitHub repository and upload this folder.
2. In repository settings, enable GitHub Pages and set the source to GitHub Actions. The `Publish Web UI` workflow deploys `app/web` automatically.
3. In repository settings, add secrets for email delivery.

For Resend:

- `RESEND_API_KEY`
- `MAIL_FROM`

For SMTP:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `MAIL_FROM`

4. In GitHub Actions settings, allow workflows to read and write repository contents.
5. The `Paper Radar` workflow runs hourly and the worker decides whether the configured schedule is due. You can also run it manually from the GitHub Actions page or from the web UI.

## Web UI

Open the web UI and enter:

- GitHub owner
- Repository name
- Branch
- Personal access token with repository content write access

The token is stored only in your browser local storage. The UI uses it to load/save `data/config.json` and trigger the GitHub Action manually.

## Notes

- PDFs are downloaded only into a temporary folder during a run.
- PDFs are deleted automatically when the run ends.
- The repository keeps `data/papers.json` to prevent repeat downloads and repeat emails.
- Each topic has `max_downloads_per_run`, adjustable from the web UI.
- The default search sources are arXiv and OpenAlex.
