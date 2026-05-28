"""
One-shot script — generates synthetic CSV fixtures for tests.

Replaces the purged real Eversports exports with anonymised data that
preserves the same schema, row counts, and structural quirks the parsers
and tests depend on.  Run once; the output is committed and the script
can be deleted.

Structural invariants preserved:
  bookings.csv  — 29 rows (28 unique emails + 1 exact duplicate of row 0
                  to exercise the bootstrap dedup path), BOM-prefixed UTF-8,
                  semicolon-delimited, all fields double-quoted.
  all activities.csv — 43 rows, BOM-prefixed UTF-8, semicolon-delimited,
                       German headers, no quoting.
  noshows.csv   — 0 bytes (the real file was empty; parsers handle this).

Test-assertion invariants (see tests/test_parsers.py, test_bootstrap.py):
  bookings row 0: start=01/05/2026 10:00, end=10:55, price=252.00 EUR,
                  attended=yes, newsletter=no (→ False), product=10er Karte-Gruppe,
                  phone=015200000001, email=test+1@example.com
  bookings: 28 distinct emails, newsletter mix of yes+no, all attended=yes,
            customer_number always blank, contacts_missing_email=0
  bookings products: 10er Karte-Gruppe (card), 20er Karte-Gruppe (card),
                     3 Trial Cards-Introduction to Pilates Reformer (trial),
                     Gruppenmitgliedschaft-1 x Woche (membership),
                     Gruppenmitgliedschaft-2 x Woche (membership),
                     Limitless-Gruppenmitgliedschaft (membership)
  activities row 0: Klasse, 01.05.2026, 10:00, 10:55, Angemeldet=10,
                    Max=10, Warteliste=12, buchbar, Reformer Pilates,
                    "All levels pilates equipment"
  activities row 2: Angemeldet=9, Max=10 → available_spots=1
  activities: 43 unique (name, date, start) combos → 43 sessions
"""

import pathlib

OUT = pathlib.Path(__file__).parent.parent / "requirements_v2" / "sample_exports"
OUT.mkdir(parents=True, exist_ok=True)

BOM = "﻿"

# ── bookings.csv ──────────────────────────────────────────────────────────────

BOOKINGS_HEADER = (
    '"Start";"End";"Activity name";"Location";"Trainer nickname";'
    '"Customer number";"First name";"Last name";"E-Mail";"Clubgroup name";'
    '"Newsletter";"Product name";"Price";"Attended";"Phone number"'
)

LOCATION = "Test Studio, Teststraße 1, 70173 Stuttgart"

# 6 product buckets required by test_bootstrap_products_classified
PRODUCTS = [
    ("10er Karte-Gruppe",                              "252.00 €"),
    ("20er Karte-Gruppe",                              "462.00 €"),
    ("3 Trial Cards-Introduction to Pilates Reformer", "59.00 €"),
    ("Gruppenmitgliedschaft-1 x Woche",               "89.00 €"),
    ("Gruppenmitgliedschaft-2 x Woche",               "129.00 €"),
    ("Limitless-Gruppenmitgliedschaft",               "149.00 €"),
]

# Sessions spread across two weeks — 5 slots
SESSIONS = [
    ("01/05/2026 10:00", "01/05/2026 10:55", "Feel-Good Friday Group Class",   "Trainer A"),
    ("01/05/2026 11:00", "01/05/2026 11:55", "Reformer Flow Group Class",       "Trainer B"),
    ("05/05/2026 09:00", "05/05/2026 09:55", "Reformer Booty Burn Group Class", "Trainer A"),
    ("06/05/2026 10:00", "06/05/2026 10:55", "Feel-Good Friday Group Class",    "Trainer B"),
    ("08/05/2026 11:00", "08/05/2026 11:55", "Reformer Flow Group Class",       "Trainer A"),
]

rows = []
email_idx = 1  # test+1@example.com … test+28@example.com

# Distribute 28 unique booking rows across sessions and products
#  Row 0  → session 0, 10er Karte-Gruppe  (used again as the duplicate row 29)
#  Rows 0-11   → 10er Karte-Gruppe
#  Rows 12-15  → 20er Karte-Gruppe
#  Rows 16-19  → Trial
#  Rows 20-22  → Mitgliedschaft-1x
#  Rows 23-24  → Mitgliedschaft-2x
#  Rows 25-27  → Limitless
assignments = (
    [0] * 12   # 12 rows → 10er Karte-Gruppe
    + [1] * 4  # 4  rows → 20er Karte-Gruppe
    + [2] * 4  # 4  rows → Trial
    + [3] * 3  # 3  rows → Mitglied 1x
    + [4] * 2  # 2  rows → Mitglied 2x
    + [5] * 3  # 3  rows → Limitless  (12+4+4+3+2+3 = 28)
)

for i, prod_idx in enumerate(assignments):
    sess = SESSIONS[i % len(SESSIONS)]
    start, end, act, trainer = sess
    prod_name, price = PRODUCTS[prod_idx]
    newsletter = "yes" if i % 3 == 1 else "no"   # mix of yes / no
    phone = f"+49176{email_idx:08d}"
    if i == 0:
        phone = "015200000001"                     # first row: German 015x format (no +)

    row = (
        f'"{start}";"{end}";"{act}";"{LOCATION}";"{trainer}";"";'
        f'"Test";"Customer {email_idx}";"test+{email_idx}@example.com";'
        f'"{"Intern" if i % 5 == 0 else "Extern"}";"{newsletter}";'
        f'"{prod_name}";"{price}";"yes";"{phone}"'
    )
    rows.append(row)
    email_idx += 1

