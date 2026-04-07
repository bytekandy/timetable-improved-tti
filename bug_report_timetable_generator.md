# 🐛 Bug Report — `time_table_generator`

> **Project:** IIIT Dharwad Timetable Scheduler  
> **Main Script:** `timetable_scheduler_changed.py`  
> **Report Date:** April 2026  
> **Total Bugs Found:** 7

---

## Summary Table

| Bug # | Title | File | Severity |
|-------|-------|------|----------|
| B01 | JSON Schema Incompatibility — `KeyError: 'Courses'` | `timetable_scheduler_changed.py` | 🔴 Critical |
| B02 | `UnicodeEncodeError` — Windows Emoji Crash | `timetable_scheduler_changed.py` | 🔴 Critical |
| B03 | Hardcoded Linux Output Path | `timetable_scheduler_changed.py` | 🟠 High |
| B04 | Faculty ID Not Resolved to Name | `timetable_scheduler_changed.py` | 🟡 Medium |
| B05 | Elective Detection by Keyword is Fragile | `timetable_scheduler_changed.py` | 🟡 Medium |
| B06 | README Commands Don't Work on Windows | `README.md` | 🟢 Low |
| B07 | Low Scheduling Success Rate on Large Dataset | `timetable_scheduler_changed.py` | 🟠 High |

---

## Detailed Bug Reports

---

### B01 — JSON Schema Incompatibility

| Field | Details |
|-------|---------|
| **File** | `timetable_scheduler_changed.py` |
| **Line (original)** | Line 564 inside `load_data()` |
| **Type** | Crash Bug |
| **Severity** | 🔴 Critical |

**Steps to Reproduce:**
```bash
python timetable_scheduler_changed.py input_large.json
```

**Actual Behavior:**
```
KeyError: 'Courses'
```
Script crashes immediately and produces no output.

**Expected Behavior:**
Script detects the schema format automatically and loads data correctly.

**Root Cause:**
The `load_data()` function hardcoded `data["Courses"]` and `data["Rooms"]`, but `input_large.json` uses entirely different key names:

```python
# Original (broken) code
for c in data["Courses"]:          # ❌ Crashes with input_large.json
    course_code = c["Course Code"] # ❌ Key doesn't exist
```

`input_large.json` uses lowercase keys: `"courses"`, `"course_id"`, `"course_name"` — completely incompatible with the original implementation. The script has no mechanism to detect or adapt to different input formats.

---

### B02 — `UnicodeEncodeError` Windows Emoji Crash

| Field | Details |
|-------|---------|
| **File** | `timetable_scheduler_changed.py` |
| **Line (original)** | Line 628, 176, 390, 476, 477, 479, 660 |
| **Type** | Platform Compatibility Bug |
| **Severity** | 🔴 Critical |

**Steps to Reproduce:**
```powershell
# Run on any Windows machine with default terminal settings
python timetable_scheduler_changed.py input.json
```

**Actual Behavior:**
```
UnicodeEncodeError: 'charmap' codec can't encode character '\U0001f4c2'
in position 0: character maps to <undefined>
```

**Expected Behavior:**
Script runs normally, printing emoji characters to the console.

**Root Cause:**
Windows PowerShell and Command Prompt default to `cp1252` encoding. Python emoji literals (📂, 📚, ✓, ⚠️, 📊) are UTF-8 only and cannot be encoded in `cp1252`.

```python
# Multiple print statements like this crash on Windows:
print(f"📂 Loading data from: {input_file}")  # ❌ Crashes on Windows
print(f"📚 Loaded:")                           # ❌ Crashes on Windows
print(f"⚠️  Basket {basket_id}: no shared slot found")  # ❌ Crashes on Windows
```

The script was clearly written and tested on Linux/macOS only.

---

### B03 — Hardcoded Linux Output Path

| Field | Details |
|-------|---------|
| **File** | `timetable_scheduler_changed.py` |
| **Line (original)** | Line 634 inside `main()` |
| **Type** | Environment Bug |
| **Severity** | 🟠 High |

**Steps to Reproduce:**
```bash
# Run on Windows
python timetable_scheduler_changed.py input.json
```

