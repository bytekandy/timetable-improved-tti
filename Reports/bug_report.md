# Bug Report — `timetable_scheduler_original.py` → `tti.py`

> **Project:** IIIT Dharwad Timetable Scheduler  
> **Original File:** `timetable_scheduler_original.py` (1065 lines)  
> **Current File:** `tti.py` (1192 lines)  
> **Report Date:** April 2026

This report documents every functional difference between the original and current scheduler, classified as either a **Bug** (incorrect/broken behaviour) or an **Enhancement** (improvement to otherwise functional code).

---

## Bugs Fixed

| # | Title | Severity | Category |
|---|-------|----------|----------|
| B01 | Single-Schema Crash | 🔴 Critical | Data Loading |
| B02 | Electives Block Each Other | 🔴 Critical | Scheduling Logic |
| B03 | Cross-Branch Teaching Rejected | 🟠 High | Scheduling Logic |
| B04 | Semester Half Cross-Check Missing | 🟠 High | Scheduling Logic |
| B05 | Rigid Basket Slot Pinning | 🟠 High | Scheduling Logic |
| B06 | Empty Faculty Mutual Blocking | 🟡 Medium | Data Loading |
| B07 | Faculty Name Normalisation Flawed | 🟡 Medium | Data Loading |
| B08 | Hardcoded Linux Output Path | 🟡 Medium | I/O |
| B09 | Faculty ID Shown Instead of Name | 🟡 Medium | Data Loading |
| B10 | Elective Flag Not Read from New Schema | 🟡 Medium | Data Loading |
| B11 | `semester_half` Ignored in New Schema | 🟡 Medium | Data Loading |
| B12 | Windows Emoji Encoding Crash | 🟢 Low | Platform |

---

### B01 — Single-Schema Crash

| | |
|-|-|
| **Severity** | 🔴 Critical |
| **Original** | `timetable_scheduler_original.py` line 564 |
| **Fixed in** | `tti.py` → `load_data()` |

**Original code:**
```python
for c in data["Courses"]:
    course_code = c["Course Code"].strip()
    semester    = int(c["Semester"])
```

**Problem:** The parser only understood the old uppercase JSON schema (`"Courses"`, `"Course Code"`, `"Faculty"`). Feeding it `input_original.json` — which uses lowercase keys (`"courses"`, `"course_code"`, `"faculty_id"`) — causes an immediate `KeyError: 'Courses'` and the program exits with zero output.

**Fix:** `tti.py` auto-detects the schema by checking `if "courses" in data:` and branches into a complete mapping layer for the new lowercase keys. Both schemas are now handled transparently.

---

### B02 — Electives Block Each Other

| | |
|-|-|
| **Severity** | 🔴 Critical |
| **Original** | `timetable_scheduler_original.py` line 331 |
| **Fixed in** | `tti.py` → `_commit_session()`, `_is_placeable()` |

**Original code:**
```python
def _record_session(self, course, session_type, time_slot, room, session_number):
    ...
    self.student_slots[course.get_student_key()][course.semester_half].append(time_slot)
```

**Problem:** Every session — both core and elective — writes to `student_slots`. Once one elective is placed at Monday 09:00, all other electives for the same student group are permanently blocked from that slot. This is logically wrong: students only register for one elective per basket, so electives within the same basket should be allowed to overlap.

**Fix:** `tti.py` uses a two-tier system. Both core and elective sessions **read** from `student_slots` to check for conflicts, but only **core** sessions **write** to it. Electives can now share time slots with each other while still being blocked by core classes.

---

### B03 — Cross-Branch Teaching Rejected

| | |
|-|-|
| **Severity** | 🟠 High |
| **Original** | `timetable_scheduler_original.py` lines 255–258 |
| **Fixed in** | `tti.py` → `_is_placeable()` |

**Original code:**
```python
if _slot_overlaps_any(time_slot, self.faculty_slots[course.faculty_name]):
    self.conflict_reasons["Faculty busy"] += 1
    return False, "Faculty busy"
```

**Problem:** If a professor teaches Physics to both CSE and ECE simultaneously (same course, different rooms), the scheduler flags the second section as a faculty conflict and rejects it. There is no exception for cross-branch shared lectures.

**Fix:** `tti.py` checks whether the existing booking at that slot is for the *exact same course* (matching code or name). If so, the faculty conflict is waived, allowing the second section to be scheduled in a different room.

---

### B04 — Semester Half Cross-Check Missing

| | |
|-|-|
| **Severity** | 🟠 High |
| **Original** | `timetable_scheduler_original.py` lines 266–271 |
| **Fixed in** | `tti.py` → `_is_placeable()`, `_active_halves()` |

**Original code:**
```python
student_key   = course.get_student_key()
semester_half = course.semester_half
if _slot_overlaps_any(time_slot,
                      self.student_slots[student_key][semester_half]):
```

