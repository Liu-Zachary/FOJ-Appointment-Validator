#!/usr/bin/env python3
"""
FOJ Tool  -  Appointment Validator + No-Show Helper (one program, one set of exports)
=====================================================================================
Replaces the "YCBM & FBM Comparison Sheet".  Run it and it produces:

  1. appointment_sheet.csv    APPROVED appointments for ALL upcoming dates (import to Sheets): a banner
                              row per date, times earliest -> latest, names A -> Z by last name.
                              One-time no-show guests are approved with a "may not show up" note.
  2. decisions.csv            Every request in sections: DENY, APPROVE, REVIEW (reasons, corrected
                              FOJ_ID, what to do in YCBM).
  3. warnings_and_penalties.csv  Checklist of guests who missed an appointment in a date range
                              (default: past 7 days): ON PENALTY (2 months in a row), WARNING (1 no-show),
                              or VERIFY FIRST (possible volunteer check-in mix-up). "Done" column:
                              select it in Sheets -> Insert -> Checkbox.
  The no-show check works on its own: YCBM export + FBM visit export is all it needs
  (python3 foj_tool.py --only noshows).

INPUT FILES (give them in any order, or just run it in the folder that holds them)
  * YCBM report         YCBM_report_*.csv                      (appointment requests/statuses)
  * Visit export        has a "Visit on" column                (FBM visit history, with date/time)
  * Guest list          First/Last/Email/Home Phone/Cell/Barcode (full guest info + barcodes)
  * Undated visit lists First Name, Last Name, Barcode only    (old "Guest Visit Export"s)
                        -> IGNORED unless you pass --undated-month, because without a date
                           we can't tell which day (or month) a visit happened.

HOW DECISIONS ARE MADE  (an appointment is APPROVED only if every check passes)
  0. Guest-list notes  "no food til re-registered", "temporary card expired", "deceased", "out of area" -> DENY.
  1. FOJ_ID check   The barcode typed in YCBM must belong to that name in the guest list.
                    Wrong/missing barcode -> we look the guest up by name (and phone/email)
                    and tell you the corrected barcode. Not in the guest list = not registered
                    (temporary / no barcode) -> DENY.
  2. Duplicates     Same household requested more than once in a month -> the LATEST request
                    is kept, earlier ones are DENIED.
  3. Already shopped  A visit this calendar month (any location) -> DENY.
  4. No-show history  No-show in 2 months in a row -> DENY ("ON PENALTY").  One no-show last month -> approved
                    with a WARNING.  Only CONFIRMED no-shows count (see below).
  Anything uncertain (fuzzy name, barcode registered to someone else, ...) -> REVIEW.

HOW NO-SHOWS ARE DECIDED
  An accepted appointment (YCBM status FINISHED / UPCOMING, not cancelled) is a no-show if no
  visit by that household is recorded on that date. Guests are matched by barcode, then
  phone/email, then exact name.  To catch volunteer check-in mistakes, a no-show is only marked
  "LIKELY" when no *unclaimed* visit that day looks like them (barcode 1 digit off, same phone,
  similar name).  Otherwise it is "VERIFY" and is NOT counted toward penalties.
  Dates with no/very few visits recorded are skipped (export gap or closed day), and
  --ignore-dates removes dates (e.g. 2026-11-05) from the evidence.

USAGE
  python3 foj_tool.py                                  # use every CSV in this folder
  python3 foj_tool.py history/ new_exports/            # or give folders and/or files (history is merged,
                                                       # overlapping exports are de-duplicated)
  python3 foj_tool.py --as-of 2026-11-07               # "today" (default: date in YCBM filename)
  python3 foj_tool.py --ignore-dates 2026-11-05        # drop these dates from visit evidence
  python3 foj_tool.py --from 2026-10-31 --to 2026-11-06   # warnings/penalties range
  python3 foj_tool.py --only validate | --only noshows
  python3 foj_tool.py --undated-month 2026-10 old_list.csv  # use an undated list for one month

Standard library only (Python 3.8+).
"""

import argparse
import csv
import glob
import os
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, date, timedelta
from difflib import SequenceMatcher

# =========================================================================== #
# Cleaning helpers
# =========================================================================== #

# Guest-list notes: these mean "do not book"; softer ones need a human look
NOTE_DENY = re.compile(r"re-?regist|registration|no food|no shop|temporary card expired|deceased|out of area|\b(?:until|til)\b", re.I)
NOTE_CHECK = re.compile(r"\bmoved\b|check address", re.I)

PLACEHOLDER_BARCODES = {"12345", "54321", "01234", "98765", "56789"}


def clean_barcode(raw):
    """Valid 5-digit barcode or ''. Reads '10944 moved...' notes; rejects 00000, 12345, 4/6 digits, text."""
    raw = (raw or "").strip().strip("\u200f\u200e")
    m = re.match(r"^(\d{5})(?!\d)", raw)
    if not m:
        return ""
    bc = m.group(1)
    if len(set(bc)) == 1 or bc in PLACEHOLDER_BARCODES:
        return ""
    if raw[5:6].isalpha():                      # '12542Santamateo6701'
        return ""
    return bc


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def name_tokens(first, last):
    s = re.sub(r"[^a-z\s]", " ", strip_accents(f"{first or ''} {last or ''}").lower())
    return sorted(t for t in s.split() if t)


