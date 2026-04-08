# IIIT Dharwad — Academic Timetable Scheduler

A constraint-based timetable scheduler for IIIT Dharwad that reads course, faculty, and room data from JSON and generates a conflict-free weekly timetable.

## Features

- **Dual Schema Support** — Automatically detects and handles both legacy (uppercase keys) and modern (lowercase/LTPC) JSON input formats.
- **Constraint Engine** — Faculty availability, room capacity, student group conflicts, cross-semester half detection, and daily session limits.
- **Elective Isolation** — Elective courses can share time slots with each other (students only pick one), but never overlap with core sessions.
- **Cross-Branch Teaching** — Same faculty teaching the same course to multiple branches simultaneously is handled correctly.
- **Priority Scheduling** — Core practicals are scheduled first (labs are scarce), then core lectures/tutorials, then electives.
- **Cross-Platform** — Works on Linux, macOS, and Windows (UTF-8 fix for emoji output).

## Quick Start

### Prerequisites

- Python 3.10+
- No external dependencies (standard library only)

### Run

```bash
python tti.py                      # uses input_even_sem.json by default
python tti.py input_even_sem.json  # explicit input file
```

### Output Files

| File | Description |
|------|-------------|
| `tt_out.json` | Full timetable with metadata, conflict breakdown, and all scheduled sessions |
| `tt_student.json` | Schedule reorganised by student group (branch + year) |
| `tt.html` | Self-contained interactive HTML viewer — open directly in a browser |

## Repository Structure

```
tti/
├── tti.py                  # Main scheduler (production code)
├── input_even_sem.json     # Current semester input data
├── input_large.json        # Large synthetic dataset for testing
├── .gitignore
│
├── TestCases/
│   ├── testcases.md        # Test case documentation with results
│   ├── TC01.json           # Student cohort constraints
│   ├── TC02.json           # Multi-section core conflict
│   ├── ...
│   └── TC15.json           # Stress benchmark (200+ courses)
│
└── Reports/
    ├── bug_report.md        # All 12 bugs fixed (B01–B12)
    ├── comparison_report.md # Side-by-side original vs current
    └── code_walkthrough.md  # Full code architecture documentation
```

## Input Format

The scheduler accepts JSON with the following structure. Both schemas are auto-detected:

**Modern Schema (recommended):**
```json
{
  "courses": [
    {
      "course_id": "COURSE_1",
      "course_code": "CS201",
      "course_name": "Data Structures",
      "semester": 3,
      "branch": "CSE",
      "section": null,
      "ltpc": "3-1-0-4",
      "faculty_id": "Dr. Smith",
      "is_elective": false,
      "num_students": 60,
      "semester_half": "Full"
    }
  ],
  "rooms": [
    { "room_id": "CR01", "capacity": 60 },
    { "room_id": "LAB01", "capacity": 30 }
  ]
}
```

**Legacy Schema (also supported):**
```json
{
  "Courses": [
    {
      "Course Code": "CS201",
      "Course Title": "Data Structures",
      "Semester": 3,
      "Branch": "CSE",
      "Lectures": 3,
      "Tutorials": 1,
      "Practicals": 0,
      "Faculty": "Dr. Smith",
      "Electives": "F",
      "Semester Half": "Full"
    }
  ],
  "Rooms": [
    { "Room": "CR01", "Seating Capacity": 60 }
  ]
}
```

### LTPC Format

The `ltpc` field encodes session counts as a single string: `"L-T-P-C"`.

| Component | Meaning | Duration per session |
|-----------|---------|---------------------|
| L | Lectures per week | 1.5 hours |
| T | Tutorials per week | 1.0 hour |
| P | Practicals per week | 2.0 hours |
| C | Credits | — |

Example: `"3-1-2-4"` → 3 lectures, 1 tutorial, 2 practicals, 4 credits.

## Scheduling Algorithm

The scheduler uses a **single-pass, greedy, constraint-based** approach (no backtracking):

1. **Load Data** — Parse JSON, resolve faculty IDs, detect schema.
2. **Build Slot Catalogue** — Generate ~138 candidate time slots across Mon–Sat.
3. **Sort Courses** — Core before elective → practical-heavy first → larger cohorts first.
4. **Place Sessions** — For each course, try every (slot, room) combination until a valid one is found.
5. **Constraint Checks** — Faculty busy? Room occupied? Students have a conflict? Daily limit exceeded? Duration mismatch?
6. **Export** — Write JSON outputs + standalone HTML viewer.

### Constraint Summary

| Constraint | Rule |
|------------|------|
| Faculty | A faculty member cannot be in two places at once (exception: cross-branch same course) |
| Room | A room cannot host two sessions at the same time |
| Students | A student group cannot have overlapping core sessions (electives may overlap each other) |
| Daily limit | Max 3 sessions of the same course per day; no same-type repeats |
| Duration | Slot duration must match session type (1.5h lecture, 1.0h tutorial, 2.0h practical) |
| Semester half | "Full" courses block both "1st half" and "2nd half" slots for their group |

## Testing

Run any test case:

```bash
python tti.py TestCases/TC01.json
python tti.py TestCases/TC15.json  # stress test
```

See `TestCases/testcases.md` for the full list of scenarios and expected outcomes.

## Documentation

| Document | Contents |
|----------|----------|
| [`Reports/bug_report.md`](Reports/bug_report.md) | All 12 bugs fixed with severity, original code, and fix description |
| [`Reports/comparison_report.md`](Reports/comparison_report.md) | Side-by-side diff between original and current version |
| [`Reports/code_walkthrough.md`](Reports/code_walkthrough.md) | Full architecture: data structures, methods, execution flow |
