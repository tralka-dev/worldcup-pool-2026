"""
World Cup Pool 2026 — Automated Score Updater
Supports multiple entries per participant.
"""

import os
import json
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timezone
from collections import defaultdict

FOOTBALL_API_KEY  = os.environ["FOOTBALL_API_KEY"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
SPREADSHEET_ID    = os.environ["SPREADSHEET_ID"]

WC2026_ID        = "WC2026"
PICKS_VISIBLE    = os.environ.get("PICKS_VISIBLE", "false").lower() == "true"
TOURNAMENT_START = datetime(2026, 6, 11, tzinfo=timezone.utc)

POINTS = {"GROUP": 1, "R32": 2, "R16": 3, "QF": 4, "SF": 5, "FINAL": 5, "WINNER": 7}

ROUND_MAP = {
    "GROUP_STAGE": "GROUP", "ROUND_OF_32": "R32", "LAST_16": "R16",
    "QUARTER_FINALS": "QF", "SEMI_FINALS": "SF", "FINAL": "FINAL",
}


def fetch_standings_and_results():
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    base = "https://api.football-data.org/v4"
    advanced_teams = set()
    third_place = []

    r = requests.get(f"{base}/competitions/{WC2026_ID}/standings", headers=headers, timeout=10)
    r.raise_for_status()
    for group in r.json().get("standings", []):
        if group.get("type") != "TOTAL":
            continue
        for entry in group["table"]:
            pos, team = entry["position"], entry["team"]["name"]
            if entry.get("playedGames", 0) > 0:
                if pos <= 2:
                    advanced_teams.add(("GROUP", team))
                elif pos == 3:
                    third_place.append((entry.get("points", 0), entry.get("goalDifference", 0), team))

    third_place.sort(reverse=True)
    for _, _, team in third_place[:8]:
        advanced_teams.add(("GROUP", team))

    r = requests.get(f"{base}/competitions/{WC2026_ID}/matches?stage=KNOCKOUT", headers=headers, timeout=10)
    r.raise_for_status()
    for match in r.json().get("matches", []):
        if match.get("status") != "FINISHED":
            continue
        stage = ROUND_MAP.get(match.get("stage", ""), None)
        if not stage:
            continue
        s = match["score"]["fullTime"]
        home, away = match["homeTeam"]["name"], match["awayTeam"]["name"]
        winner = home if (s.get("home") or 0) > (s.get("away") or 0) else away
        loser  = away if winner == home else home
        advanced_teams.add((stage, winner))
        if stage == "FINAL":
            advanced_teams.add(("FINAL", loser))
            advanced_teams.add(("WINNER", winner))

    return advanced_teams


def calculate_scores(picks_rows, advanced_teams):
    """
    Each row is a separate entry. Multiple rows with the same name are
    treated as separate entries (e.g. 'Alex Entry 1', 'Alex Entry 2').
    The Picks sheet should have an Entry Name column (col A) — participants
    can name their entries anything they like.
    """
    entries = []
    name_counts = defaultdict(int)

    for row in picks_rows[1:]:
        if not row or not row[0]:
            continue
        raw_name = row[0].strip()
        if not raw_name:
            continue

        # Auto-number duplicate names: "Alex", "Alex (2)", "Alex (3)"
        name_counts[raw_name] += 1
        count = name_counts[raw_name]
        display_name = raw_name if count == 1 else f"{raw_name} ({count})"

        grp_pts = ko_pts = champ_pts = 0
        col = 1
        for rnd, cnt in [("GROUP",32),("R32",16),("R16",8),("QF",4),("SF",2),("FINAL",2)]:
            for _ in range(cnt):
                if col < len(row) and row[col]:
                    if (rnd, row[col].strip()) in advanced_teams:
                        if rnd == "GROUP":
                            grp_pts += POINTS[rnd]
                        else:
                            ko_pts += POINTS[rnd]
                col += 1
        if col < len(row) and row[col]:
            if ("WINNER", row[col].strip()) in advanced_teams:
                champ_pts += POINTS["WINNER"]

        entries.append({
            "name":     display_name,
            "group":    grp_pts,
            "knockout": ko_pts,
            "champion": champ_pts,
            "total":    grp_pts + ko_pts + champ_pts
        })

    # Sort by total descending
    entries.sort(key=lambda x: x["total"], reverse=True)
    return entries


def update_sheet(service, entries, picks_visible):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if picks_visible:
        header = ["Rank", "Entry Name", "Group Pts", "Knockout Pts", "Champion Pts", "Total", "Last Updated"]
        rows = [[i+1, e["name"], e["group"], e["knockout"], e["champion"], e["total"], now if i==0 else ""]
                for i, e in enumerate(entries)]
        data = [header] + rows
    else:
        header = ["Rank", "Entry Name", "Total Points", "Note", "Last Updated"]
        rows = [[i+1, e["name"], "—", "Picks revealed when tournament starts", now if i==0 else ""]
                for i, e in enumerate(entries)]
        data = [
            ["⚽ WORLD CUP POOL 2026 — Standings"],
            ["Picks are hidden until the tournament kicks off on June 11, 2026."],
            [""],
            header
        ] + rows

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range="Leaderboard!A1",
        valueInputOption="RAW",
        body={"values": data}
    ).execute()
    status = "PICKS HIDDEN" if not picks_visible else "PICKS VISIBLE"
    print(f"[✓] Leaderboard updated — {len(entries)} entries — {status} — {now}")


def main():
    now_utc = datetime.now(timezone.utc)
    print(f"[→] Running at {now_utc.isoformat()}")

    picks_visible = PICKS_VISIBLE or (now_utc >= TOURNAMENT_START)
    print(f"[→] Picks visible: {picks_visible}")

    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)

    picks_rows = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range="Picks!A1:BL500"
    ).execute().get("values", [])
    print(f"[→] {len(picks_rows)-1} entries loaded")

    advanced = fetch_standings_and_results()
    print(f"[→] {len(advanced)} advancement records fetched")

    entries = calculate_scores(picks_rows, advanced)
    update_sheet(service, entries, picks_visible)
    print("[✓] Done!")


if __name__ == "__main__":
    main()