def name_sim(a, b):
    sa, sb = "".join(a), "".join(b)
    if not sa or not sb:
        return 0.0
    if SequenceMatcher(None, sa, sb).quick_ratio() < 0.6:
        return 0.0
    score = SequenceMatcher(None, sa, sb).ratio()
    A, B = set(a), set(b)
    small, big = (A, B) if len(A) <= len(B) else (B, A)
    if len(small) >= 2 and small <= big:
        score = max(score, 0.9)
    return score


def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10 or len(set(d)) <= 2 or d.startswith("000") or d == "1234567890":
        return ""
    return d


def bc_close(a, b):
    """True if two barcodes differ by one digit or one adjacent swap (typical typing mistakes)."""
    if not a or not b or a == b or len(a) != len(b):
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) == 1:
        return True
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]


def parse_dt(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def month_of(d):
    return f"{d.year:04d}-{d.month:02d}"


def prev_month(m):
    y, mo = map(int, m.split("-"))
    mo -= 1
    if mo == 0:
        y, mo = y - 1, 12
    return f"{y:04d}-{mo:02d}"


def month_label(m):
    y, mo = map(int, m.split("-"))
    return date(y, mo, 1).strftime("%b %Y")


def fmt_date(d):
    return f"{d:%A, %B} {d.day}, {d.year}"


def short_date(d):
    return f"{d:%a %b} {d.day}"


def fmt_time(dt):
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def sort_name_key(first, last):
    return (strip_accents(last).lower().strip(), strip_accents(first).lower().strip())

# =========================================================================== #
# File reading / detection
# =========================================================================== #

def read_rows(path):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, newline="", encoding=enc) as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Cannot decode {path}")


def detect_kind(rows):
    if not rows:
        return None
    cols = {(c or "").strip().lower() for c in rows[0].keys()}
    if {"fname", "lname", "foj_id", "start", "status"} <= cols:
        return "ycbm"
    if "visit on" in cols and "barcode" in cols:
        return "visits"
    if {"first name", "last name", "barcode"} <= cols and ("email" in cols or "cell phone" in cols):
        return "guests"
    if {"first name", "last name", "barcode"} <= cols:
        return "undated"
    return None


# =========================================================================== #
# People
# =========================================================================== #

class P:
    """A person/household identity from any source."""
    def __init__(self, first, last, barcode_raw="", phones=(), email=""):
        self.first = (first or "").strip()
        self.last = (last or "").strip()
        self.bc_raw = (barcode_raw or "").strip()
        self.bc = clean_barcode(barcode_raw)
        self.tokens = name_tokens(first, last)
        self.key = "".join(self.tokens)
        self.phones = {x for x in (norm_phone(p) for p in phones) if x}
        em = (email or "").strip().lower()
        self.emails = {em} if "@" in em else set()
        self.note = self.bc_raw[5:].strip() if self.bc else ("" if not self.bc_raw else self.bc_raw)

    @property
    def name(self):
        return f"{self.first} {self.last}".strip()

    @property
    def hkey(self):
        return self.bc or ("name:" + self.key)


def match(a, b):
    """'barcode' | 'contact' | 'name' (confirmed)  |  'fuzzy' (verify)  |  None"""
    if a.bc and b.bc:
        return "barcode" if a.bc == b.bc else None
    if (a.phones & b.phones) or (a.emails & b.emails):
        return "contact"
    if a.key and a.key == b.key:
        return "name"
    if len(a.key) >= 7 and name_sim(a.tokens, b.tokens) >= 0.88:
        return "fuzzy"
    return None


CONFIRMED = {"barcode", "contact", "name"}
RANK = {"barcode": 4, "contact": 3, "name": 2, "fuzzy": 1}


class Master:
    """The full guest list (+ any barcode seen in visit exports)."""
    def __init__(self):
        self.people = []
        self.by_bc = defaultdict(list)
        self.by_key = defaultdict(list)
        self.by_token = defaultdict(list)
        self.by_phone = defaultdict(list)
        self.by_email = defaultdict(list)

    def add(self, p):
        if not p.key:
            return
        i = len(self.people)
        self.people.append(p)
        if p.bc:
            self.by_bc[p.bc].append(p)
        self.by_key[p.key].append(p)
        for t in set(p.tokens):
            if len(t) >= 3:
                self.by_token[t].append(i)
        for ph in p.phones:
            self.by_phone[ph].append(p)
        for em in p.emails:
            self.by_email[em].append(p)

    def find_name(self, q):
        res = [(1.0, p) for p in self.by_key.get(q.key, [])] if q.key else []
        if len(q.key) >= 7:
            cand = set()
            for t in q.tokens:
                if len(t) >= 4:
                    cand.update(self.by_token.get(t, []))
            for i in cand:
                p = self.people[i]
                if p.key != q.key:
                    s = name_sim(q.tokens, p.tokens)
                    if s >= 0.88:
                        res.append((s, p))
        res.sort(key=lambda x: -x[0])
        return res