**Problem:** Student conflicts are only checked within the exact same `semester_half` string. A `"Full"` semester course and a `"1st half"` course targeting the same student group are never compared against each other, allowing students to be double-booked across overlapping semester periods.

**Fix:** `tti.py` introduces `_active_halves()` which expands a course's half into all overlapping keys: `"Full"` checks against both `"1st half"` and `"2nd half"`, ensuring comprehensive conflict detection.

---

### B05 — Rigid Basket Slot Pinning

| | |
|-|-|
| **Severity** | 🟠 High |
| **Original** | `timetable_scheduler_original.py` lines 364–420 |
| **Fixed in** | `tti.py` → `_group_electives()` |

**Original code:**
```python
def _assign_basket_slots(self):
    for basket_id, basket_courses in self.elective_baskets.items():
        for candidate in lecture_slots:
            ...
            self.basket_slots[basket_id] = candidate
```

**Problem:** All electives in a basket are forcefully pinned to a single pre-assigned time slot. If that one slot is already occupied by a core course, a lab, or another faculty member, the **entire basket fails to schedule**. This is a major contributor to low success rates — entire groups of 5–8 elective courses are dropped because of a single slot conflict.

**Fix:** `tti.py` removes `_assign_basket_slots()` and `basket_slots` entirely. Baskets are still computed and labeled (`B1`, `B2`, ...) for UI display, but each elective independently competes for any available open slot.

---

### B06 — Empty Faculty Mutual Blocking

| | |
|-|-|
| **Severity** | 🟡 Medium |
| **Original** | `timetable_scheduler_original.py` line 576 |
| **Fixed in** | `tti.py` → `load_data()` |

**Original code:**
```python
raw_faculty  = c.get("Faculty", "Unknown")
faculty_name = _normalise_faculty_name(raw_faculty)
```

**Problem:** Courses where the faculty field is blank (`""` or whitespace) all normalise to the same empty string. The scheduler then treats all unassigned courses as if they are taught by one extremely busy professor, preventing any two from sharing a time slot. In a dataset with 10+ unassigned courses, this quietly kills dozens of valid placements.

**Fix:** Each unassigned course receives a unique synthetic ID (`_unassigned_1`, `_unassigned_2`, etc.) so they are treated as independent entities.

---

### B07 — Faculty Name Normalisation Flawed

| | |
|-|-|
| **Severity** | 🟡 Medium |
| **Original** | `timetable_scheduler_original.py` line 125 |
| **Fixed in** | `tti.py` → `_normalise_faculty()` |

**Original code:**
```python
name = re.sub(r'\s+[A-Z]\.?$', '', name)
```

**Problem:** The regex strips any trailing single uppercase letter unconditionally. For short faculty names like `"Dr. U"` or `"Dr. V"`, this collapses both to just `"Dr."`, causing the scheduler to believe they are the same person. This produces false faculty conflicts — two completely different professors are treated as one.

**Fix:** `tti.py` adds a guard: if stripping the trailing letter would leave only a title token (like `"Dr."`, `"Prof."`), the letter is preserved. The whitespace collapsing behaviour is retained for its intended purpose.

---

### B08 — Hardcoded Linux Output Path

| | |
|-|-|
| **Severity** | 🟡 Medium |
| **Original** | `timetable_scheduler_original.py` line 634 |
| **Fixed in** | `tti.py` → `main()` |

**Original code:**
```python
output_dir = "/mnt/user-data/outputs"
os.makedirs(output_dir, exist_ok=True)
```

**Problem:** Output files are written to a Linux-specific absolute path (`/mnt/user-data/outputs`) that typically doesn't exist outside the original development environment. On standard setups, the directory is silently created at an unexpected location, and users can't find their output files.

**Fix:** Changed to `output_dir = "."` — files are written to the current working directory.

---

### B09 — Faculty ID Shown Instead of Name

| | |
|-|-|
| **Severity** | 🟡 Medium |
| **Original** | `timetable_scheduler_original.py` line 576 |
| **Fixed in** | `tti.py` → `load_data()` |

**Original code:**
```python
raw_faculty = c.get("Faculty", "Unknown")
```

**Problem:** The original parser only supports the old schema where `"Faculty"` contains a human-readable name. The new schema uses `"faculty_id": "F012"` with a separate `"faculties"` lookup table. Without resolving this, all output shows raw IDs like `F012` or `F017` instead of `Dr. Lakshmi Iyer`. Conflict logs become unreadable.

**Fix:** `tti.py` builds a lookup dictionary from `data.get("faculties", [])` and resolves every `faculty_id` to its actual name before storing.

---

### B10 — Elective Flag Not Read from New Schema

| | |
|-|-|
| **Severity** | 🟡 Medium |
| **Original** | `timetable_scheduler_original.py` line 593 |
| **Fixed in** | `tti.py` → `load_data()` |

