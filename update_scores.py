"""
World Cup Pool 2026 — Automated Score Updater
Supports multiple entries per participant.

Fixes:
- Skips Timestamp column (col A), Entry Name is col B, picks start col C
- Correct round structure: GROUP(24) + 3RD(8) + R16(8) + QF(4) + SF(2) + FINAL(2) + CHAMPION(1)
- Duplicate rule: if same team appears more than once in R16 onwards, points awarded only once
- Email column included in Leaderboard output
- Correct point values: GROUP=1, 3RD=1, R16=2, QF=3, SF=4, FINAL=5, CHAMPION=7
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

WC2026_ID        = os.environ.get("WC2026_ID", "WC")  # Update after finding correct ID
PICKS_VISIBLE    = os.environ.get("PICKS_VISIBLE", "false").lower() == "true"
TOURNAMENT_START = datetime(2026, 6, 11, tzinfo=timezone.utc)

POINTS = {
    "GROUP":  1,
    "3RD":    1,
    "R32":    2,
    "R16":    3,
    "QF":     4,
    "SF":     5,
    "FINAL":  6,
    "WINNER": 7,
}

ROUND_MAP = {
    "GROUP_STAGE":    "GROUP",
    "ROUND_OF_32":    "R32",
    "LAST_16":        "R16",
    "QUARTER_FINALS": "QF",
    "SEMI_FINALS":    "SF",
    "FINAL":          "FINAL",
}

# Picks sheet column layout (0-indexed):
# Col 0     (A)      = Timestamp
# Col 1     (B)      = Entry Name
# Col 2-25  (C-Z)    = Group stage picks (24 cols, Groups A-L, 1st and 2nd pick each)
# Col 26-33 (AA-AH)  = 3rd Place picks (8 cols)
# Col 34-49 (AI-AX)  = Round of 32 picks (16 cols)
# Col 50-57 (AY-BF)  = Round of 16 picks (8 cols)
# Col 58-61 (BG-BJ)  = Quarter-final picks (4 cols)
# Col 62-63 (BK-BL)  = Semi-final picks (2 cols)
# Col 64-65 (BM-BN)  = Final picks (2 cols)
# Col 66    (BO)     = Champion pick (1 col)
# Col 67    (BP)     = Email Address

ROUND_COLS = [
    ("GROUP", 2,  25),   # cols 2-25  = 24 group picks (C-Z)
    ("3RD",   26, 33),   # cols 26-33 = 8 third place picks (AA-AH)
    ("R32",   34, 49),   # cols 34-49 = 16 round of 32 picks (AI-AX)
    ("R16",   50, 57),   # cols 50-57 = 8 round of 16 picks (AY-BF)
    ("QF",    58, 61),   # cols 58-61 = 4 quarter-final picks (BG-BJ)
    ("SF",    62, 63),   # cols 62-63 = 2 semi-final picks (BK-BL)
    ("FINAL", 64, 65),   # cols 64-65 = 2 final picks (BM-BN)
]
CHAMPION_COL = 66  # Column BO
EMAIL_COL    = 67  # Column BP


def fetch_standings_and_results():
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    base = "https://api.football-data.org/v4"
    advanced_teams = set()
    third_place = []

    # Fetch group stage standings
    try:
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
                        third_place.append((
                            entry.get("points", 0),
                            entry.get("goalDifference", 0),
                            team
                        ))
    except Exception as e:
        print(f"[!] Could not fetch standings: {e}")

    # Best 8 third-place teams advance
    third_place.sort(reverse=True)
    for _, _, team in third_place[:8]:
        advanced_teams.add(("3RD", team))

    # Fetch knockout results
    try:
        r = requests.get(f"{base}/competitions/{WC2026_ID}/matches?stage=KNOCKOUT", headers=headers, timeout=10)
        r.raise_for_status()
        for match in r.json().get("matches", []):
            if match.get("status") != "FINISHED":
                continue
            stage = ROUND_MAP.get(match.get("stage", ""), None)
            if not stage:
                continue
            s = match["score"]["fullTime"]
            home = match["homeTeam"]["name"]
            away = match["awayTeam"]["name"]
            home_score = s.get("home") or 0
            away_score = s.get("away") or 0
            winner = home if home_score > away_score else away
            loser  = away if winner == home else home
            advanced_teams.add((stage, winner))
            if stage == "FINAL":
                advanced_teams.add(("FINAL", loser))   # Both finalists get FINAL points
                advanced_teams.add(("WINNER", winner))  # Champion gets bonus
            # For R32: both teams that played get R32 credit (they made it out of groups)
            # Winner advances to R16
            if stage == "R32":
                advanced_teams.add(("R32", loser))  # Loser still made it to R32
    except Exception as e:
        print(f"[!] Could not fetch knockout results: {e}")

    return advanced_teams


def get_cell(row, col):
    """Safely get a cell value from a row."""
    if col < len(row) and row[col]:
        return row[col].strip()
    return None


def calculate_scores(picks_rows, advanced_teams):
    entries = []
    name_counts = defaultdict(int)

    for row in picks_rows[1:]:  # Skip header row
        if not row or len(row) < 2 or not row[1]:
            continue

        raw_name = row[1].strip()  # Col B = Entry Name
        if not raw_name:
            continue

        email = get_cell(row, EMAIL_COL) or ""  # Col BP = Email Address

        # Auto-number duplicate names
        name_counts[raw_name] += 1
        count = name_counts[raw_name]
        display_name = raw_name if count == 1 else f"{raw_name} ({count})"

        grp_pts   = 0
        r32_pts   = 0
        r16_pts   = 0
        qf_pts    = 0
        sf_pts    = 0
        final_pts = 0
        champ_pts = 0

        # --- Group stage + 3rd place ---
        for rnd, col_start, col_end in [("GROUP", 2, 25), ("3RD", 26, 33)]:
            for col in range(col_start, col_end + 1):
                team = get_cell(row, col)
                if team and (rnd, team) in advanced_teams:
                    grp_pts += POINTS[rnd]

        # --- Knockout rounds with duplicate protection per round ---
        knockout_picks = []
        for rnd, col_start, col_end in [("R32", 34, 49), ("R16", 50, 57), ("QF", 58, 61), ("SF", 62, 63), ("FINAL", 64, 65)]:
            for col in range(col_start, col_end + 1):
                team = get_cell(row, col)
                if team:
                    knockout_picks.append((rnd, team))

        scored_round_teams = set()
        round_pts = {"R32": 0, "R16": 0, "QF": 0, "SF": 0, "FINAL": 0}
        for rnd, team in knockout_picks:
            if (rnd, team) in scored_round_teams:
                continue
            scored_round_teams.add((rnd, team))
            if (rnd, team) in advanced_teams:
                round_pts[rnd] += POINTS[rnd]

        r32_pts   = round_pts["R32"]
        r16_pts   = round_pts["R16"]
        qf_pts    = round_pts["QF"]
        sf_pts    = round_pts["SF"]
        final_pts = round_pts["FINAL"]

        # --- Champion ---
        champ = get_cell(row, CHAMPION_COL)
        if champ and ("WINNER", champ) in advanced_teams:
            champ_pts += POINTS["WINNER"]

        total = grp_pts + r32_pts + r16_pts + qf_pts + sf_pts + final_pts + champ_pts

        entries.append({
            "name":     display_name,
            "email":    email,
            "group":    grp_pts,
            "r32":      r32_pts,
            "r16":      r16_pts,
            "qf":       qf_pts,
            "sf":       sf_pts,
            "final":    final_pts,
            "champion": champ_pts,
            "total":    total,
        })

    # Sort by total descending
    entries.sort(key=lambda x: x["total"], reverse=True)
    return entries


def update_sheet(service, entries, picks_visible):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if picks_visible:
        header = ["Rank", "Entry Name", "Email", "Group Pts", "R32 Pts", "R16 Pts", "QF Pts", "SF Pts", "Final Pts", "Champion Pts", "Total", "Last Updated"]
        rows = [
            [
                i + 1,
                e["name"],
                e["email"],
                e["group"],
                e["r32"],
                e["r16"],
                e["qf"],
                e["sf"],
                e["final"],
                e["champion"],
                e["total"],
                now if i == 0 else ""
            ]
            for i, e in enumerate(entries)
        ]
        data = [header] + rows
    else:
        header = ["Rank", "Entry Name", "Email", "Total Points", "Note", "Last Updated"]
        rows = [
            [
                i + 1,
                e["name"],
                e["email"],
                "—",
                "Picks revealed when tournament starts",
                now if i == 0 else ""
            ]
            for i, e in enumerate(entries)
        ]
        data = [
            ["⚽ WORLD CUP POOL 2026 — Standings"],
            ["Picks are hidden until the tournament kicks off on June 11, 2026."],
            [""],
            header
        ] + rows

    # Clear the sheet first to remove any old data
    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range="Leaderboard!A1:Z1000"
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range="Leaderboard!A1",
        valueInputOption="RAW",
        body={"values": data}
    ).execute()

    # Apply top 3 colors if picks are visible
    if picks_visible and len(entries) > 0:
        apply_top3_colors(service, picks_visible)

    status = "PICKS HIDDEN" if not picks_visible else "PICKS VISIBLE"
    print(f"[✓] Leaderboard updated — {len(entries)} entries — {status} — {now}")


def apply_top3_colors(service, picks_visible):
    """Highlight top 3 rows with gold, silver, bronze colors."""

    # Row offsets depend on whether picks are visible or hidden
    # Visible mode: row 1 = header, rows 2+ = entries
    # Hidden mode: rows 1-3 = title/note/blank, row 4 = header, rows 5+ = entries
    if picks_visible:
        header_row = 0   # 0-indexed
        first_entry_row = 1
    else:
        header_row = 3
        first_entry_row = 4

    # Colors: gold, silver, bronze
    colors = [
        {"red": 1.0,  "green": 0.84, "blue": 0.0},   # Gold
        {"red": 0.75, "green": 0.75, "blue": 0.75},   # Silver
        {"red": 0.8,  "green": 0.5,  "blue": 0.2},    # Bronze
    ]

    requests_body = []

    # First clear all background colors from entry rows
    requests_body.append({
        "repeatCell": {
            "range": {
                "sheetId": get_leaderboard_sheet_id(service),
                "startRowIndex": first_entry_row,
                "endRowIndex": first_entry_row + 100,
            },
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
            "fields": "userEnteredFormat.backgroundColor"
        }
    })

    # Apply gold/silver/bronze to top 3
    sheet_id = get_leaderboard_sheet_id(service)
    for i, color in enumerate(colors):
        row_index = first_entry_row + i
        requests_body.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor"
            }
        })

    service.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": requests_body}
    ).execute()
    print("[✓] Top 3 colors applied")


def get_leaderboard_sheet_id(service):
    """Get the sheet ID of the Leaderboard tab."""
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == "Leaderboard":
            return sheet["properties"]["sheetId"]
    return 0


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
        spreadsheetId=SPREADSHEET_ID,
        range="Picks!A1:BP500"  # A through BP to capture all columns including Email
    ).execute().get("values", [])
    print(f"[→] {len(picks_rows) - 1} entries loaded")

    advanced = fetch_standings_and_results()
    print(f"[→] {len(advanced)} advancement records fetched")

    entries = calculate_scores(picks_rows, advanced)
    update_sheet(service, entries, picks_visible)
    print("[✓] Done!")


def test_mode():
    """
    Test mode — uses fake tournament results based on the real picks in the sheet.
    Pretends that ALL teams the first entry picked actually advanced.
    This lets you verify column mapping, scoring logic, and Leaderboard output
    without needing a real API key or waiting for the tournament to start.

    Run with:  python update_scores.py test
    """
    print("=" * 60)
    print("[TEST MODE] Using fake results — no API calls made")
    print("=" * 60)

    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDS_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)

    picks_rows = service.spreadsheets().values().get(
        spreadsheetId=os.environ["SPREADSHEET_ID"],
        range="Picks!A1:BP500"
    ).execute().get("values", [])
    print(f"[→] {len(picks_rows) - 1} entries loaded from sheet")

    if len(picks_rows) < 2:
        print("[!] No entries found in sheet — add a test entry via the form first!")
        return

    # Build fake advanced_teams by pretending every team the FIRST entry picked actually won
    first_row = picks_rows[1]
    fake_advanced = set()

    for col in range(2, 26):   # Group stage
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("GROUP", team))

    for col in range(26, 34):  # 3rd place
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("3RD", team))

    for col in range(34, 50):  # Round of 32
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("R32", team))

    for col in range(50, 58):  # Round of 16
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("R16", team))

    for col in range(58, 62):  # Quarter-finals
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("QF", team))

    for col in range(62, 64):  # Semi-finals
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("SF", team))

    for col in range(64, 66):  # Final
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("FINAL", team))

    champ = get_cell(first_row, CHAMPION_COL)
    if champ: fake_advanced.add(("WINNER", champ))

    print(f"[→] Fake results: {len(fake_advanced)} teams marked as advanced")

    entries = calculate_scores(picks_rows, fake_advanced)

    print("\n[→] Calculated scores:")
    print(f"  {'Rank':<5} {'Name':<25} {'Email':<30} {'Grp':>5} {'R32':>5} {'R16':>5} {'QF':>5} {'SF':>5} {'Fin':>5} {'Champ':>6} {'Total':>6}")
    print("  " + "-" * 105)
    for i, e in enumerate(entries):
        print(f"  {i+1:<5} {e['name']:<25} {e['email']:<30} {e['group']:>5} {e['r32']:>5} {e['r16']:>5} {e['qf']:>5} {e['sf']:>5} {e['final']:>5} {e['champion']:>6} {e['total']:>6}")

    print("\n[→] Writing test results to Leaderboard tab...")
    update_sheet(service, entries, picks_visible=True)
    print("[✓] Test complete! Check the Leaderboard tab in your spreadsheet.")
    print("    The first entry should have a PERFECT score since we used their picks as results.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_mode()
    else:
        main()