def neighbor_barcodes(bc):
    """Every barcode one typed digit away (substitution or adjacent swap)."""
    out = set()
    for i in range(len(bc)):
        for d in "0123456789":
            if d != bc[i]:
                out.add(bc[:i] + d + bc[i + 1:])
    for i in range(len(bc) - 1):
        if bc[i] != bc[i + 1]:
            out.add(bc[:i] + bc[i + 1] + bc[i] + bc[i + 2:])
    return out


def edit_close(a, b):
    return bool(a and b and (a == b or bc_close(a, b)))


def pick_candidate(cands, q, entered):
    """Choose one barcode among name-matched guest-list people (or None if ambiguous / none)."""
    with_bc = {}
    for s, p in cands:
        if p.bc:
            with_bc.setdefault(p.bc, (s, p))
    if not with_bc:
        return None, 0
    if len(with_bc) == 1:
        bc, (s, p) = next(iter(with_bc.items()))
        return bc, s
    by_contact = [bc for bc, (s, p) in with_bc.items() if (q.phones & p.phones) or (q.emails & p.emails)]
    if len(by_contact) == 1:
        return by_contact[0], with_bc[by_contact[0]][0]
    near = [bc for bc in with_bc if edit_close(entered, bc)]
    if len(near) == 1:
        return near[0], with_bc[near[0]][0]
    return None, 0


def name_overlap(q, o):
    """Names plausibly the same person/household (similar, or share a name word)."""
    return name_sim(q.tokens, o.tokens) >= 0.6 or any(len(t) >= 3 for t in set(q.tokens) & set(o.tokens))


def owner_matches(q, o):
    """Is the name on the request plausibly the person (or household) that owns this barcode?"""
    return name_overlap(q, o) or bool((q.phones & o.phones) or (q.emails & o.emails))


def resolve_identity(q, master):
    """
    Check the barcode typed in YCBM against the guest list.
    Returns dict(status, bc, note, soft, flags)
      status: OK | CORRECTED | UNREGISTERED | AMBIGUOUS | MISMATCH
    """
    entered = q.bc
    out = {"status": "OK", "bc": entered, "note": "", "soft": False, "flags": []}
    owners = master.by_bc.get(entered, []) if entered else []

    def note_flags(people):
        return [o.note for o in people if o.note and (NOTE_DENY.search(o.note) or NOTE_CHECK.search(o.note))]

    if owners:
        ok = any(owner_matches(q, o) for o in owners)
        if ok:
            out["flags"] = note_flags(owners)
            return out
        cands = [(s, p) for s, p in master.find_name(q) if p.bc and p.bc != entered]
        bc, score = pick_candidate(cands, q, entered)
        owner = owners[0].name
        if bc:
            out.update(status="CORRECTED", bc=bc, soft=score < 1.0,
                       note=f"FOJ_ID {entered} belongs to {owner}; this name is {bc} in the guest list")
            out["flags"] = note_flags(master.by_bc[bc])
            return out
        if len({p.bc for s, p in cands}) > 1:
            out.update(status="AMBIGUOUS", soft=True,
                       note=f"FOJ_ID {entered} belongs to {owner}; several guests share this name")
            return out
        out.update(status="MISMATCH", soft=True,
                   note=f"FOJ_ID {entered} is registered to {owner}, name not found in guest list")
        return out

    # barcode missing / invalid / not in guest list -> look up by name
    cands = master.find_name(q)
    bc, score = pick_candidate(cands, q, entered)
    if bc:
        why = "missing/invalid" if not entered else f"{entered} not in guest list"
        out.update(status="CORRECTED", bc=bc, soft=score < 1.0,
                   note=f"FOJ_ID {why}; guest list has this name as {bc}")
        out["flags"] = note_flags(master.by_bc[bc])
        return out
    if len({p.bc for s, p in cands if p.bc}) > 1:
        out.update(status="AMBIGUOUS", bc="", soft=True, note="Several guests share this name; couldn't pick a barcode")
        return out
    # blank/wrong barcode: same phone or email as a guest whose name overlaps
    contact = {}
    for ph in q.phones:
        for o in master.by_phone.get(ph, []):
            if o.bc and name_overlap(q, o):
                contact[o.bc] = o
    for em in q.emails:
        for o in master.by_email.get(em, []):
            if o.bc and name_overlap(q, o):
                contact[o.bc] = o
    if len(contact) == 1:
        nb, o = next(iter(contact.items()))
        out.update(status="CORRECTED", bc=nb, soft=True,
                   note=f"FOJ_ID {entered or 'blank'}; same phone/email as {o.name} ({nb}) - confirm")
        out["flags"] = note_flags(master.by_bc[nb])
        return out
    # typo'd barcode that is one digit / one swap away from a real barcode whose owner shares the name
    if entered:
        near = [(nb, o) for nb in neighbor_barcodes(entered) for o in master.by_bc.get(nb, []) if owner_matches(q, o)]
        if len({nb for nb, o in near}) == 1:
            nb, o = near[0]
            out.update(status="CORRECTED", bc=nb, soft=True,
                       note=f"FOJ_ID {entered} not in guest list; one typo away from {nb} ({o.name}) - confirm")
            out["flags"] = note_flags(master.by_bc[nb])
            return out
    found = "name is on the guest list but has no barcode" if cands else "name not found in guest list"
    out.update(status="UNREGISTERED", bc="",
               note=f"FOJ_ID {entered or 'missing/invalid'}; {found} (temporary/unregistered - can't book)")
    return out