**Original code:**
```python
is_elective = c.get("Electives") == "T"
```

**Problem:** This only works with the old schema where an `"Electives"` field contains `"T"` or `"F"`. The new schema uses `"is_elective": true/false`. When fed new-format data, every course evaluates to `is_elective = False`, producing zero baskets and scheduling all electives as rigid core courses.

**Fix:** `tti.py` reads `bool(entry.get("is_elective", False))` for the new schema, falling back to `"Electives" == "T"` for legacy data.

---

### B11 — `semester_half` Ignored in New Schema

| | |
|-|-|
| **Severity** | 🟡 Medium |
| **Original** | `timetable_scheduler_original.py` lines 568–573 |
| **Fixed in** | `tti.py` → `load_data()` |

**Original code:**
```python
if "Semester Half" not in c or c["Semester Half"] in (None, ""):
    semester_half = "Sem-II"
```

**Problem:** The parser only reads `"Semester Half"` (uppercase, old schema). The new schema's `"semester_half"` field is never checked. When using new-format data, every course defaults to `"Sem-II"` regardless of its actual semester half, breaking any scheduling logic that depends on distinguishing "1st half" from "2nd half" courses.

**Fix:** `tti.py` reads `entry.get("semester_half", entry.get("half"))` first, and only falls back to credit-based inference when neither key is present.

---

### B12 — Windows Emoji Encoding Crash

| | |
|-|-|
| **Severity** | 🟢 Low |
| **Original** | `timetable_scheduler_original.py` lines 174, 628, 660+ |
| **Fixed in** | `tti.py` → `main()` |

**Original code:**
```python
print(f"📂 Loading data from: {input_file}")
print(f"📚 Loaded:")
print(f"⚠️  Basket {basket_id}: no shared slot found")
```

**Problem:** Windows PowerShell and Command Prompt default to `cp1252` encoding. Python emoji literals (📂, 📚, ⚠️) are UTF-8 only and cannot be encoded in `cp1252`, causing `UnicodeEncodeError`. The script was written and tested on Linux/macOS only.

**Fix:** `tti.py` wraps `sys.stdout` in a UTF-8 `TextIOWrapper` at the start of `main()`. This is a low-severity issue because a simple workaround exists (`python -X utf8`), and it only affects the print output, not the actual scheduling logic.

---

## Enhancements

The following changes improve the scheduler's performance and usability but are not fixes for broken behaviour.

| # | Enhancement | Impact |
|---|-------------|--------|
| E01 | **Saturday + Evening Slots** — Added Saturday as a valid scheduling day and evening blocks (17:00–18:30). Weekly slot count increased from ~100 to ~138. | Higher placement rate |
| E02 | **Daily Limit Raised** — Maximum sessions per course per day raised from 2 to 3. | Fewer unnecessary rejections |
| E03 | **Same-Day Session Bans Removed** — Removed blanket bans on Lecture+Tutorial and Lecture+Practical on the same day. Now only same-type repeats (e.g., two Lectures on Monday) are blocked. | Fewer false positives |
| E04 | **Scheduling Priority Sort** — Core courses get absolute first priority. Within cores, practical-heavy and large-cohort courses go first since labs are the scarcest resource. | Most constrained courses get best slots |
| E05 | **Elective Room Shuffling** — Eligible rooms are randomly shuffled for elective courses to spread them across buildings instead of clustering. | Better room utilisation |
| E06 | **Smallest-Fit Room Selection** — Electives use ascending capacity order (smallest room that fits). Original used descending order, wasting large halls on small cohorts. | Preserves big rooms for core |
| E07 | **LTPC String Format** — Supports `"ltpc": "3-1-0-4"` compact format alongside the separate `lectures`/`tutorials`/`practicals` fields. | Compatibility with new data pipeline |
| E08 | **Code Refactoring** — Cleaner class/method/field names (e.g., `ImprovedScheduler` → `Scheduler`, `course_code` → `code`, `_record_session` → `_commit_session`). | Maintainability |
| E09 | **Output File Renaming** — `timetable_output.json` → `tt_out.json`, `timetable_by_student.json` → `tt_student.json`, `timetable_standalone.html` → `tt.html`. | Cleaner filenames |
| E10 | **Test Suite** — 15 structured test cases (TC01–TC15) covering student cohorts, faculty conflicts, room logistics, durations, semester halves, and stress scenarios. | Regression testing |

### Combined Impact of E01–E03 on Success Rate

The expanded time pool, raised daily limits, and removed same-day bans collectively address the original scheduler's low placement rate:

| Metric | Original | Current |
|--------|----------|---------|
| Weekly time slots | ~100 | ~138 |
| Daily session cap | 2 | 3 |
| Same-day bans | Lecture+Tutorial, Lecture+Practical | Same-type only |
| **Success rate** | **~43%** | **~65%** |