**Actual Behavior:**
Output files are either:
- Not found in the project directory
- Written to `C:\mnt\user-data\outputs\` (an unexpected path)

**Expected Behavior:**
Output files (`timetable_output.json`, `timetable_by_student.json`, `timetable_standalone.html`) should be saved in the same folder as the script.

**Root Cause:**
```python
# Original code
output_dir = "/mnt/user-data/outputs"   # ❌ Linux absolute path
```
This is a Linux-specific mount path (likely from a cloud or Docker environment). It does not exist on Windows and silently redirects outputs to an unexpected location.

---

### B04 — Faculty ID Not Resolved to Name

| Field | Details |
|-------|---------|
| **File** | `timetable_scheduler_changed.py` |
| **Line** | `load_data()` — new schema branch |
| **Type** | Data Mapping Bug |
| **Severity** | 🟡 Medium |

**Steps to Reproduce:**
```bash
python timetable_scheduler_changed.py input_large.json
# Observe conflict output
```

**Actual Behavior:**
```
• ECE037 (ECE  Sem-I) - Practical #1 - Faculty: F012
• CSE002 (CSE  Sem-I) - Practical #1 - Faculty: F015
```
Conflicts and schedule entries show opaque faculty IDs (`F012`, `F015`) that are meaningless to the end user.

**Expected Behavior:**
Should show readable faculty names:
```
• ECE037 (ECE  Sem-I) - Practical #1 - Faculty: Dr. Lakshmi Iyer
```

**Root Cause:**
`input_large.json` contains a `"faculties"` lookup table mapping each ID to a name. The `load_data()` function reads `faculty_id` from course entries but never cross-references this lookup table:
```python
# Bug: uses the raw ID as the faculty name
raw_faculty = c.get("faculty_id", "Unknown")  # ❌ Stores "F012", not "Dr. Lakshmi Iyer"
faculty_name = _normalise_faculty_name(raw_faculty)
```
The `"faculties"` array in the JSON is completely ignored.

---

### B05 — Elective Detection by Keyword is Fragile

| Field | Details |
|-------|---------|
| **File** | `timetable_scheduler_changed.py` |
| **Line** | `load_data()` — new schema branch, elective detection line |
| **Type** | Logic Bug |
| **Severity** | 🟡 Medium |

**Steps to Reproduce:**
Run with `input_large.json`. Observe that no elective baskets are detected (output shows `Elective baskets detected: 0`).

**Actual Behavior:**
`is_elective = False` for all 96 courses. No baskets are formed.

**Expected Behavior:**
Elective courses should be correctly tagged and grouped into baskets.

**Root Cause:**
```python
# Fragile keyword matching
is_elective = "Elective" in course_title or "elective" in course_title.lower()
```
This approach fails because:
1. `input_large.json` courses have titles like `"Topics in Signals"`, `"Modern VLSI I"`, `"Research in Machine Learning I"` — none contain the word "elective"
2. A course like `"Introduction to Reinforcement Learning"` would **not** be detected as an elective even if it was intended to be one
3. The correct approach is to use a dedicated flag field in the data (like the old schema's `"Electives": "T"`)

---

### B06 — README Commands Don't Work on Windows

| Field | Details |
|-------|---------|
| **File** | `README.md` |
| **Lines** | 22–29, 349–365 |
| **Type** | Documentation Bug |
| **Severity** | 🟢 Low |

**Steps to Reproduce:**
Follow the Quick Start guide in `README.md` on Windows.

**Actual Behavior:**
```
'python3' is not recognized as an internal or external command
'open' is not recognized as an internal or external command
```

**Expected Behavior:**
Commands in the README should work on all supported platforms.

**Root Cause:**
The README exclusively uses macOS/Linux conventions:
```bash
# README says (macOS/Linux only):
python3 timetable_scheduler_improved.py input.json   # ❌ Windows uses 'python'
open timetable_standalone.html                        # ❌ Windows uses 'start' or 'Start-Process'
nano input.json                                       # ❌ Windows doesn't have 'nano'
cat timetable_output.json | grep "total_sessions"    # ❌ Windows doesn't have 'cat' or 'grep' in cmd
```
No Windows alternatives are documented anywhere in the README.

---

### B07 — Low Scheduling Success Rate on Large Datasets

| Field | Details |
|-------|---------|
| **File** | `timetable_scheduler_changed.py` — scheduling algorithm |
| **Lines** | `generate_timetable()`, `_generate_time_slots()`, `_check_constraints()` |
| **Type** | Algorithmic Limitation |
| **Severity** | 🟠 High |

**Steps to Reproduce:**
```bash
python timetable_scheduler_changed.py input_large.json
```

**Actual Behavior:**
```
Sessions scheduled: 187
Unscheduled       : 247
Success rate      : 43.1%
```
Only 43.1% of required sessions are placed. 247 sessions are permanently unscheduled.

**Expected Behavior:**
A production timetable scheduler should achieve **>80% placement rate** on real-world institutional data.

**Root Cause — Multiple contributing factors:**

1. **Only 20 time slots/week** — The system hardcodes 4 lecture slots × 5 days = 20 slots. With 96 courses needing 3–6 sessions each (~400+ total sessions needed), there are simply not enough slots available.

2. **Overlapping slot windows** — Lecture (1.5h), tutorial (1h), and practical (2h) slots all start at similar times (e.g., all at 09:00), creating heavy contention for the same room/faculty at peak hours.

3. **Over-strict daily constraints** — The rule "no lecture + tutorial on the same day AND no lecture + practical on same day" blocks many valid combinations:
```python
# This rejects valid scheduling pairs:
if {session_type, et} == {SessionType.LECTURE, SessionType.TUTORIAL}:
    return False, "Already has a theory session today"
if {session_type, et} == {SessionType.LECTURE, SessionType.PRACTICAL}:
    return False, "Already has lecture+practical today"
```

4. **No retry or backtracking** — If a session fails to schedule, it is simply logged as a conflict. There is no backtracking mechanism to free up a previously-allocated slot that might enable a better overall solution.

---

## Comparison: `time_table_generator` vs `Automated-TimeTable`

| Feature | `time_table_generator` | `Automated-TimeTable` |
|---------|------------------------|----------------------|
| **Output Format** | JSON + HTML viewer | Excel (.xlsx) files |
| **Web Interface** | Static HTML only | Full Flask web app |
| **Input Format** | JSON | CSV + Excel |
| **Elective Baskets** | ✅ Supported | ✅ Supported (B1–B4) |
| **Exam Scheduling** | ❌ Not supported | ✅ `scheduler_1.py` |
| **Teacher Timetable** | ❌ Not supported | ✅ `teacher_timetables.xlsx` |
| **Cross-Platform** | ❌ Linux only (bugs on Windows) | ✅ Works on Windows |
| **Schema Flexibility** | ❌ One schema only (original) | ✅ CSV is more universal |
| **Reproducibility** | ✅ Deterministic output | ❌ Random slot selection |
| **Faculty Name Display** | ✅ (old schema), ❌ (new schema) | ✅ Direct from CSV |

> [!NOTE]
> This comparison demonstrates that `time_table_generator` has a simpler but less robust architecture, while `Automated-TimeTable` handles more real-world scenarios. The bugs above represent concrete gaps that limit the system's usability in a production college environment.