# =========================================================================== #
# Loading data
# =========================================================================== #

class Appt:
    pass


def load_ycbm(rows):
    out = {}
    for r in rows:
        ref = (r.get("Ref") or r.get("Id") or "").strip() or f"row{len(out)}"
        a = Appt()
        a.p = P(r.get("FNAME"), r.get("LNAME"), r.get("FOJ_ID"), [r.get("PHONE")], r.get("EMAIL"))
        a.start = parse_dt(r.get("Start"))
        a.status = (r.get("Status") or "").strip().upper()
        a.cancelled = (r.get("Cancelled") or "").strip().lower() == "true"
        a.made = parse_dt(r.get("Booking Made")) or datetime.min
        a.ref = ref
        a.profile = (r.get("Profile") or "").lower()
        a.volunteer = "volunteer" in a.profile
        a.phone_raw = (r.get("PHONE") or "").lstrip("'").strip()
        a.email_raw = (r.get("EMAIL") or "").strip()
        # barcode typed in first-name box, name typed in ID box -> swap back
        if re.fullmatch(r"\s*\d{5}\s*", r.get("FNAME") or "") and not a.p.bc and re.search(r"[A-Za-z]", r.get("FOJ_ID") or ""):
            a.p = P(r.get("FOJ_ID"), r.get("LNAME"), r.get("FNAME"), [r.get("PHONE")], r.get("EMAIL"))
        if a.start:
            out[ref] = a                    # later files win for the same booking
    return list(out.values())


class Visit:
    pass


def load_visits(rows, ignore_dates):
    out, seen = [], set()
    for r in rows:
        dt = parse_dt(r.get("Visit on"))
        if not dt or dt.date() in ignore_dates:
            continue
        v = Visit()
        v.dt = dt
        v.loc = (r.get("Visit Location") or "").strip()
        v.p = P(r.get("First Name"), r.get("Last Name"), r.get("Barcode"),
                [r.get("Home Phone"), r.get("Cell Phone")], r.get("Email"))
        sig = (dt, v.p.key, v.p.bc)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(v)
    return out

# =========================================================================== #
# No-show evaluation
# =========================================================================== #

def evaluate_attendance(accepted, visits, min_visits, ignore_dates):
    """Mark each accepted past appointment ATTENDED / NOSHOW_LIKELY / NOSHOW_VERIFY / SKIPPED."""
    by_date = defaultdict(list)
    for v in visits:
        by_date[v.dt.date()].append(v)
    covered = {d for d, vs in by_date.items() if len(vs) >= min_visits}
    claimed = set()

    for a in accepted:
        d = a.start.date()
        a.outcome, a.detail, a.matched_visit = None, "", None
        if d in ignore_dates:
            a.outcome, a.detail = "SKIPPED", "date ignored"
            continue
        if d not in covered:
            a.outcome, a.detail = "SKIPPED", "no visit data for this date"
            continue
        best = None
        for v in by_date[d]:
            s = match(a.id_p, v.p)
            if s and (best is None or RANK[s] > RANK[best[0]]):
                best = (s, v)
                if s == "barcode":
                    break
        if best and best[0] in CONFIRMED:
            a.outcome, a.matched_visit = "ATTENDED", best[1]
            a.detail = f"visit {best[1].dt:%I:%M %p} at {best[1].loc} (matched by {best[0]})".replace(" 0", " ")
            claimed.add(id(best[1]))
        elif best:
            a.outcome, a.matched_visit = "ATTENDED_VERIFY", best[1]
            a.detail = f"similar name visited {best[1].dt:%I:%M %p}: {best[1].p.name} [{best[1].p.bc or 'no barcode'}]"
            claimed.add(id(best[1]))

    # who visited that day without matching any appointment? (walk-ins / check-in mix-ups)
    for a in accepted:
        if a.outcome is not None:
            continue
        d = a.start.date()
        cands = []
        for v in by_date[d]:
            if id(v) in claimed:
                continue
            q, u = a.id_p, v.p
            near_in_time = abs((v.dt - a.start).total_seconds()) <= 3600     # check-in mistakes happen at their slot
            if q.bc and u.bc and bc_close(q.bc, u.bc) and near_in_time:
                cands.append(f"{u.name} [{u.bc}] checked in {v.dt:%I:%M %p} - barcode 1 digit off".replace(" 0", " "))
            elif (q.phones & u.phones) or (q.emails & u.emails):
                cands.append(f"{u.name} [{u.bc or 'no barcode'}] - same phone/email")
            elif len(q.key) >= 6 and name_sim(q.tokens, u.tokens) >= 0.8:
                cands.append(f"{u.name} [{u.bc or 'no barcode'}] - similar name")
        if cands:
            a.outcome = "NOSHOW_VERIFY"
            a.detail = "Possible check-in mix-up: " + "; ".join(cands[:2])
        else:
            a.outcome, a.detail = "NOSHOW_LIKELY", "No visit recorded that day"
    return covered