# Row 29 = exact duplicate of row 27 (test+28@example.com, same email+start+activity
# → deduped by bootstrap to 28 unique bookings).  Row 0 (test+1@example.com) appears
# only ONCE so test_bootstrap_contact_derived_fields can assert total_sessions_attended==1.
rows.append(rows[27])

assert len(rows) == 29, f"Expected 29 booking rows, got {len(rows)}"
unique_emails = {r.split(";")[8].strip('"') for r in rows[:-1]}  # exclude dup
assert len(unique_emails) == 28, f"Expected 28 unique emails, got {len(unique_emails)}"

bookings_content = BOM + BOOKINGS_HEADER + "\n" + "\n".join(rows) + "\n"
(OUT / "bookings.csv").write_text(bookings_content, encoding="utf-8")
print(f"bookings.csv: {len(rows)} rows written")

# ── all activities.csv ────────────────────────────────────────────────────────

ACTIVITIES_HEADER = (
    "Typ;Datum;Startzeit;Endzeit;Name;Angemeldet;Anwesend;"
    "Max. Teilnehmer;Warteliste;Trainer;Ort;Status;Sport;"
    "Aktivitätsgruppe;Kommentar zur Einheit;Veröffentlicht"
)

# 43 unique sessions across 6 weeks (Mon/Wed/Fri schedule)
ACTIVITY_SLOTS = [
    # (date DD.MM.YYYY, start HH:MM, end HH:MM, name, trainer, reg, att, max, waitlist)
    # Week 1
    ("01.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A", 10, 10, 10, 12),
    ("01.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  8,  8, 10,  0),
    ("01.05.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer A",  9,  9, 10,  0),  # row 2: avail=1
    ("04.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer B",  7,  7, 10,  0),
    ("04.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer A",  6,  6, 10,  0),
    ("06.05.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer B",  5,  5, 10,  0),
    ("06.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  8,  8, 10,  0),
    ("08.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  4,  4, 10,  0),
    # Week 2
    ("11.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  9,  9, 10,  3),
    ("11.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  7,  7, 10,  0),
    ("11.05.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer A",  6,  6, 10,  0),
    ("13.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer B",  8,  8, 10,  0),
    ("13.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer A",  5,  5, 10,  0),
    ("15.05.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer B",  4,  4, 10,  0),
    ("15.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  7,  7, 10,  0),
    ("15.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  3,  3, 10,  0),
    # Week 3
    ("18.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  6,  6, 10,  0),
    ("18.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  5,  5, 10,  0),
    ("18.05.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer A",  4,  4, 10,  0),
    ("20.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer B",  8,  8, 10,  0),
    ("20.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer A",  7,  7, 10,  0),
    ("22.05.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer B",  6,  6, 10,  0),
    ("22.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  5,  5, 10,  0),
    ("22.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  4,  4, 10,  0),
    # Week 4
    ("25.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  9,  9, 10,  1),
    ("25.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  8,  8, 10,  0),
    ("25.05.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer A",  7,  7, 10,  0),
    ("27.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer B",  6,  6, 10,  0),
    ("27.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer A",  5,  5, 10,  0),
    ("29.05.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer B",  4,  4, 10,  0),
    ("29.05.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  3,  3, 10,  0),
    ("29.05.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  2,  2, 10,  0),
    # Week 5
    ("01.06.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  8,  8, 10,  0),
    ("01.06.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  7,  7, 10,  0),
    ("01.06.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer A",  6,  6, 10,  0),
    ("03.06.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer B",  5,  5, 10,  0),
    ("03.06.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer A",  4,  4, 10,  0),
    ("05.06.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer B",  3,  3, 10,  0),
    ("05.06.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  2,  2, 10,  0),
    ("05.06.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  1,  1, 10,  0),
    # Week 6 (3 more to reach 43)
    ("08.06.2026", "10:00", "10:55", "Feel-Good Friday Group Class",    "Trainer A",  0,  0, 10,  0),
    ("08.06.2026", "11:00", "11:55", "Reformer Flow Group Class",        "Trainer B",  0,  0, 10,  0),
    ("08.06.2026", "09:00", "09:55", "Reformer Booty Burn Group Class",  "Trainer A",  0,  0, 10,  0),
]

assert len(ACTIVITY_SLOTS) == 43, f"Expected 43 activity rows, got {len(ACTIVITY_SLOTS)}"

act_rows = []
for date, start, end, name, trainer, reg, att, max_p, wait in ACTIVITY_SLOTS:
    act_rows.append(
        f"Klasse;{date};{start};{end};{name};{reg};{att};{max_p};{wait};{trainer};;buchbar;"
        f"Reformer Pilates;All levels pilates equipment;;"
    )

activities_content = BOM + ACTIVITIES_HEADER + "\n" + "\n".join(act_rows) + "\n"
(OUT / "all activities.csv").write_text(activities_content, encoding="utf-8")
print(f"all activities.csv: {len(act_rows)} rows written")

# ── noshows.csv — 0 bytes (real file was empty) ───────────────────────────────

(OUT / "noshows.csv").write_bytes(b"")
print("noshows.csv: 0 bytes written")

print("\nDone. Run: uv run pytest tests/test_parsers.py tests/test_bootstrap.py -v")
