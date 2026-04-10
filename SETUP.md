# World Cup Pool 2026 — Setup Guide

## Overview
This repo automatically fetches World Cup results every hour and updates
your Google Sheets leaderboard. Zero manual work during the tournament.

---

## Step 1 — Fork this repo on GitHub
1. Go to github.com and create a free account if you don't have one
2. Click the "+" → "New repository" → name it `worldcup-pool-2026`
3. Upload all files from this folder to the repo

---

## Step 2 — Create your Google Sheet
1. Go to sheets.google.com → create a new blank sheet
2. Name it **World Cup Pool 2026**
3. Create these tabs (sheets):
   - `Picks`  — paste participant picks here (see Picks format below)
   - `Leaderboard` — script writes here automatically
4. Copy the Sheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/[THIS_PART]/edit`

---

## Step 3 — Get a football-data.org API key
1. Go to https://www.football-data.org/client/register
2. Sign up for free (takes 1 minute)
3. You'll receive an API key by email — save it

---

## Step 4 — Set up Google Service Account
1. Go to https://console.cloud.google.com
2. Create a new project (name: "worldcup-pool")
3. Enable "Google Sheets API"
4. Go to IAM → Service Accounts → Create Service Account
5. Name it "worldcup-updater" → Done
6. Click the account → Keys → Add Key → JSON → Download
7. Open the JSON file — you'll paste its entire contents as a GitHub secret
8. Share your Google Sheet with the service account email
   (looks like: worldcup-updater@worldcup-pool.iam.gserviceaccount.com)
   Give it "Editor" access

---

## Step 5 — Add GitHub Secrets
In your GitHub repo → Settings → Secrets and variables → Actions → New secret:

| Secret name        | Value                                    |
|--------------------|------------------------------------------|
| FOOTBALL_API_KEY   | your football-data.org API key           |
| GOOGLE_CREDS_JSON  | entire contents of the service account JSON file |
| SPREADSHEET_ID     | your Google Sheet ID from Step 2         |

---

## Step 6 — Update competition ID (June 2026)
When the tournament approaches, find the 2026 World Cup ID:
1. Call: https://api.football-data.org/v4/competitions (with your API key)
2. Find "FIFA World Cup 2026" and note its code
3. Update `WC2026_ID` in `update_scores.py` with the correct code

---

## Picks sheet format
The `Picks` tab must have this column structure (row 1 = headers):

| Col | Content |
|-----|---------|
| A | Participant Name |
| B-AH (cols 2-33) | 32 group stage picks (team names, 1 per cell) |
| AI-AX (cols 35-50) | 16 Round of 32 picks |
| AY-BF (cols 51-58) | 8 Round of 16 picks |
| BG-BJ (cols 59-62) | 4 Quarter-final picks |
| BK-BL (cols 63-64) | 2 Semi-final picks |
| BM-BN (cols 65-66) | 2 Final picks |
| BO (col 67)        | Champion pick |

Team names must match exactly what football-data.org returns.
Use the Teams sheet in the Excel file for the canonical name list.

---

## How to collect picks (Google Form)
1. Create a Google Form with one question per pick slot
2. In Form → Responses → Link to Google Sheets → your existing sheet
3. Set the response destination to the "Picks" tab
4. Set a deadline via Form settings → "Accept responses until [date]"
5. Share the form link with all participants

---

## Scoring
| Round                    | Points per correct pick | Max |
|--------------------------|------------------------|-----|
| Group stage → R32        | 1 pt                   | 32  |
| Round of 32 → R16        | 2 pts                  | 32  |
| Round of 16 → QF         | 3 pts                  | 24  |
| Quarter-finals → SF      | 4 pts                  | 16  |
| Semi-finals → Final      | 5 pts                  | 10  |
| Champion                 | 7 pts                  |  7  |
| **Maximum total**        |                        |**121**|

---

## Testing before the tournament
Run manually from your repo: Actions tab → "Update World Cup Scores" → Run workflow
This lets you verify everything is connected before June 2026.