def build_month_table(accepted):
    """hkey -> month -> status ('noshow' | 'attended' | 'unconfirmed')"""
    per = defaultdict(lambda: defaultdict(list))
    for a in accepted:
        if a.outcome in (None, "SKIPPED"):
            continue
        per[a.id_p.hkey][month_of(a.start)].append(a.outcome)
    table = {}
    for h, months in per.items():
        table[h] = {}
        for m, outs in months.items():
            if any(o == "ATTENDED" for o in outs):
                table[h][m] = "attended"
            elif all(o == "NOSHOW_LIKELY" for o in outs):
                table[h][m] = "noshow"
            else:
                table[h][m] = "unconfirmed"
    return table


def streak(table, h, appt_month):
    """Consecutive confirmed no-show months ending at appt_month (or the month before it)."""
    t = table.get(h, {})
    cur = appt_month if appt_month in t else prev_month(appt_month)
    months = []
    while t.get(cur) == "noshow":
        months.append(cur)
        cur = prev_month(cur)
    return months[::-1], t.get(prev_month(appt_month)) == "unconfirmed" or t.get(appt_month) == "unconfirmed"

# =========================================================================== #
# Validator
# =========================================================================== #

def validate(pending, visits, table, as_of):
    visits_by_month = defaultdict(list)
    for v in visits:
        visits_by_month[month_of(v.dt)].append(v)

    # duplicates: keep the LATEST request per household per month
    groups = defaultdict(list)
    for a in pending:
        if not a.volunteer:
            groups[(a.id_p.hkey, month_of(a.start))].append(a)
    keep = {}
    for k, lst in groups.items():
        lst.sort(key=lambda a: (a.made, a.start))
        keep[k] = lst[-1]

    for a in pending:
        a.reasons, a.notes, a.soft = [], [], []
        idr = a.ident
        if a.volunteer:
            a.decision = "SKIP"
            a.reasons.append("Volunteer-shopping profile (not validated)")
            continue

        # ---- 1. FOJ_ID
        if idr["status"] == "UNREGISTERED":
            a.reasons.append("Not registered: " + idr["note"])
        elif idr["status"] in ("AMBIGUOUS", "MISMATCH"):
            a.soft.append("ID needs a human check: " + idr["note"])
        elif idr["status"] == "CORRECTED":
            (a.soft if idr["soft"] else a.notes).append("FOJ_ID fix: " + idr["note"])
        for f in idr["flags"]:
            if NOTE_DENY.search(f):
                a.reasons.append(f"Guest record note: \"{f}\"")
            else:
                a.soft.append(f"Guest record note: \"{f}\" - check before approving")

        # ---- 2. duplicate
        k = (a.id_p.hkey, month_of(a.start))
        kept = keep.get(k)
        if kept is not None and kept is not a:
            a.reasons.append(f"Duplicate request: keeping their latest request ({short_date(kept.start)} "
                             f"{fmt_time(kept.start)}, booked {kept.made:%b %d})".replace(" 0", " "))

        # ---- 3. already shopped this month
        m = month_of(a.start)
        hit = None
        for v in visits_by_month.get(m, []):
            s = match(a.id_p, v.p)
            if s in CONFIRMED:
                hit = v
                break
        if hit:
            a.reasons.append(f"Already shopped this month ({short_date(hit.dt)} at {hit.loc})")

        # ---- 4. no-show history
        months, unconf = streak(table, a.id_p.hkey, m)
        if len(months) >= 2:
            a.reasons.append("ON PENALTY: no-show " + " and ".join(month_label(x) for x in months[-2:]) + " (2 months in a row)")
        elif len(months) == 1:
            a.notes.append(f"WARNING: no-show in {month_label(months[0])} - may not show up")
        elif unconf:
            a.soft.append("Possible no-show last month is unconfirmed (check-in mix-up?) - not counted")

        if a.reasons:
            a.decision = "DENY"
        elif a.soft:
            a.decision = "REVIEW"
        else:
            a.decision = "APPROVE"

# =========================================================================== #
# CSV output
# =========================================================================== #

def slot_of(dt):
    return dt.replace(minute=(0 if dt.minute < 30 else 30), second=0)


