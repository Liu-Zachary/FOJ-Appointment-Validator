# FOJ-Appointment-Validator

Currently only a script; more likely to develop it into a web application as I progress in development. 


One Python script that replaces the old "YCBM & FBM Comparison Sheet" for a food pantry that books shopping appointments in **YouCanBookMe (YCBM)** and records visits in **FoodBank Manager (FBM)**.

It does two jobs from the same exports:

1. **Appointment Validator** – decides which upcoming appointment requests to **approve**, **deny**, or send to **review**, and produces a clean appointment sheet.
2. **No-Show Helper** – compares accepted appointments against visit records to find who didn't show up, and tracks **warnings** (1 no-show) and **penalties** (no-shows 2 months in a row).

No dependencies. Python 3.8+ standard library only.

---

## Quick start

```bash
# 1. Put these CSVs in one folder together with foj_tool.py
#    - YCBM report            (YCBM_report_*.csv)
#    - FBM visit export        (has a "Visit on" column)
#    - Guest list with barcodes (First/Last/Email/Home Phone/Cell Phone/Barcode)

# 2. Run it
python3 foj_tool.py
```

Results are written to a new folder named `foj_output_<date>/`.

Each CSV is meant to be imported into Google Sheets. In the import dialog, **uncheck "Convert text to numbers, dates, and formulas"** so barcodes like `09814` keep their leading zero.

---

## Outputs

| File | What it is |
|---|---|
| `decisions.csv` | Every request in sections: **DENY**, **APPROVE**, **REVIEW** (plus skipped volunteer profiles). Shows full name, appointment date and time, the reason(s), notes, the corrected barcode, and what to do in YCBM. |
| `appointment_sheet.csv` | **Approved appointments only**, for all upcoming dates. A banner row per date, times earliest → latest, names A → Z by last name. Guests with one prior no-show are approved with a "may not show up" note. |
| `warnings_and_penalties.csv` | Checklist of guests who missed an appointment in a date range. Statuses: `ON PENALTY`, `WARNING`, `VERIFY FIRST`. Has a **Done** column for tracking contact. |

**Making the Done column into checkboxes:** CSV can't store checkboxes. After importing to Sheets, select the Done column and choose **Insert → Checkbox**. The `FALSE` values become unchecked boxes.

---

## How decisions are made

A request is **approved only if every check passes**. If any hard check fails it is **denied** with every failing reason listed. Uncertain cases go to **review**.

1. **Guest-list notes.** Notes such as "no food til re-registered", "temporary card expired", "guest deceased", or "out of area" → **deny**. Softer notes ("moved", "check address") → review.
2. **Barcode (FOJ_ID) check.** The barcode typed in YCBM must belong to that name in the guest list.
   - Blank, placeholder (`00000`, `12345`), 4- or 6-digit, or mistyped barcodes are looked up by **name**, then **phone/email**, then single-digit typos and swaps, and the corrected barcode is suggested.
   - Clear matches are fixed automatically; uncertain ones go to review.
   - A guest who can't be found in the guest list is treated as not registered → **deny**.
3. **Duplicate requests.** If a household requests more than once in a calendar month, the **latest request is kept** and earlier ones are denied.
4. **Already shopped this month.** Any visit in the same calendar month, at any location, → **deny**. Resets on the 1st of each month.
5. **No-show history.**
   - No-shows in **2 months in a row** → **deny**, marked `ON PENALTY`.
   - **One** no-show → still approved, with a `WARNING` note on the appointment sheet.
   - Only *confirmed* no-shows count (see below).

### How no-shows are decided

An accepted appointment (YCBM status `FINISHED` or `UPCOMING`, not cancelled) is a no-show if no visit by that household is recorded on that date.

- Guests are matched by **barcode**, then **phone/email**, then **exact name**. A near-identical name is flagged to verify, not matched.
- To avoid penalizing guests who actually came (for example, a volunteer checked in the wrong person), a no-show is only `LIKELY` when no *unclaimed* visit near their slot looks like them (barcode one digit off, same phone/email, or a very similar name). Otherwise it is `VERIFY FIRST` and **does not count** toward warnings or penalties.
- Dates with fewer than 5 recorded visits (configurable) are skipped, since that usually means an export gap or a closed day.

---

## Running it

```bash
python3 foj_tool.py                                   # everything, defaults
python3 foj_tool.py history/ new_exports/             # give folders and/or files; history is merged
python3 foj_tool.py --as-of YYYY-MM-DD                # run as if today were this date
python3 foj_tool.py --from YYYY-MM-DD --to YYYY-MM-DD # warnings/penalties checklist range
python3 foj_tool.py --only validate                   # just the appointment validator
python3 foj_tool.py --only noshows                    # just the no-show helper
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `files...` | every CSV in the current folder | CSVs or folders, in any order. The tool detects each file's type from its columns. Overlapping exports are de-duplicated. |
| `--as-of DATE` | date in the YCBM filename, else today | "Today". Visits after this date are ignored. |
| `--from` / `--to` | 7 days before as-of → day before as-of | Date range for the warnings/penalties checklist. |
| `--only validate\|noshows` | both | Run just one half. |
| `--ignore-dates D1,D2` | none | Drop days with bad or partial visit data from the evidence. |
| `--min-visits-per-day N` | 5 | A date needs at least N recorded visits to be judged for no-shows. |
| `--undated-month YYYY-MM` | off | Count an undated visit list as visits in that month (shopping check only). |
| `--outdir PATH` | `foj_output_<as-of>` | Output folder. |

The dates above are placeholders. The tool works for any month or year, including across year-end (Nov + Dec no-shows → penalty for January).

### Input files

| Input | How it's recognized | Used for |
|---|---|---|
| YCBM report | columns `FNAME, LNAME, FOJ_ID, Start, Status` | Appointment requests and statuses |
| FBM visit export (dated) | has `Visit on` and `Barcode` | Who shopped, and when. The source of truth for no-shows. |
| Guest list | `First Name, Last Name, Barcode` plus `Email` / phone columns | Barcode checking and fixing, guest-list notes |
| Undated visit lists | only `First Name, Last Name, Barcode` | **Ignored** by default, because without a date the tool can't tell which day or month the visit happened |

You can run the no-show helper with only the YCBM report and the dated FBM export (`--only noshows`). Including the guest list makes it more accurate, because corrected barcodes catch guests who did attend.

### Keeping history

The no-show rule needs about two months of past YCBM and visit data. Either:

- **Rolling export** – each run, export YCBM and FBM from the 1st of last month through today; or
- **History folder** – keep every export in a folder and run `python3 foj_tool.py history/`.


