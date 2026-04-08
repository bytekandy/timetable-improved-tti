# Code Walkthrough — `tti.py`

> **Project:** IIIT Dharwad Timetable Scheduler  
> **Module:** `tti.py`  
> **Last Updated:** April 2026

This document provides a complete technical walkthrough of the `tti.py` module — covering every data structure, function, method, and the step-by-step execution flow from CLI invocation to HTML output generation.

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [Imports & Dependencies](#2-imports--dependencies)
3. [Data Structures](#3-data-structures)
4. [Utility Functions](#4-utility-functions)
5. [The Scheduler Class](#5-the-scheduler-class)
6. [Data Loading — `load_data()`](#6-data-loading--load_data)
7. [HTML Generation — `_write_html()`](#7-html-generation--_write_html)
8. [CLI Entry Point — `main()`](#8-cli-entry-point--main)
9. [End-to-End Execution Flow](#9-end-to-end-execution-flow)

---

## 1. High-Level Architecture

```
┌──────────────┐     ┌──────────────┐     ┌───────────────────┐
│  input.json  │────▶│  load_data() │────▶│  List[Course]     │
│              │     │              │     │  List[Room]        │
└──────────────┘     └──────────────┘     └────────┬──────────┘
                                                   │
                                                   ▼
                                          ┌────────────────┐
                                          │   Scheduler    │
                                          │  ┌───────────┐ │
                                          │  │ _build_   │ │
                                          │  │  slots()  │ │
                                          │  └───────────┘ │
                                          │  ┌───────────┐ │
                                          │  │ run()     │ │
                                          │  └───────────┘ │
                                          └───────┬────────┘
                                                  │
                              ┌────────────────────┼────────────────────┐
                              ▼                    ▼                    ▼
                      ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
                      │ tt_out.json  │    │tt_student.json│   │   tt.html    │
                      └──────────────┘    └──────────────┘    └──────────────┘
```

The scheduler is a **single-pass, constraint-based, greedy algorithm**. It does not backtrack. Courses are sorted by priority (core before elective, lab-heavy before lecture-only), and each session is placed into the first valid (slot, room) combination found.

---

## 2. Imports & Dependencies

| Module | Purpose |
|--------|---------|
| `json` | Reading input JSON, writing output JSON |
| `os` | File path operations, directory creation |
| `re` | Regular expressions for faculty name normalisation |
| `sys` | CLI argument parsing, stdout encoding control |
| `io` | `TextIOWrapper` for UTF-8 stdout override on Windows |
| `random` | Shuffling room order for elective placement |
| `datetime.time` | Representing time-of-day values in `TimeSlot` |
| `typing` | Type hints (`List`, `Dict`, `Optional`) |
| `dataclasses` | `@dataclass` decorator for structured data |
| `enum.Enum` | `SessionType` enumeration |
| `collections.defaultdict` | Auto-initialising nested dictionaries for booking registries |
| `collections.Counter` | Tallying conflict reasons |

> **Zero external dependencies.** The module runs on Python 3.10+ standard library only.

---

## 3. Data Structures

### 3.1 `SessionType` (Enum)

```python
class SessionType(Enum):
    LECTURE   = "Lecture"
    TUTORIAL  = "Tutorial"
    PRACTICAL = "Practical"
```

Identifies the three types of academic sessions. Each type maps to a fixed duration:

| Type | Duration | Room Requirement |
|------|----------|------------------|
| `LECTURE` | 1.5 hours | Large hall or classroom |
| `TUTORIAL` | 1.0 hour | Classroom |
| `PRACTICAL` | 2.0 hours | Lab only (`L*` or `LAB*` prefix) |

---

### 3.2 `TimeSlot` (dataclass)

```python
@dataclass
class TimeSlot:
    day:            str       # e.g. "Monday"
    start_time:     time      # e.g. time(9, 0)
    end_time:       time      # e.g. time(10, 30)
    duration_hours: float     # e.g. 1.5
```

Represents a named block of time on a specific weekday. The key method is:

- **`overlaps(other)`** — Returns `True` when two slots share any time on the same day, using proper interval math (`self.start < other.end and other.start < self.end`). This was a critical fix (AK01) replacing naive equality checks.

`TimeSlot` is hashable (`__hash__`, `__eq__` defined) so it can be used in sets and as dictionary keys.

---

### 3.3 `Room` (dataclass)

```python
@dataclass
class Room:
    id:       str   # e.g. "CR01", "LAB01", "HALL01"
    capacity: int   # e.g. 100
```

A physical room on campus. Room classification is done by naming convention at runtime:
- `id.startswith("L")` or `"LAB" in id.upper()` → classified as a **lab**
- `capacity >= 100` → classified as a **large room**
- Everything else → **classroom**

---

### 3.4 `Course` (dataclass)

```python
@dataclass
class Course:
    id:           str        # Internal unique ID ("COURSE_1", "COURSE_2", ...)
    code:         str        # e.g. "CS206"
    name:         str        # e.g. "Data Structures"
    semester:     int        # e.g. 3
    half:         str        # "Full", "1st half", or "2nd half"
    branch:       str        # e.g. "CSE"
    section:      Optional[str]  # e.g. "A" or None
    lectures:     int        # Number of lecture sessions per week
    tutorials:    int        # Number of tutorial sessions per week
    practicals:   int        # Number of practical sessions per week
    faculty:      str        # Normalised faculty name
    is_elective:  bool       # True if elective, False if core
    num_students: int        # Class strength for room capacity matching
    basket:       Optional[str]  # Elective basket label ("B1", "B2", ...) or None
```

**Key method:**

- **`student_group_key()`** — Returns a string like `"CSE_Yr2_A"` (or `"CSE_Yr2"` without section). This key determines which group of students share a timetable, ensuring conflicts between courses targeting the same group are detected.

---

### 3.5 `ScheduledSession` (dataclass)

```python
@dataclass
class ScheduledSession:
    course:         Course
    session_type:   SessionType
    slot:           TimeSlot
    room:           Room
    faculty:        str
    session_number: int              # e.g. Lecture #1, Lecture #2
    basket:         Optional[str]    # Inherited from Course
```

Represents a single successfully-placed session in the final timetable. One `Course` with `lectures=3` will produce three `ScheduledSession` objects (Lecture #1, #2, #3), each potentially in a different slot and room.

---

## 4. Utility Functions

### 4.1 `_overlaps_any(slot, booked)`

```python
def _overlaps_any(slot: TimeSlot, booked: List[TimeSlot]) -> bool
```

Returns `True` if the given `slot` overlaps with **any** slot in the `booked` list. Used throughout the constraint checker to test faculty, room, and student availability.

---

### 4.2 `_normalise_faculty(raw)`

```python
def _normalise_faculty(raw: str) -> str
```

**Purpose:** Ensure that the same professor isn't treated as two different people due to inconsistent data entry.

**Steps:**
1. Strip leading/trailing whitespace and collapse multiple spaces to single space.
2. Check if the last word is a single uppercase letter (a trailing initial like `"L"` in `"Dr. Shirshendu L"`).
3. If so, strip it — **unless** stripping would leave only a title (e.g., `"Dr."`, `"Prof."`). This guard was added by test case TC03.

**Examples:**
| Input | Output |
|-------|--------|
| `"  Dr. Newton  "` | `"Dr. Newton"` |
| `"Dr. Shirshendu L"` | `"Dr. Shirshendu"` |
| `"Dr. U"` | `"Dr. U"` (preserved — TC03 fix) |

---

## 5. The Scheduler Class

### 5.1 Constructor — `__init__()`

When `Scheduler(courses, rooms)` is called, the following happens:

1. **Booking Registries Initialised:**

   | Registry | Type | Purpose |
   |----------|------|---------|
   | `faculty_slots` | `Dict[str, List[TimeSlot]]` | Tracks which time slots each faculty member is booked into |
   | `room_slots` | `Dict[str, List[TimeSlot]]` | Tracks which time slots each room is occupied |
   | `student_slots` | `Dict[str, Dict[str, List[TimeSlot]]]` | Tracks which time slots each student group is booked into, keyed by `(group_key, half)`. **Only core courses write here** (AK02). |
   | `sessions_today` | `Dict[str, Dict[str, List[SessionType]]]` | Tracks which session types a course already has on a given day (`course.id → day → [SessionType, ...]`) |

2. **Time Slot Catalogue Built** via `_build_slots()`.
3. **Room Pools Classified** into `large_rooms`, `classrooms`, `labs`, and `non_lab_rooms`.
4. **Elective Groups Computed** via `_group_electives()`.
5. **Summary Printed** to the terminal.

---

### 5.2 `_build_slots()` — Time Catalogue

Generates ~**23 candidate slots per day × 6 days = ~138 weekly slots** across three time blocks:

| Block | Hours | Slot Types Available |
|-------|-------|----------------------|
| Morning | 09:00 – 12:30 | 4 × 1.5h lectures, 5 × 1.0h tutorials, 4 × 2.0h practicals |
| Afternoon | 14:00 – 16:30 | 2 × 1.5h lectures, 4 × 1.0h tutorials, 2 × 2.0h practicals |
| Evening | 17:00 – 18:30 | 1 × 1.5h lecture, 2 × 1.0h tutorials |

Multiple overlapping start times are intentional — the overlap checker in `_is_placeable()` enforces that no two sessions for the same resource actually overlap.

Slots are pre-indexed by duration in `slots_by_duration` for O(1) lookup during placement.

---

### 5.3 `_group_electives()` — Basket Formation

Groups all elective courses by `(semester, branch)`. Each unique group receives a basket label (`B1`, `B2`, ...). This label is attached to each `Course.basket` field for display in the HTML viewer.

> **Important (AK03):** Baskets are purely cosmetic grouping labels. They do **not** enforce slot pinning — each elective schedules independently.

---

### 5.4 `_eligible_rooms()` — Room Selection

Returns the set of rooms a given course+session_type can use, filtered by capacity:

| Scenario | Room Pool | Sort Order |
|----------|-----------|------------|
| **Practical** | `self.labs` only | Natural order |
| **Elective (lecture/tutorial)** | `self.non_lab_rooms` | Ascending capacity (smallest first — AK08) |
| **Core (lecture/tutorial)** | `self.large_rooms + self.classrooms` | Descending capacity (biggest first) |

---

### 5.5 `_active_halves()` — Semester Half Expansion

Converts a course's `half` string into the list of half-keys that should be checked for student conflicts:

| Course Half | Keys Checked |
|-------------|-------------|
| `"Full"` | `["1st half", "2nd half"]` |
| `"1st half"` | `["1st half"]` |
| `"2nd half"` | `["2nd half"]` |

This ensures a "Full" course correctly blocks both halves of the semester for its student group.

---

### 5.6 `_is_placeable()` — The Constraint Engine

The heart of the scheduler. Checks whether placing a session at a specific `(slot, room)` violates any hard constraint. Returns `(True, "OK")` or `(False, reason_string)`.

**Constraints checked in order:**

#### Constraint 1: Faculty Availability
- Iterates through all slots already booked by this faculty.
- If overlap detected, checks the **cross-branch exception** (AK07): if the existing booking is for the *exact same course* (matching code or name), the conflict is waived.
- Otherwise → `"Faculty busy"`.

#### Constraint 2: Room Availability
- Checks if the room is already occupied at the proposed slot.
- Otherwise → `"Room occupied"`.

#### Constraint 3: Student Group Availability
- Looks up the student group key and expands the course's semester half into all relevant half-keys (AK06).
- Checks `student_slots` for overlaps.
- **Key insight (AK02):** Both core and elective sessions **read** this registry, but only core sessions **write** to it. This means two electives can share a slot, but an elective cannot overlap a core session.
- Otherwise → `"Students busy"` or `"Students busy (elective vs core clash)"`.

#### Constraint 4: Daily Session Limit
- At most **3 sessions** of the same course on any given day (AK09).
- No repeat of the **same session type** on the same day (e.g., two Lectures on Monday is blocked).
- Otherwise → `"Max 3 sessions/day"` or `"Lecture already today"`.

#### Constraint 5: Duration Match
- The slot's `duration_hours` must exactly match the required duration for the session type (1.5h / 1.0h / 2.0h).
- Otherwise → `"Wrong slot duration"`.

---

### 5.7 `_commit_session()` — Booking a Session

Once `_is_placeable()` returns `True`, this method:

1. Creates a `ScheduledSession` object and appends it to `self.schedule`.
2. Registers the slot in `faculty_slots[faculty]`.
3. Registers the slot in `room_slots[room.id]`.
4. **Only if core:** Registers the slot in `student_slots[group_key][half_key]` for each active half.
5. Records the session type in `sessions_today[course.id][day]`.

---

### 5.8 `_try_place()` — Placement Attempt

Attempts to place **one** session of a given type for a course:

1. Gets eligible rooms via `_eligible_rooms()`.
2. Gets candidate slots of the correct duration from `slots_by_duration`.
3. For electives, **shuffles** the room order randomly to spread them across buildings.
4. Iterates all `(slot, room)` combinations; on the first `_is_placeable() == True`, calls `_commit_session()` and returns `True`.
5. If no combination works, returns `False`.

---

### 5.9 `_schedule_course()` — Course-Level Scheduling

Schedules **all** sessions for a single course. Builds a plan:
```
[(LECTURE, count), (TUTORIAL, count), (PRACTICAL, count)]
```
For each session in the plan, calls `_try_place()`. If placement fails, a human-readable conflict description is appended to `self.unscheduled`.

Returns the count of successfully placed sessions.

---

### 5.10 `run()` — The Main Scheduling Pass

This is the top-level orchestrator:

1. **Sorts courses** by scheduling priority (AK08):
   - Core before elective
   - By semester half, then semester number
   - Practical-heavy courses first (labs are scarce)
   - Larger classes first

2. **Iterates** through the sorted list, calling `_schedule_course()` for each.

3. **Prints** a summary: total scheduled, unscheduled, and conflict breakdown.

4. **Returns** `self.to_dict()`.

---

### 5.11 `to_dict()` — JSON Serialisation

Converts the schedule into a JSON-compatible dictionary with two top-level keys:

- **`metadata`** — Aggregate statistics: `total_sessions`, `total_conflicts`, `total_courses`, `elective_baskets`, `conflict_breakdown`.
- **`schedule`** — Array of session objects, each containing course code, title, day, times, room, faculty, etc.
- **`conflicts`** — Array of human-readable strings describing each unscheduled session.

---

### 5.12 `by_student_group()` — Student View Export

Reorganises the schedule by student group key (e.g., `"CSE_Yr2"`). Each group's sessions are sorted by day order (Monday → Friday) then by time. This powers the "Student View" tab in the HTML output.

---

## 6. Data Loading — `load_data()`

```python
def load_data(filepath: str) -> tuple[List[Course], List[Room]]
```

Parses a JSON input file and returns `(courses, rooms)`.

### Schema Detection (AS01)

The function auto-detects the JSON schema:

| Check | Schema Type | Key Style |
|-------|-------------|-----------|
| `"courses" in data` | **New schema** | Lowercase: `course_code`, `course_name`, `ltpc`, `faculty_id` |
| Otherwise | **Old schema** | Uppercase: `Course Code`, `Course Title`, `Lectures`, `Faculty` |

### New Schema Parsing Pipeline

1. **Faculty Lookup Table (KM01):** Builds a `dict` from `data["faculties"]` mapping IDs → names.
2. **LTPC Parsing:** Reads `"ltpc": "3-1-0-4"` string, splits by `"-"`, extracts `lectures`, `tutorials`, `practicals`, `credits`.
3. **Elective Flag (KM02):** Reads `bool(entry.get("is_elective", False))` directly.
4. **Semester Half (TC14):** First tries `entry.get("semester_half")`, then falls back to credit-based inference.
5. **Empty Faculty (AK05):** If faculty string is blank, assigns `_unassigned_{idx}`.
6. **Normalisation (AK04):** Passes faculty through `_normalise_faculty()`.

### Room Parsing

Reads the `"rooms"` (or `"Rooms"`) array. Each room entry requires `room_id` (or `Room ID`) and `capacity` (or `Capacity`).

---

## 7. HTML Generation — `_write_html()`

```python
def _write_html(timetable: Dict, by_student: Dict, filepath: str) -> None
```

Generates a **self-contained, standalone HTML file** with embedded CSS and JavaScript. No external dependencies.

### Features:
- **Stats Dashboard:** Animated counters showing total sessions, success rate, elective groups, and unscheduled count.
- **Student View Tab:** Dropdown selector for student groups. Renders a day-by-day timetable grid.
- **Elective Baskets Tab:** Displays all baskets with their shared time slots and course choices.
- **Export Tab:** One-click download buttons for JSON and CSV exports.
- **Dark Theme:** Modern glassmorphism UI with CSS custom properties.

### Data Injection:
The timetable and student-group dictionaries are serialised as JSON and injected directly into `<script>` tags using Python f-string interpolation with doubled braces (`{{` / `}}`).

---

## 8. CLI Entry Point — `main()`

```python
def main():
```

### Execution Steps:

1. **UTF-8 Fix (AS02):** If stdout encoding isn't UTF-8, wraps it with `io.TextIOWrapper`.
2. **Input File:** Takes `sys.argv[1]` or defaults to `"input_even_sem.json"`.
3. **Load Data:** Calls `load_data()` → `(courses, rooms)`.
4. **Run Scheduler (KM04):** Instantiates `Scheduler(courses, rooms)` and calls `.run()`.
5. **Export Student View:** Calls `scheduler.by_student_group()`.
6. **Write Files (AS03):** Writes to current directory:
   - `tt_out.json` — Full timetable with metadata
   - `tt_student.json` — Student-group organised schedule
   - `tt.html` — Standalone interactive HTML viewer
7. **Print Summary:** Courses, scheduled, unscheduled, success rate, elective groups.
8. **Print Conflicts:** First 15 unscheduled sessions with `…and N more` if truncated.

---

## 9. End-to-End Execution Flow

Below is the complete step-by-step path from command line to output files:

```
$ python3 tti.py input_even_sem.json
```

```
1.  main() called
    │
    ├── 1a. Force UTF-8 stdout (AS02)
    ├── 1b. Read CLI argument → "input_even_sem.json"
    │
    ├── 2. load_data("input_even_sem.json")
    │   ├── 2a. json.load() → raw dict
    │   ├── 2b. Detect schema (new vs old)  (AS01)
    │   ├── 2c. Build faculty lookup table  (KM01)
    │   ├── 2d. For each course entry:
    │   │   ├── Parse LTPC string → L, T, P, C
    │   │   ├── Read is_elective flag       (KM02)
    │   │   ├── Read semester_half          (TC14)
    │   │   ├── Handle empty faculty        (AK05)
    │   │   ├── Normalise faculty name      (AK04)
    │   │   └── Construct Course object
    │   ├── 2e. Parse rooms → List[Room]
    │   └── 2f. Return (courses, rooms)
    │
    ├── 3. Scheduler.__init__(courses, rooms)
    │   ├── 3a. Initialise booking registries (empty dicts)
    │   ├── 3b. _build_slots() → 138+ weekly TimeSlots
    │   ├── 3c. Index slots by duration
    │   ├── 3d. Classify rooms → large_rooms, classrooms, labs
    │   ├── 3e. _group_electives() → basket labels (AK03)
    │   └── 3f. Print loaded summary
    │
    ├── 4. scheduler.run()
    │   ├── 4a. Sort courses by priority     (AK08)
    │   ├── 4b. For each course (sorted):
    │   │   └── _schedule_course(course)
    │   │       └── For each (type, count) in plan:
    │   │           └── _try_place(course, type, n)
    │   │               ├── Get eligible rooms
    │   │               ├── Get candidate slots by duration
    │   │               ├── For each (slot, room):
    │   │               │   └── _is_placeable(course, type, slot, room)
    │   │               │       ├── Check faculty     (AK07 exception)
    │   │               │       ├── Check room
    │   │               │       ├── Check students    (AK02, AK06)
    │   │               │       ├── Check daily limit (AK09)
    │   │               │       └── Check duration    (AK01)
    │   │               │
    │   │               ├── If OK → _commit_session() → return True
    │   │               └── If none → return False → log unscheduled
    │   │
    │   ├── 4c. Print conflict breakdown
    │   └── 4d. Return to_dict()
    │
    ├── 5. scheduler.by_student_group()
    │   └── Reorganise schedule by student group key
    │
    ├── 6. Write output files (AS03)
    │   ├── tt_out.json      ← json.dump(timetable)
    │   ├── tt_student.json  ← json.dump(by_student)
    │   └── tt.html          ← _write_html(timetable, by_student)
    │
    └── 7. Print final summary + first 15 unscheduled
```