def sorted_appts(lst):
    return sorted(lst, key=lambda a: (a.start.date(), slot_of(a.start), sort_name_key(a.p.first, a.p.last)))


def cap(x):
    x = (x or "").strip()
    return x.title() if x and (x == x.lower() or x == x.upper()) else x


def full_name(p):
    return f"{cap(p.first)} {cap(p.last)}".strip()


def date_txt(d):
    return f"{d:%a %b} {d.day}, {d.year}"


def bc_note(a):
    if a.ident["status"] == "CORRECTED":
        return f"Fixed - YCBM had {a.p.bc_raw or 'blank'}"
    if a.ident["status"] == "UNREGISTERED":
        return "No barcode found"
    return ""


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)


# ---------------------------------------------------------------- appointment sheet (approved only)
def appointment_sheet_csv(approved, path):
    """Approved appointments for ALL dates: a banner row per date, a blank row between dates."""
    by_date = defaultdict(list)
    for a in sorted_appts(approved):
        by_date[a.start.date()].append(a)
    rows = [["Date", "Appt Time", "Last Name", "First Name", "Barcode", "Notes"]]
    for n, d in enumerate(sorted(by_date)):
        if n:
            rows.append([])
        rows.append([f"{fmt_date(d).upper()}  -  {len(by_date[d])} appointment{'' if len(by_date[d]) == 1 else 's'}", "", "", "", "", ""])
        for a in by_date[d]:
            notes = [n_ for n_ in a.notes if "WARNING" in n_]
            if a.ident["status"] == "CORRECTED":
                notes.append(f"ID fixed (YCBM had {a.p.bc_raw or 'blank'})")
            rows.append([short_date(d), fmt_time(a.start), cap(a.p.last), cap(a.p.first),
                         a.ident["bc"], "; ".join(notes)])
    write_rows(path, rows)


# ---------------------------------------------------------------- decisions (DENY / APPROVE / REVIEW sections)
DEC_COLS = ["Decision", "Full Name", "Appointment Date", "Appointment Time", "Reason(s)", "Notes / Warnings",
            "Needs Human Check", "FOJ_ID Entered", "FOJ_ID Corrected", "Action", "YCBM Status", "Booked",
            "Booking Ref", "Email", "Phone"]
SECTIONS = [("DENY", "DENY"), ("APPROVE", "APPROVE"), ("REVIEW", "REVIEW - needs a human decision"),
            ("SKIP", "SKIPPED - volunteer-shopping profiles")]


def dec_action(a):
    bits = []
    if a.ident["status"] == "CORRECTED":
        bits.append(f"Update FOJ_ID in YCBM: {a.p.bc_raw or '(blank)'} -> {a.ident['bc']}")
    if a.decision == "DENY":
        bits.append("Decline in YCBM")
    elif a.decision == "REVIEW":
        bits.append("Check, then approve or decline")
    return "; ".join(bits)


def decisions_csv(appts, path):
    rows = []
    for key, title in SECTIONS:
        lst = sorted_appts([a for a in appts if a.decision == key])
        if not lst:
            continue
        if rows:
            rows.append([])
        rows.append([f"{title}  ({len(lst)})"])
        rows.append(DEC_COLS)
        for a in lst:
            rows.append([a.decision, full_name(a.p), date_txt(a.start.date()), fmt_time(a.start),
                         "; ".join(a.reasons), "; ".join(a.notes), "; ".join(a.soft), a.p.bc_raw,
                         a.ident["bc"] if a.ident["status"] != "UNREGISTERED" else "", dec_action(a), a.status.title(),
                         f"{a.made:%Y-%m-%d %H:%M}", a.ref, a.email_raw, a.phone_raw])
    write_rows(path, rows)


# ---------------------------------------------------------------- no-shows
def checklist_status(table, a):
    """Status for a no-show appointment, or '' if it shouldn't be contacted about."""
    if a.outcome == "NOSHOW_VERIFY":
        return "VERIFY FIRST - possible check-in mix-up (don't contact yet)"
    ms, _ = streak(table, a.id_p.hkey, month_of(a.start))
    if len(ms) >= 2:
        return "ON PENALTY - no-show 2 months in a row"
    if len(ms) == 1:
        return "WARNING - 1 no-show"
    return ""


