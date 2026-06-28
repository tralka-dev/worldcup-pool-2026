"""
World Cup Pool 2026 — Automated Score Updater
Supports multiple entries per participant.

Column layout (0-indexed):
  Col 0     (A)   = Timestamp
  Col 1     (B)   = Entry Name
  Col 2-25  (C-Z) = Group stage picks (24 cols, Groups A-L, 1st+2nd each)
  Col 26-33       = Best 3rd place picks (8 cols)
  Col 34-49       = Round of 32 picks (16 cols)
  Col 50-57       = Round of 16 picks (8 cols)
  Col 58-61       = Quarter-final picks (4 cols)
  Col 62-63       = Semi-final picks (2 cols)
  Col 64          = Final winner / Champion (1 col)
  Col 65          = Email

Point values:
  GROUP/3RD = 1, R32 = 2, R16 = 3, QF = 4, SF = 5, FINAL = 5, CHAMPION = 7

Scoring rules:
  Group + 3rd picks (cols 2-33): 1 pt for any team that qualified to R32,
  regardless of whether they finished 1st, 2nd, or as a best 3rd place team.
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

WC2026_ID        = os.environ.get("WC2026_ID", "WC")
PICKS_VISIBLE    = os.environ.get("PICKS_VISIBLE", "false").lower() == "true"
TOURNAMENT_START = datetime(2026, 6, 11, tzinfo=timezone.utc)

POINTS = {
    "GROUP":  1,
    "3RD":    1,
    "R32":    2,
    "R16":    3,
    "QF":     4,
    "SF":     5,
    "FINAL":  5,   # correct finalist pick
    "WINNER": 7,   # correct champion pick
}

ROUND_MAP = {
    "GROUP_STAGE":    "GROUP",
    "ROUND_OF_32":    "R32",
    "LAST_16":        "R16",
    "QUARTER_FINALS": "QF",
    "SEMI_FINALS":    "SF",
    "FINAL":          "FINAL",
}

CHAMPION_COL = 64
EMAIL_COL    = 65

# Maps API team names → form/sheet team names
# Add entries here whenever the API uses a different spelling than the entry form
TEAM_NAME_MAP = {
    "Korea Republic":          "South Korea",
    "IR Iran":                 "Iran",
    "Côte d'Ivoire":           "Ivory Coast",
    "Cote d'Ivoire":           "Ivory Coast",
    "C\u00f4te d'Ivoire":      "Ivory Coast",
    "Bosnia-Herzegovina":      "Bosnia and Herzegovina",
    "Bosnia & Herzegovina":    "Bosnia and Herzegovina",
    "Congo DR":                "DR Congo",
    "Türkiye":                 "Turkiye",
    "T\u00fcrkiye":            "Turkiye",
    "Czech Republic":          "Czechia",
}
}

def normalize(name):
    """Normalize a team name from the API to match the entry form spelling."""
    return TEAM_NAME_MAP.get(name, name)


SHORT_HEADERS = (
    ["Timestamp", "Entry Name"]
    + [f"Grp {g} {n}" for g in "ABCDEFGHIJKL" for n in ["1st", "2nd"]]
    + [f"3rd {i+1}" for i in range(8)]
    + [f"R32-{i+1}" for i in range(16)]
    + [f"R16-{i+1}" for i in range(8)]
    + [f"QF-{i+1}" for i in range(4)]
    + ["SF-1", "SF-2"]
    + ["Champion", "Email"]
)


def rename_picks_headers(service):
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range="Picks!A1",
        valueInputOption="RAW",
        body={"values": [SHORT_HEADERS]}
    ).execute()
    print(f"[✓] Picks headers renamed ({len(SHORT_HEADERS)} columns)")


def fetch_standings_and_results():
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    base = "https://api.football-data.org/v4"
    advanced_teams = set()
    r32_qualifiers = set()  # all 32 teams that qualified to R32 (1st, 2nd, or best 3rd)
    third_place = []

    # Group stage standings
    try:
        r = requests.get(f"{base}/competitions/{WC2026_ID}/standings", headers=headers, timeout=10)
        r.raise_for_status()
        for group in r.json().get("standings", []):
            if group.get("type") != "TOTAL":
                continue
            for entry in group["table"]:
                pos  = entry["position"]
                team = normalize(entry["team"]["name"])
                if entry.get("playedGames", 0) > 0:
                    if pos <= 2:
                        advanced_teams.add(("GROUP", team))
                        r32_qualifiers.add(team)
                    elif pos == 3:
                        third_place.append((
                            entry.get("points", 0),
                            entry.get("goalDifference", 0),
                            team
                        ))
    except Exception as e:
        print(f"[!] Could not fetch standings: {e}")

    # Best 8 third-place teams also qualify for R32
    third_place.sort(reverse=True)
    for _, _, team in third_place[:8]:
        advanced_teams.add(("3RD", team))
        r32_qualifiers.add(team)

    # Knockout results
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
            home = normalize(match["homeTeam"]["name"])
            away = normalize(match["awayTeam"]["name"])
            home_score = s.get("home") or 0
            away_score = s.get("away") or 0
            winner = home if home_score > away_score else away
            loser  = away if winner == home else home
            advanced_teams.add((stage, winner))
            if stage == "FINAL":
                advanced_teams.add(("FINAL", loser))
                advanced_teams.add(("WINNER", winner))
            if stage == "R32":
                advanced_teams.add(("R32", loser))
    except Exception as e:
        print(f"[!] Could not fetch knockout results: {e}")

    return advanced_teams, r32_qualifiers


def get_cell(row, col):
    if col < len(row) and row[col]:
        return row[col].strip()
    return None


def calculate_scores(picks_rows, advanced_teams, r32_qualifiers):
    entries = []
    name_counts = defaultdict(int)

    for row in picks_rows[1:]:  # skip header
        if not row or len(row) < 2 or not row[1]:
            continue
        raw_name = row[1].strip()
        if not raw_name:
            continue

        email = get_cell(row, EMAIL_COL) or ""

        name_counts[raw_name] += 1
        count = name_counts[raw_name]
        display_name = raw_name if count == 1 else f"{raw_name} ({count})"

        # ── Group + 3rd picks (cols 2-33): 1 pt if team qualified to R32 ──
        # Doesn't matter if they finished 1st, 2nd, or as best 3rd place
        grp_pts = 0
        for col in range(2, 34):
            team = get_cell(row, col)
            if team and team in r32_qualifiers:
                grp_pts += 1

        # ── Knockout rounds with duplicate protection ──
        round_pts = {"R32": 0, "R16": 0, "QF": 0, "SF": 0, "FINAL": 0}
        scored_round_teams = set()

        knockout_rounds = [
            ("R32",   34, 49),
            ("R16",   50, 57),
            ("QF",    58, 61),
            ("SF",    62, 63),
        ]
        for rnd, col_start, col_end in knockout_rounds:
            for col in range(col_start, col_end + 1):
                team = get_cell(row, col)
                if not team:
                    continue
                key = (rnd, team)
                if key in scored_round_teams:
                    continue
                scored_round_teams.add(key)
                if key in advanced_teams:
                    round_pts[rnd] += POINTS[rnd]

        # ── Final / Champion (col 64) ──
        champ_pts = 0
        final_pts = 0
        champ = get_cell(row, CHAMPION_COL)
        if champ:
            if ("FINAL", champ) in advanced_teams:
                final_pts = POINTS["FINAL"]
            if ("WINNER", champ) in advanced_teams:
                champ_pts = POINTS["WINNER"]

        total = grp_pts + sum(round_pts.values()) + final_pts + champ_pts

        entries.append({
            "name":          display_name,
            "email":         email,
            "group":         grp_pts,
            "r32":           round_pts["R32"],
            "r16":           round_pts["R16"],
            "qf":            round_pts["QF"],
            "sf":            round_pts["SF"],
            "final":         final_pts,
            "champion":      champ_pts,
            "total":         total,
            "grp_correct":   grp_pts,
            "r32_correct":   round_pts["R32"] // POINTS["R32"],
            "r16_correct":   round_pts["R16"] // POINTS["R16"],
            "qf_correct":    round_pts["QF"]  // POINTS["QF"],
            "sf_correct":    round_pts["SF"]  // POINTS["SF"],
            "final_correct": 1 if final_pts > 0 else 0,
            "champ_correct": 1 if champ_pts > 0 else 0,
        })

    entries.sort(key=lambda x: x["total"], reverse=True)
    return entries


def update_sheet(service, entries, picks_visible):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if picks_visible:
        header = [
            "Rank", "Entry Name", "Email",
            "Group+3rd", "G Correct",
            "R32 Pts",   "R32 Correct",
            "R16 Pts",   "R16 Correct",
            "QF Pts",    "QF Correct",
            "SF Pts",    "SF Correct",
            "Final Pts", "Champ Pts",
            "Total", "Last Updated"
        ]
        rows = [
            [
                i + 1,
                e["name"], e["email"],
                e["group"],       f"{e['grp_correct']}/32",
                e["r32"],         f"{e['r32_correct']}/16",
                e["r16"],         f"{e['r16_correct']}/8",
                e["qf"],          f"{e['qf_correct']}/4",
                e["sf"],          f"{e['sf_correct']}/2",
                e["final"],       e["champion"],
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
                i + 1, e["name"], e["email"],
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

    if picks_visible and len(entries) > 0:
        apply_top3_colors(service, picks_visible)

    status = "PICKS HIDDEN" if not picks_visible else "PICKS VISIBLE"
    print(f"[✓] Leaderboard updated — {len(entries)} entries — {status} — {now}")


def apply_top3_colors(service, picks_visible):
    first_entry_row = 1 if picks_visible else 4
    colors = [
        {"red": 1.0,  "green": 0.84, "blue": 0.0},
        {"red": 0.75, "green": 0.75, "blue": 0.75},
        {"red": 0.8,  "green": 0.5,  "blue": 0.2},
    ]
    requests_body = []
    sheet_id = get_leaderboard_sheet_id(service)

    requests_body.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": first_entry_row, "endRowIndex": first_entry_row + 100},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
            "fields": "userEnteredFormat.backgroundColor"
        }
    })
    for i, color in enumerate(colors):
        row_index = first_entry_row + i
        requests_body.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1},
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
    rename_picks_headers(service)

    picks_rows = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Picks!A1:BP500"
    ).execute().get("values", [])
    print(f"[→] {len(picks_rows) - 1} entries loaded")

    advanced, r32_qualifiers = fetch_standings_and_results()
    print(f"[→] {len(advanced)} advancement records fetched")
    print(f"[→] {len(r32_qualifiers)} teams qualified to R32")

    entries = calculate_scores(picks_rows, advanced, r32_qualifiers)
    update_sheet(service, entries, picks_visible)
    print("[✓] Done!")


def test_mode():
    print("=" * 60)
    print("[TEST MODE] Using fake results — no API calls made")
    print("=" * 60)

    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDS_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    rename_picks_headers(service)

    picks_rows = service.spreadsheets().values().get(
        spreadsheetId=os.environ["SPREADSHEET_ID"],
        range="Picks!A1:BP500"
    ).execute().get("values", [])
    print(f"[→] {len(picks_rows) - 1} entries loaded")

    if len(picks_rows) < 2:
        print("[!] No entries found — add a test entry via the form first!")
        return

    first_row = picks_rows[1]
    fake_advanced = set()
    r32_qualifiers = set()

    for col in range(2, 34):  # all group + 3rd picks
        team = get_cell(first_row, col)
        if team:
            fake_advanced.add(("GROUP", team))
            r32_qualifiers.add(team)
    for col in range(34, 50):
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("R32", team))
    for col in range(50, 58):
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("R16", team))
    for col in range(58, 62):
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("QF", team))
    for col in range(62, 64):
        team = get_cell(first_row, col)
        if team: fake_advanced.add(("SF", team))
    champ = get_cell(first_row, CHAMPION_COL)
    if champ:
        fake_advanced.add(("FINAL", champ))
        fake_advanced.add(("WINNER", champ))

    print(f"[→] Fake results: {len(fake_advanced)} teams marked as advanced")
    entries = calculate_scores(picks_rows, fake_advanced, r32_qualifiers)

    print(f"\n  {'Rank':<5} {'Name':<25} {'Grp':>5} {'R32':>5} {'R16':>5} {'QF':>4} {'SF':>4} {'Fin':>4} {'Chp':>4} {'Tot':>6}")
    print("  " + "-" * 75)
    for i, e in enumerate(entries):
        print(f"  {i+1:<5} {e['name']:<25} {e['group']:>5} {e['r32']:>5} {e['r16']:>5} {e['qf']:>4} {e['sf']:>4} {e['final']:>4} {e['champion']:>4} {e['total']:>6}")

    print("\n[→] Writing test results to Leaderboard tab...")
    update_sheet(service, entries, picks_visible=True)
    print("[✓] Test complete! First entry should have a perfect score.")


def custom_test_mode():
    print("=" * 60)
    print("[CUSTOM TEST MODE] Using manually defined fake results")
    print("=" * 60)

    FAKE_RESULTS = {
        "GROUP": [
            "Mexico", "South Africa",
            "Switzerland", "Canada",
            "Brazil", "Morocco",
            "USA", "Australia",
            "Germany", "Ivory Coast",
            "Netherlands", "Japan",
            "Egypt", "Belgium",
            "Spain", "Uruguay",
            "France", "Norway",
            "Argentina", "Austria",
            "Portugal", "Colombia",
            "England", "Croatia",
        ],
        "3RD": [
            "Bosnia and Herzegovina", "Scotland", "Paraguay", "Sweden",
            "Algeria", "Iran", "Cape Verde", "Ecuador",
        ],
        "R32": [
            "Switzerland", "Brazil", "Germany", "Netherlands",
            "France", "USA", "Belgium", "Spain",
            "Paraguay", "Egypt", "Colombia", "Portugal",
            "Canada", "Argentina", "England", "Morocco",
        ],
        "R16": [
            "Brazil", "Netherlands", "France", "Spain",
            "Argentina", "Portugal", "England", "Germany",
        ],
        "QF":     ["Brazil", "France", "Argentina", "England"],
        "SF":     ["Brazil", "Argentina"],
        "FINAL":  ["Brazil", "Argentina"],
        "WINNER": ["Brazil"],
    }

    fake_advanced = set()
    r32_qualifiers = set()

    for rnd, teams in FAKE_RESULTS.items():
        for team in teams:
            fake_advanced.add((rnd, team))

    # All group 1st/2nd + best 3rd qualify for R32
    for team in FAKE_RESULTS["GROUP"] + FAKE_RESULTS["3RD"]:
        r32_qualifiers.add(team)

    print(f"[→] Fake results: {len(fake_advanced)} advancement records")
    print(f"[→] {len(r32_qualifiers)} teams in R32")

    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDS_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=creds)
    rename_picks_headers(service)

    picks_rows = service.spreadsheets().values().get(
        spreadsheetId=os.environ["SPREADSHEET_ID"],
        range="Picks!A1:BP500"
    ).execute().get("values", [])
    print(f"[→] {len(picks_rows) - 1} entries loaded")

    if len(picks_rows) < 2:
        print("[!] No entries found — submit some picks first!")
        return

    entries = calculate_scores(picks_rows, fake_advanced, r32_qualifiers)

    print(f"\n  {'Rank':<5} {'Name':<25} {'Grp':>5} {'R32':>5} {'R16':>5} {'QF':>4} {'SF':>4} {'Fin':>4} {'Chp':>4} {'Tot':>6}")
    print("  " + "-" * 75)
    for i, e in enumerate(entries):
        print(f"  {i+1:<5} {e['name']:<25} {e['group']:>5} {e['r32']:>5} {e['r16']:>5} {e['qf']:>4} {e['sf']:>4} {e['final']:>4} {e['champion']:>4} {e['total']:>6}")

    print("\n[→] Writing results to Leaderboard tab...")
    update_sheet(service, entries, picks_visible=True)
    print("[✓] Done! Check the Leaderboard tab.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_mode()
    elif len(sys.argv) > 1 and sys.argv[1] == "custom":
        custom_test_mode()
    else:
        main()