def warnings_penalties_checklist(accepted, table, d_from, d_to, outdir):
    """One row per guest who missed an appointment in the date range. 'Done' = checkbox column in Sheets."""
    latest = {}
    for a in sorted_appts([a for a in accepted if d_from <= a.start.date() <= d_to
                           and a.outcome in ("NOSHOW_LIKELY", "NOSHOW_VERIFY")]):
        latest[a.id_p.hkey] = a                      # latest miss in the range wins
    def rank(st):
        return 0 if st.startswith("ON PENALTY") else (1 if st.startswith("WARNING") else 2)
    items = []
    for a in latest.values():
        st = checklist_status(table, a)
        if st:
            items.append((rank(st), a.start, sort_name_key(a.p.first, a.p.last), st, a))
    items.sort(key=lambda x: x[:3])
    rows = [["Done", "Status", "Full Name", "Barcode", "Barcode Note", "Missed Appointment", "No-show Months",
             "Details", "Email", "Phone", "Booking Ref"]]
    for _, _, _, st, a in items:
        months = ""
        if a.outcome == "NOSHOW_LIKELY":
            months = ", ".join(month_label(m) for m in streak(table, a.id_p.hkey, month_of(a.start))[0])
        rows.append(["FALSE", st, full_name(a.p), a.id_p.bc, bc_note(a),
                     f"{date_txt(a.start.date())} {fmt_time(a.start)}", months,
                     a.detail if a.outcome == "NOSHOW_VERIFY" else "", a.email_raw, a.phone_raw, a.ref])
    write_rows(os.path.join(outdir, "warnings_and_penalties.csv"), rows)
    n = lambda k: sum(1 for x in items if x[0] == k)
    return n(0), n(1), n(2)


# =========================================================================== #
# Main
# =========================================================================== #

def parse_dates(s):
    return {datetime.strptime(x.strip(), "%Y-%m-%d").date() for x in (s or "").split(",") if x.strip()}


def main():
    ap = argparse.ArgumentParser(description="FOJ appointment validator + no-show helper")
    ap.add_argument("files", nargs="*", help="CSV files (any order). Default: every CSV in this folder")
    ap.add_argument("--as-of", help="'Today' (YYYY-MM-DD). Default: date in the YCBM filename, else today")
    ap.add_argument("--ignore-dates", default="", help="Comma-separated dates whose visits/appointments are ignored")
    ap.add_argument("--from", dest="d_from", help="No-show checklist start date (default: 7 days before as-of)")
    ap.add_argument("--to", dest="d_to", help="No-show checklist end date (default: day before as-of)")
    ap.add_argument("--only", choices=["validate", "noshows"], help="Run just one half")
    ap.add_argument("--min-visits-per-day", type=int, default=5,
                    help="A date needs at least this many visits to be judged for no-shows (default 5)")
    ap.add_argument("--undated-month", help="Use undated visit lists, treating all rows as visits in this month (YYYY-MM)")
    ap.add_argument("--outdir", help="Output folder (default: foj_output_<as-of>)")
    args = ap.parse_args()

    paths = []
    for item in (args.files or ["."]):
        if os.path.isdir(item):
            paths += sorted(glob.glob(os.path.join(item, "*.csv")))     # a folder = every CSV inside it
        else:
            paths.append(item)
    ycbm_rows, visit_rows, guest_rows, undated = [], [], None, []
    ycbm_names = []
    for p in paths:
        try:
            rows = read_rows(p)
        except Exception as e:
            print(f"  skipped {p}: {e}")
            continue
        kind = detect_kind(rows)
        if kind == "ycbm":
            ycbm_rows += rows
            ycbm_names.append(p)
        elif kind == "visits":
            visit_rows += rows
        elif kind == "guests":
            if guest_rows is None or len(rows) > len(guest_rows):
                guest_rows = rows
        elif kind == "undated":
            undated.append((p, rows))
    if not ycbm_rows:
        sys.exit("No YCBM report found. Put the YCBM_report_*.csv in this folder (or list the files).")
    if not visit_rows:
        sys.exit("No dated visit export found (needs a 'Visit on' column).")

    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
    else:
        mm = None
        for n in ycbm_names:
            mm = re.search(r"(\d{4})_(\d{2})_(\d{2})", os.path.basename(n)) or mm
        as_of = date(*map(int, mm.groups())) if mm else date.today()
    ignore = parse_dates(args.ignore_dates)
    d_to = datetime.strptime(args.d_to, "%Y-%m-%d").date() if args.d_to else as_of - timedelta(days=1)
    d_from = datetime.strptime(args.d_from, "%Y-%m-%d").date() if args.d_from else d_to - timedelta(days=6)
    outdir = args.outdir or f"foj_output_{as_of:%Y-%m-%d}"
    os.makedirs(outdir, exist_ok=True)

    # ---- load
    appts = load_ycbm(ycbm_rows)
    visits = [v for v in load_visits(visit_rows, ignore) if v.dt.date() <= as_of]   # nothing after "today" counts
    notices = []
    for p, rows in undated:
        if args.undated_month:
            y, mo = map(int, args.undated_month.split("-"))
            for r in rows:
                v = Visit()
                v.dt = datetime(y, mo, 15)
                v.loc = "undated list"
                v.p = P(r.get("First Name"), r.get("Last Name"), r.get("Barcode"))
                visits.append(v)
            notices.append(f"Undated list {os.path.basename(p)} ({len(rows)} rows) counted as {args.undated_month} visits "
                           "(shopping check only; can't be used for no-shows)")
        else:
            notices.append(f"Undated list {os.path.basename(p)} ({len(rows)} rows) IGNORED - no dates. "
                           "Add visits to the dated export, or pass --undated-month")

    master = Master()
    for r in (guest_rows or []):
        master.add(P(r.get("First Name"), r.get("Last Name"), r.get("Barcode"),
                     [r.get("Home Phone"), r.get("Cell Phone")], r.get("Email")))
    for v in visits:
        if v.p.bc and v.p.bc not in master.by_bc:
            master.add(v.p)
    if guest_rows is None:
        notices.append("No guest list found - FOJ_ID checking is limited to barcodes seen in visits")

    # visits that have no barcode: borrow it from the guest list if the name is unambiguous
    for v in visits:
        if not v.p.bc and v.p.key:
            hits = {p.bc for s, p in master.find_name(v.p) if s == 1.0 and p.bc}
            if len(hits) == 1:
                v.p.bc = hits.pop()

    # ---- identity for every appointment
    cache = {}
    for a in appts:
        ck = (a.p.key, a.p.bc, tuple(sorted(a.p.phones)))
        if ck not in cache:
            cache[ck] = resolve_identity(a.p, master)
        a.ident = cache[ck]
        a.id_p = P(a.p.first, a.p.last, a.ident["bc"], a.p.phones, next(iter(a.p.emails), ""))
        a.id_p.phones, a.id_p.emails = a.p.phones, a.p.emails

    # ---- no-show evaluation on accepted past appointments
    accepted = [a for a in appts if not a.cancelled and a.status in ("FINISHED", "UPCOMING")
                and a.start.date() < as_of and not a.volunteer]
    covered = evaluate_attendance(accepted, visits, args.min_visits_per_day, ignore)
    table = build_month_table(accepted)
    skipped_dates = {a.start.date() for a in accepted if a.outcome == "SKIPPED"}

    print("=" * 74)
    print(f"FOJ TOOL   as-of {as_of}   ({len(appts)} YCBM rows, {len(visits)} visits, {len(master.people)} guests)")
    print("=" * 74)
    for n in notices:
        print("NOTE:", n)
    if ignore:
        print("Ignoring dates:", ", ".join(sorted(str(d) for d in ignore)))
    last_visit = max(v.dt.date() for v in visits)
    print(f"Visit data covers {min(v.dt.date() for v in visits)} to {last_visit}")

    # ---- validator
    if args.only != "noshows":
        pending = [a for a in appts if not a.cancelled and a.status not in ("REJECTED", "CANCELLED")
                   and a.start.date() > as_of]
        stale = [a for a in appts if not a.cancelled and a.status == "TENTATIVE" and a.start.date() <= as_of
                 and not a.volunteer]
        validate(pending, visits, table, as_of)
        for a in stale:
            a.decision, a.reasons, a.notes = "REVIEW", [], []
            a.soft = [f"Request is still TENTATIVE but its date ({short_date(a.start.date())}) has passed - accept/decline in YCBM"]
        allv = pending + stale
        approved = [a for a in pending if a.decision == "APPROVE"]
        appointment_sheet_csv(approved, os.path.join(outdir, "appointment_sheet.csv"))
        decisions_csv(allv, os.path.join(outdir, "decisions.csv"))
        c = defaultdict(int)
        for a in allv:
            c[a.decision] += 1
        print(f"\nAPPOINTMENT VALIDATOR: {len(pending)} upcoming requests (+{len(stale)} stale tentative)")
        print(f"  APPROVE {c['APPROVE']}   DENY {c['DENY']}   REVIEW {c['REVIEW']}   SKIP {c['SKIP']}")
        fixes = sum(1 for a in pending if a.ident["status"] == "CORRECTED")
        print(f"  FOJ_ID corrections suggested: {fixes}")
        reasons = defaultdict(int)
        for a in pending:
            for r in (a.reasons if a.decision == "DENY" else []):
                reasons[re.sub(r"\(.*|:.*", "", r).strip()] += 1
        for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    deny reason: {r}: {n}")
        if not pending:
            print("  (No requests after the as-of date in this YCBM export - export upcoming appointments"
                  " or set --as-of earlier.)")

    # ---- warnings & penalties
    if args.only != "validate":
        in_range = [a for a in accepted if d_from <= a.start.date() <= d_to]
        showed = sum(1 for a in in_range if a.outcome in ("ATTENDED", "ATTENDED_VERIFY"))
        pen, warn, ver = warnings_penalties_checklist(accepted, table, d_from, d_to, outdir)
        print(f"\nNO-SHOW CHECK {d_from} to {d_to}: {len(in_range)} appointments, {showed} showed up")
        print(f"  Checklist: {pen} ON PENALTY (2 months in a row), {warn} WARNING (1 no-show), "
              f"{ver} VERIFY FIRST (possible check-in mix-up)")
        if skipped_dates:
            print("  not evaluated (no data / ignored):", ", ".join(str(d) for d in sorted(skipped_dates)))

    print(f"\nFiles written to: {outdir}/")


if __name__ == "__main__":
    main()
