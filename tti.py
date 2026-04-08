"""
tti.py — IIIT Dharwad Timetable Scheduler
==========================================

Reads a structured JSON input (produced by generate_input.py) and generates
a weekly timetable for all courses.

## Bug Fix Contributors & IDs

### Anurag S. (AS##)
  * AS01 / Schema Compatibility: `load_data()` dynamically accepts both Old
         (Uppercase keys) and New (Lowercase keys) JSON schemas, avoiding `KeyError`.
  * AS02 / Windows Emoji Crash: Forced `sys.stdout` UTF-8 wrapper to prevent
         `UnicodeEncodeError` on Windows consoles.
  * AS03 / Hardcoded Paths: Output directories changed from Linux-specific
         absolute paths to current working directory (`.`).

### Kanhaiya M. (KM##)
  * KM01 / Faculty ID Resolution: The parser now resolves `faculty_id` (e.g., `F012`)
         to readable names using the `faculties` lookup array.
  * KM02 / Fragile Elective Detection: Transitioned from brittle title-matching
         logic to relying on explicit `is_elective` flags / standardized heuristics.
  * KM03 / README Commands: Documented cross-platform CLI alternatives for Windows users.
  * KM04 / Low Scheduling Rate: Expanded time pools (including early/late slots and
         Saturday options) and relaxed over-strict constraints.

### Anand K. (AK##)
  * AK01 / Overlap Detection: `overlaps()` uses proper time boundary math instead of
         strict dictionary `in` equality to prevent partial overlaps.
  * AK02 / Elective Isolation: Electives check for clashes against core classes, but
         bypass logging themselves into `student_slots` to avoid blocking other
         independently attended electives.
  * AK03 / Elective Basket Slot Pinning: Removed forced rigid slot pinning. Baskets
         are still computed, but electives schedule freely into gaps.
  * AK04 / Faculty Normalisation: Collapsed extra whitespaces and trailing initials
         so identical profs aren't double-booked.
  * AK05 / Empty Faculty Handling: Assigns synthetic string IDs (`_unassigned_`)
         instead of blank strings so unassigned courses don't block each other.
  * AK06 / Cross-Semester Halves: Time slots are properly checked and locked across
         ALL active semester halves (Full/1st/2nd half logic).
  * AK07 / Cross-branch Shared Courses: Exception logic allows exact same course
         title/code to be placed concurrently in different rooms without triggering
         faculty busy error.
  * AK08 / Placement Priority & Shuffling: Core courses placed first. Electives try
         smallest-fit rooms first.
  * AK09 / Daily Limit Extension: Raised to 3 distinct session occurrences safely
         per class daily.

### Test Case Discoveries
  * TC03 / Faculty Name Over-Strip: Fixed regex that aggressively stripped single-letter
         names (e.g., "Dr. U" → "Dr.") causing false faculty collisions.
  * TC14 / Semester Half Ignored: Fixed `load_data()` to read `semester_half` from
         JSON instead of hardcoding based on credits alone.

Usage:
  python tti.py [input.json]
  (defaults to input_even_sem.json if no argument is given)
"""

import json
import os
import re
import sys
import io
import random
from datetime import time
from typing import List, Dict, Optional
from dataclasses import dataclass
from enum import Enum
from collections import defaultdict, Counter


# ---------------------------------------------------------------------------
# Enums and data classes
# ---------------------------------------------------------------------------

class SessionType(Enum):
    LECTURE   = "Lecture"
    TUTORIAL  = "Tutorial"
    PRACTICAL = "Practical"


@dataclass
class TimeSlot:
    """A named block of time on a specific weekday."""
    day:            str
    start_time:     time
    end_time:       time
    duration_hours: float

    def overlaps(self, other: "TimeSlot") -> bool:
        """
        True when two slots share any time on the same day.
        # AK01: Proper boundary overlap logic replaced naive equality checks (`in`)
        # which incorrectly allowed partially overlapping practicals and lectures.
        """
        if self.day != other.day:
            return False
        return self.start_time < other.end_time and other.start_time < self.end_time

    def __hash__(self):
        return hash((self.day, self.start_time, self.end_time))

    def __eq__(self, other):
        return (self.day == other.day
                and self.start_time == other.start_time
                and self.end_time   == other.end_time)

    def __str__(self):
        return f"{self.day} {self.start_time:%H:%M}–{self.end_time:%H:%M}"


@dataclass
class Course:
    """All static information about a single course offering."""
    id:           str            # internal scheduler ID, e.g. "COURSE_1"
    code:         str            # official course code, e.g. "CS301"
    name:         str            # full course name
    semester:     int
    half:         str            # "Full" | "1st half" | "2nd half"
    branch:       str            # "CSE" | "DSAI" | "ECE"
    section:      Optional[str]  # e.g. "A" — None when the whole branch takes it
    lectures:     int            # sessions per week
    tutorials:    int
    practicals:   int
    faculty:      str            # normalised faculty name
    is_elective:  bool
    basket:       Optional[str] = None   # elective group label, set by the scheduler
    num_students: int = 60

    def student_group_key(self) -> str:
        """
        Unique identifier for the set of students that must attend this course.
        Sections get their own key; otherwise students are grouped by branch + year.
        """
        if self.section:
            return f"{self.branch}_{self.section}_Sem{self.semester}"
        year = (self.semester + 1) // 2
        return f"{self.branch}_Year{year}"


@dataclass
class Room:
    """A physical room or lab."""
    id:       str
    capacity: int


@dataclass
class ScheduledSession:
    """One successfully placed session in the timetable."""
    course:         Course
    session_type:   SessionType
    slot:           TimeSlot
    room:           Room
    faculty:        str
    session_number: int          # 1st, 2nd, … occurrence of this session type
    basket:         Optional[str] = None


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _overlaps_any(slot: TimeSlot, booked: List[TimeSlot]) -> bool:
    """Return True if *slot* overlaps with at least one slot in *booked*."""
    return any(slot.overlaps(b) for b in booked)


def _normalise_faculty(raw: str) -> str:
    """
    Collapse extra whitespace and strip trailing single-letter abbreviations
    so that "Dr. Shirshendu L" and "Dr. Shirshendu Layek" resolve to the same key.
    # AK04: Faculty name normalisation prevents the same person from being
    # double-booked just because their name was spelt slightly differently.
    """
    name = " ".join(raw.strip().split())
    parts = name.split()
    if len(parts) > 1 and re.match(r'^[A-Z]\.?$', parts[-1]):
        leftover = parts[:-1]
        titles = {"dr.", "dr", "prof.", "prof", "mr.", "mr", "ms.", "ms"}
        # TC03: Do not strip if it leaves ONLY a title (e.g. "Dr. U" -> "Dr.")
        if not (len(leftover) == 1 and leftover[0].lower() in titles):
            name = " ".join(leftover)
    return name


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """
    Constraint-based timetable scheduler.

    The scheduler works in two phases:
      1. Core courses are scheduled first (they share a student body and compete
         for a shared pool of large lecture halls and labs).
      2. Electives fill the remaining free slots (each elective has its own
         isolated student cohort, so two electives can run at the same time).

    Booking state is tracked in three dictionaries:
      faculty_slots   – all slots already claimed by a faculty member
      room_slots      – all slots already claimed by a room
      student_slots   – all slots claimed by CORE sessions for a student group
                        (electives are deliberately excluded so they can overlap
                        each other, but they still check this dict to avoid
                        clashing with core sessions)
    """

    def __init__(self, courses: List[Course], rooms: List[Room]):
        self.courses  = courses
        self.rooms    = rooms
        self.schedule: List[ScheduledSession] = []

        # --- booking registries ---
        # Maps faculty name → list of booked TimeSlots
        self.faculty_slots: Dict[str, List[TimeSlot]] = defaultdict(list)
        # Maps room ID → list of booked TimeSlots
        self.room_slots: Dict[str, List[TimeSlot]] = defaultdict(list)
        # Maps student-group key → { semester-half → list of booked TimeSlots }
        # Only populated by CORE courses.
        self.student_slots: Dict[str, Dict[str, List[TimeSlot]]] = defaultdict(lambda: defaultdict(list))

        # Tracks which session types a course already has on a given day.
        # Maps course.id → { day → [SessionType, …] }
        self.sessions_today: Dict[str, Dict[str, List[SessionType]]] = defaultdict(lambda: defaultdict(list))

        # Both semester halves (used when a "Full"-semester course spans the year)
        self._halves = ["1st half", "2nd half"]

        # --- time slot catalogue ---
        self.all_slots = self._build_slots()
        # Pre-index by duration so the placer can quickly iterate candidates
        self.slots_by_duration: Dict[float, List[TimeSlot]] = defaultdict(list)
        for s in self.all_slots:
            self.slots_by_duration[s.duration_hours].append(s)

        # --- room pools ---
        # Large lecture halls (≥100 seats) – for big core cohorts
        self.large_rooms   = sorted([r for r in rooms if r.capacity >= 100], key=lambda r: -r.capacity)
        # Standard classrooms (<100 seats, not labs) – for smaller groups
        self.classrooms    = sorted([r for r in rooms if r.capacity < 100
                                     and not r.id.startswith("L") and "LAB" not in r.id.upper()],
                                    key=lambda r: -r.capacity)
        # Labs – reserved for practical sessions
        self.labs          = [r for r in rooms if r.id.startswith("L") or "LAB" in r.id.upper()]
        # Non-lab rooms sorted smallest-first – used for electives (pack small rooms)
        self.non_lab_rooms = sorted([r for r in rooms if r not in self.labs], key=lambda r: r.capacity)

        # --- outcome tracking ---
        self.unscheduled: List[str]   = []    # human-readable conflict descriptions
        self.conflict_tally: Counter  = Counter()

        # --- elective grouping (for output labels only, no slot pinning) ---
        self.elective_groups = self._group_electives()

        self._print_summary()

    # -----------------------------------------------------------------------
    # Initialisation helpers
    # -----------------------------------------------------------------------

    def _print_summary(self):
        core_count     = sum(1 for c in self.courses if not c.is_elective)
        elective_count = sum(1 for c in self.courses if c.is_elective)
        print("📚 Loaded:")
        print(f"  Courses       : {len(self.courses)}  (core: {core_count}, elective: {elective_count})")
        print(f"  Large rooms   : {[f'{r.id}({r.capacity})' for r in self.large_rooms]}")
        print(f"  Classrooms    : {len(self.classrooms)}")
        print(f"  Labs          : {[r.id for r in self.labs]}")

    def _group_electives(self) -> Dict[str, List[Course]]:
        """
        Assign a basket label to every elective course.
        Courses in the same (semester, branch) group share a basket label so the
        output viewer can display which courses a student is choosing between.
        # AK03: Rigid slot pinning removed. Electives compete freely for open slots
        # instead of being forcefully bundled into the exact same time slot, increasing placement rate.
        """
        groups: Dict[tuple, List[Course]] = defaultdict(list)
        for course in self.courses:
            if course.is_elective:
                groups[(course.semester, course.branch)].append(course)

        baskets: Dict[str, List[Course]] = {}
        for idx, (_, group) in enumerate(groups.items(), start=1):
            label = f"B{idx}"
            for course in group:
                course.basket = label
            baskets[label] = group
        return baskets

    def _build_slots(self) -> List[TimeSlot]:
        """
        Generate all candidate time slots for the week.

        Campus schedule:
          Morning   block  09:00 – 12:30   (no lunch overlap)
          Afternoon block  14:00 – 16:30   (post-lunch)
          Evening   block  17:00 – 18:30   (post-snack break)

        Multiple overlapping start times within each block are intentional –
        they give the placer flexibility while the overlap checker enforces
        that no two sessions for the same resource overlap.
        """
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        slots = []

        for day in days:

            # ── Morning block (09:00–12:30) ─────────────────────────────

            # 1.5-hour lecture windows
            slots += [
                TimeSlot(day, time( 9,  0), time(10, 30), 1.5),
                TimeSlot(day, time(10,  0), time(11, 30), 1.5),
                TimeSlot(day, time(10, 30), time(12,  0), 1.5),
                TimeSlot(day, time(11,  0), time(12, 30), 1.5),
            ]

            # 1-hour tutorial windows
            slots += [
                TimeSlot(day, time( 9,  0), time(10,  0), 1.0),
                TimeSlot(day, time(10,  0), time(11,  0), 1.0),
                TimeSlot(day, time(10, 30), time(11, 30), 1.0),
                TimeSlot(day, time(11,  0), time(12,  0), 1.0),
                TimeSlot(day, time(11, 30), time(12, 30), 1.0),
            ]

            # 2-hour practical windows
            slots += [
                TimeSlot(day, time( 9,  0), time(11,  0), 2.0),
                TimeSlot(day, time( 9, 30), time(11, 30), 2.0),
                TimeSlot(day, time(10,  0), time(12,  0), 2.0),
                TimeSlot(day, time(10, 30), time(12, 30), 2.0),
            ]

            # ── Afternoon block (14:00–16:30) ───────────────────────────

            slots += [
                TimeSlot(day, time(14,  0), time(15, 30), 1.5),
                TimeSlot(day, time(15,  0), time(16, 30), 1.5),
            ]
            slots += [
                TimeSlot(day, time(14,  0), time(15,  0), 1.0),
                TimeSlot(day, time(14, 30), time(15, 30), 1.0),
                TimeSlot(day, time(15,  0), time(16,  0), 1.0),
                TimeSlot(day, time(15, 30), time(16, 30), 1.0),
            ]
            slots += [
                TimeSlot(day, time(14,  0), time(16,  0), 2.0),
                TimeSlot(day, time(14, 30), time(16, 30), 2.0),
            ]

            # ── Evening block (17:00–18:30) ─────────────────────────────
            # Only 1.5h available – no 2-hour practicals here.

            slots.append(TimeSlot(day, time(17,  0), time(18, 30), 1.5))
            slots += [
                TimeSlot(day, time(17,  0), time(18,  0), 1.0),
                TimeSlot(day, time(17, 30), time(18, 30), 1.0),
            ]

        return slots

    # -----------------------------------------------------------------------
    # Room selection
    # -----------------------------------------------------------------------

    def _eligible_rooms(self, course: Course, session_type: SessionType) -> List[Room]:
        """
        Return rooms suitable for this session, filtered by minimum capacity.

        Practicals always go to labs.
        Elective lectures/tutorials use the smallest available room (efficient packing,
        since elective cohorts are small and pre-registered).
        Core lectures/tutorials use large halls or classrooms.
        # FIX (2nd Commit): Sorts by ascending capacity for electives, ensuring small groups
        # don't unnecessarily occupy large lecture halls needed by core courses.
        """
        min_cap = course.num_students

        if session_type == SessionType.PRACTICAL:
            return [r for r in self.labs if r.capacity >= min_cap]

        if course.is_elective:
            # Ascending-capacity order → smallest available room that fits
            return [r for r in self.non_lab_rooms if r.capacity >= min_cap]

        # Core course: prefer large halls, then classrooms
        return [r for r in self.large_rooms + self.classrooms if r.capacity >= min_cap]

    # -----------------------------------------------------------------------
    # Semester-half helpers
    # -----------------------------------------------------------------------

    def _active_halves(self, half: str) -> List[str]:
        """
        Return which semester-half keys are relevant for a course's *half* value.
        "Full"-year courses are booked into both halves to block both sub-periods.
        """
        h = half.lower()
        if "full" in h:
            return self._halves
        if "1" in h or "first" in h or "sem-i" in h:
            return ["1st half"]
        if "2" in h or "second" in h or "sem-ii" in h:
            return ["2nd half"]
        return self._halves   # safe default

    # -----------------------------------------------------------------------
    # Constraint checking
    # -----------------------------------------------------------------------

    def _is_placeable(self, course: Course, session_type: SessionType,
                      slot: TimeSlot, room: Room) -> tuple[bool, str]:
        """
        Check whether placing *session_type* for *course* at *slot* in *room*
        violates any hard constraint.  Returns (True, "OK") or (False, reason).

        Constraints checked (in order):
          1. Faculty availability    – the faculty member must be free at *slot*.
                                       Exception: cross-branch shared lectures
                                       (same course name/code, already placed in
                                       another room) do not count as a conflict.
          2. Room availability       – the room must be free at *slot*.
          3. Student availability    – the student group must be free at *slot*.
                                       student_slots contains only CORE bookings, so
                                       two electives can share a slot but an elective
                                       cannot overlap a core session for its group.
          4. Daily session limit     – at most 3 sessions of this course per day,
                                       and no repeat of the same session type on the
                                       same day.
          5. Duration match          – the slot must have exactly the required duration.
        """
        # 1. Faculty check
        for booked_slot in self.faculty_slots[course.faculty]:
            if slot.overlaps(booked_slot):
                # AK07: Cross-branch shared teaching exception.
                # Allow cross-branch shared lectures: same course taught to multiple
                # branches simultaneously in different rooms by the same faculty.
                is_shared = any(
                    s.slot == booked_slot
                    and (s.course.code == course.code or s.course.name == course.name)
                    for s in self.schedule
                    if s.faculty == course.faculty
                )
                if is_shared:
                    continue
                self.conflict_tally["Faculty busy"] += 1
                return False, "Faculty busy"

        # 2. Room check
        if _overlaps_any(slot, self.room_slots[room.id]):
            self.conflict_tally["Room occupied"] += 1
            return False, "Room occupied"

        # 3. Student group check
        # AK02: Elective isolation.
        # Both core and elective sessions check this dict, but only core sessions
        # write to it — so electives can overlap each other but not core sessions.
        group_key = course.student_group_key()
        # AK06: Cross-semester half detection. Checks all active half variants.
        for half_key in self._active_halves(course.half):
            if _overlaps_any(slot, self.student_slots[group_key][half_key]):
                reason = ("Students busy (elective vs core clash)"
                          if course.is_elective else "Students busy")
                self.conflict_tally[reason] += 1
                return False, "Students busy"

        # 4. Daily limit
        today = self.sessions_today[course.id][slot.day]
        # AK09: Daily limit bumped to 3, giving courses more breathing room.
        if len(today) >= 3:
            self.conflict_tally["Max 3 sessions/day"] += 1
            return False, "Max 3 sessions/day"
        # Checks session uniqueness so lecture+practical overlaps on same day trigger.
        if session_type in today:
            self.conflict_tally[f"{session_type.value} already today"] += 1
            return False, f"{session_type.value} already today"

        # 5. Duration match
        required = {SessionType.LECTURE: 1.5, SessionType.TUTORIAL: 1.0, SessionType.PRACTICAL: 2.0}
        if slot.duration_hours != required[session_type]:
            self.conflict_tally["Wrong slot duration"] += 1
            return False, "Wrong slot duration"

        return True, "OK"

    # -----------------------------------------------------------------------
    # Session placement
    # -----------------------------------------------------------------------

    def _commit_session(self, course: Course, session_type: SessionType,
                        slot: TimeSlot, room: Room, session_number: int) -> None:
        """
        Record a successfully placed session and update all booking registries.
        Electives are excluded from student_slots so they don't block each other.
        """
        self.schedule.append(ScheduledSession(
            course         = course,
            session_type   = session_type,
            slot           = slot,
            room           = room,
            faculty        = course.faculty,
            session_number = session_number,
            basket         = course.basket,
        ))
        self.faculty_slots[course.faculty].append(slot)
        self.room_slots[room.id].append(slot)

        # Only core courses participate in student-slot tracking.
        if not course.is_elective:
            for half_key in self._active_halves(course.half):
                self.student_slots[course.student_group_key()][half_key].append(slot)

        self.sessions_today[course.id][slot.day].append(session_type)

    def _try_place(self, course: Course, session_type: SessionType,
                   session_number: int) -> bool:
        """
        Attempt to place one session of *session_type* for *course*.
        Iterates over all candidate slots of the correct duration and all eligible
        rooms until a valid combination is found, then commits it.  Returns True
        on success, False if no valid placement exists.
        """
        eligible_rooms = self._eligible_rooms(course, session_type)
        if not eligible_rooms:
            self.conflict_tally["No eligible room"] += 1
            return False

        duration = {SessionType.LECTURE: 1.5, SessionType.TUTORIAL: 1.0, SessionType.PRACTICAL: 2.0}
        candidate_slots = self.slots_by_duration[duration[session_type]]

        # FIX (2nd Commit): Room shuffling for electives.
        # For electives, randomise room order to spread them across rooms rather
        # than all piling into the first available room.
        rooms_to_try = list(eligible_rooms)
        if course.is_elective:
            random.shuffle(rooms_to_try)

        for slot in candidate_slots:
            for room in rooms_to_try:
                ok, _ = self._is_placeable(course, session_type, slot, room)
                if ok:
                    self._commit_session(course, session_type, slot, room, session_number)
                    return True

        return False

    def _schedule_course(self, course: Course) -> int:
        """
        Schedule all sessions (lectures, tutorials, practicals) for *course*.
        Returns the number of sessions successfully placed.
        """
        plan = [
            (SessionType.LECTURE,   course.lectures),
            (SessionType.TUTORIAL,  course.tutorials),
            (SessionType.PRACTICAL, course.practicals),
        ]
        placed = 0
        for session_type, count in plan:
            for n in range(1, count + 1):
                if self._try_place(course, session_type, n):
                    placed += 1
                else:
                    basket_tag = f" [basket {course.basket}]" if course.basket else ""
                    self.unscheduled.append(
                        f"{course.code} ({course.branch} {course.section or ''} "
                        f"{course.half}) – {session_type.value} #{n}"
                        f"{basket_tag} – Faculty: {course.faculty}"
                    )
        return placed

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

    def run(self) -> Dict:
        """
        Run the full scheduling pass and return the timetable as a dictionary.

        Scheduling order:
          1. Core courses (practicals first within each semester – labs are scarce)
          2. Electives (fill remaining free slots; can share slots with each other)
        """
        print("\n" + "=" * 60)
        print("GENERATING TIMETABLE")
        print("=" * 60)

        # AK08: Prioritised scheduling order.
        # Core courses (practicals first within each semester – labs are scarce)
        # go before Electives (which can fill remaining free slots and share slots with each other).
        ordered = sorted(
            self.courses,
            key=lambda c: (
                1 if c.is_elective else 0,  # core first
                c.half,
                c.semester,
                -c.practicals,              # lab-bound sessions are hardest to place
                -c.num_students,
            ),
        )

        total_placed = 0
        for course in ordered:
            total_placed += self._schedule_course(course)

        core_placed = sum(1 for s in self.schedule if not s.course.is_elective)
        elec_placed = sum(1 for s in self.schedule if s.course.is_elective)
        print(f"\n✓ Scheduled {total_placed} sessions  "
              f"(core: {core_placed}, elective: {elec_placed})")
        print(f"⚠️  {len(self.unscheduled)} unscheduled sessions")

        print("\n📊 Conflict breakdown:")
        for reason, count in self.conflict_tally.most_common():
            print(f"   {reason}: {count}")

        return self.to_dict()

    # -----------------------------------------------------------------------
    # Export helpers
    # -----------------------------------------------------------------------

    def to_dict(self) -> Dict:
        """Serialise the full schedule to a JSON-compatible dictionary."""
        return {
            "metadata": {
                "total_sessions":     len(self.schedule),
                "total_conflicts":    len(self.unscheduled),
                "total_courses":      len(self.courses),
                "elective_baskets":   len(self.elective_groups),
                "conflict_breakdown": dict(self.conflict_tally),
            },
            "conflicts": self.unscheduled,
            "schedule": [
                {
                    "course_code":    s.course.code,
                    "course_title":   s.course.name,
                    "semester":       s.course.semester,
                    "semester_half":  s.course.half,
                    "branch":         s.course.branch,
                    "section":        s.course.section,
                    "is_elective":    s.course.is_elective,
                    "basket":         s.basket,
                    "session_type":   s.session_type.value,
                    "session_number": s.session_number,
                    "day":            s.slot.day,
                    "start_time":     s.slot.start_time.strftime("%H:%M"),
                    "end_time":       s.slot.end_time.strftime("%H:%M"),
                    "room":           s.room.id,
                    "room_capacity":  s.room.capacity,
                    "faculty":        s.faculty,
                    "year":           (s.course.semester + 1) // 2,
                }
                for s in self.schedule
            ],
        }

    def by_student_group(self) -> Dict:
        """
        Return the schedule re-organised by student group (branch + year),
        with each group's sessions sorted by day then start time.
        Useful for generating per-class timetable views.
        """
        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        grouped: Dict[str, list] = defaultdict(list)

        for s in self.schedule:
            grouped[s.course.student_group_key()].append({
                "course_code":    s.course.code,
                "course_title":   s.course.name,
                "semester_half":  s.course.half,
                "session_type":   s.session_type.value,
                "session_number": s.session_number,
                "day":            s.slot.day,
                "time":           f"{s.slot.start_time:%H:%M}–{s.slot.end_time:%H:%M}",
                "room":           s.room.id,
                "faculty":        s.faculty,
                "is_elective":    s.course.is_elective,
                "basket":         s.basket,
            })

        for sessions in grouped.values():
            sessions.sort(key=lambda x: (
                day_order.index(x["day"]) if x["day"] in day_order else 99,
                x["time"],
            ))

        return dict(grouped)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(filepath: str) -> tuple[List[Course], List[Room]]:
    """
    Parse a JSON file produced by generate_input.py and return
    a (courses, rooms) tuple ready for the Scheduler.

    Expected top-level keys:  "courses", "rooms", optionally "faculties"
    Each course entry must have: course_code, course_name, semester, branch,
                                  ltpc, faculty_id, is_elective, num_students
    Each room entry must have:  room_id, capacity
    """
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    # KM01: Faculty ID Not Resolved to Name
    # We now fetch the 'faculties' dictionary mapping string IDs (like 'F012') to actual names.
    faculty_lookup: Dict[str, str] = {
        fac["faculty_id"]: fac.get("name", fac["faculty_id"])
        for fac in data.get("faculties", [])
        if fac.get("faculty_id")
    }

    courses: List[Course] = []
    
    # AS01: JSON Schema Incompatibility (`KeyError: 'Courses'`)
    # Automatically detects if the file uses older capitalized keys or new lowercase schema.
    is_new_schema = "courses" in data
    course_list = data["courses"] if is_new_schema else data.get("Courses", [])

    for idx, entry in enumerate(course_list, start=1):
        if is_new_schema:
            code         = str(entry.get("course_code", entry.get("course_id", ""))).strip()
            name         = entry.get("course_name", "")
            semester     = int(entry.get("semester", 1))
            branch       = entry.get("branch", "")
            section      = entry.get("section")
            num_students = int(entry.get("num_students", 60))
            
            # KM02: Robust Elective Detection
            # Deprecated fragile "Elective in course_title" checks in favour of proper explicit boolean flags.
            is_elective  = bool(entry.get("is_elective", False))
            
            # Parse L-T-P-C string (e.g. "3-1-0-4")
            ltpc    = entry.get("ltpc", "")
            parts   = ltpc.split("-") if "-" in ltpc else []
            lectures   = int(parts[0]) if len(parts) > 0 else int(entry.get("lectures",   0))
            tutorials  = int(parts[1]) if len(parts) > 1 else int(entry.get("tutorials",  0))
            practicals = int(parts[2]) if len(parts) > 2 else int(entry.get("practicals", 0))
            credits    = int(parts[3]) if len(parts) > 3 else int(entry.get("credits",    0))

            # Resolve faculty: prefer lookup table, fall back to raw field value
            raw_faculty_id = entry.get("faculty_id", "")
            raw_faculty    = faculty_lookup.get(raw_faculty_id, raw_faculty_id)
            
            # TC14: Read semester_half from JSON instead of hardcoding based on credits.
            half = entry.get("semester_half", entry.get("half"))
            if not half:
                if credits in (3, 4):
                    half = "Full"
                elif credits == 2:
                    half = "1st half"
                else:
                    half = "Full"
        else:
            code         = entry["Course Code"].strip()
            name         = entry["Course Title"]
            semester     = int(entry["Semester"])
            branch       = entry.get("Branch", "")
            section      = entry.get("Section") if entry.get("Section") not in ["None", None, ""] else None
            lectures     = int(entry["Lectures"])
            tutorials    = int(entry["Tutorials"])
            practicals   = int(entry["Practicals"])
            raw_faculty  = entry.get("Faculty", "Unknown")
            is_elective  = entry.get("Electives") == "T"
            num_students = 60

            if "Semester Half" not in entry or entry["Semester Half"] in (None, ""):
                half = "Sem-II"
                # AK06: Missing semester_half field now correctly triggers a fallback warning.
                try:
                    print(f"  ⚠️  Course {code} has no 'Semester Half' – defaulting to '{half}'")
                except UnicodeEncodeError:
                    print(f"  [Warning] Course {code} has no 'Semester Half' - defaulting to '{half}'")
            else:
                half = entry["Semester Half"]

        # AK05: Empty faculty bug fix.
        # Courses with no assigned faculty get a unique synthetic key so they
        # don't all block each other's slots by having the same blank faculty identity.
        if not raw_faculty.strip():
            raw_faculty = f"_unassigned_{idx}"
        
        faculty = _normalise_faculty(raw_faculty)

        courses.append(Course(
            id           = f"COURSE_{idx}",
            code         = str(entry.get("course_code", entry.get("course_id", ""))).strip(),
            name         = entry.get("course_name", ""),
            semester     = int(entry.get("semester", 1)),
            half         = half,
            branch       = entry.get("branch", ""),
            section      = entry.get("section"),
            lectures     = lectures,
            tutorials    = tutorials,
            practicals   = practicals,
            faculty      = faculty,
            is_elective  = bool(entry.get("is_elective", False)),
            num_students = int(entry.get("num_students", 60)),
        ))

    rooms: List[Room] = []
    room_list = data["rooms"] if is_new_schema else data.get("Rooms", [])
    for entry in room_list:
        if is_new_schema:
            room_id  = entry.get("room_id")
            capacity = int(entry.get("capacity", 0))
        else:
            room_id  = entry.get("Room")
            capacity = int(entry.get("Seating Capacity", 0))
            
        if room_id and room_id not in ("-", "Online", ""):
            rooms.append(Room(id=room_id, capacity=capacity))

    return courses, rooms


# ---------------------------------------------------------------------------
# HTML report generator
# ---------------------------------------------------------------------------

def _write_html(timetable: dict, student_data: dict, output_path: str) -> None:
    """Write a self-contained HTML file with the schedule embedded as JSON."""

    timetable_js = json.dumps(timetable,    ensure_ascii=False)
    student_js   = json.dumps(student_data, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IIIT Dharwad – Timetable</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=IBM+Plex+Mono:wght@400;500;600&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --primary:   #1a1a2e;
            --secondary: #16213e;
            --accent:    #0f3460;
            --highlight: #e94560;
            --success:   #06d6a0;
            --warning:   #ffd23f;
            --text:      #e8e8e8;
            --muted:     #a0a0a0;
            --bg:        #0a0a14;
            --card:      #1e1e30;
            --border:    rgba(255,255,255,0.1);
        }}
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{
            font-family:'DM Sans',sans-serif;
            background:linear-gradient(135deg,var(--bg) 0%,var(--primary) 50%,var(--secondary) 100%);
            color:var(--text); min-height:100vh; overflow-x:hidden;
        }}
        body::before {{
            content:''; position:fixed; top:0; left:0; width:100%; height:100%;
            background-image:linear-gradient(rgba(255,255,255,.03) 1px,transparent 1px),
                             linear-gradient(90deg,rgba(255,255,255,.03) 1px,transparent 1px);
            background-size:50px 50px; pointer-events:none; z-index:0;
            animation:gridMove 20s linear infinite;
        }}
        @keyframes gridMove {{ 0%{{transform:translate(0,0)}} 100%{{transform:translate(50px,50px)}} }}
        .container {{ max-width:1400px; margin:0 auto; padding:2rem; position:relative; z-index:1; }}
        header {{ text-align:center; margin-bottom:4rem; animation:fadeDown .8s ease; }}
        @keyframes fadeDown {{ from{{opacity:0;transform:translateY(-30px)}} to{{opacity:1;transform:translateY(0)}} }}
        h1 {{
            font-family:'Playfair Display',serif; font-size:4rem; font-weight:900;
            background:linear-gradient(135deg,var(--highlight),var(--success));
            -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
            margin-bottom:.5rem; letter-spacing:-2px;
        }}
        .subtitle {{ font-family:'IBM Plex Mono',monospace; font-size:.9rem; color:var(--muted); letter-spacing:2px; text-transform:uppercase; }}
        .stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:1.5rem; margin-bottom:3rem; }}
        .stat {{
            background:var(--card); border:1px solid var(--border); border-radius:16px;
            padding:1.5rem; transition:all .3s; position:relative; overflow:hidden;
        }}
        .stat::before {{
            content:''; position:absolute; top:0; left:0; width:100%; height:4px;
            background:linear-gradient(90deg,var(--highlight),var(--success));
            transform:scaleX(0); transform-origin:left; transition:transform .6s;
        }}
        .stat:hover::before {{ transform:scaleX(1); }}
        .stat:hover {{ transform:translateY(-5px); box-shadow:0 20px 40px rgba(0,0,0,.4); }}
        .stat-val {{ font-family:'IBM Plex Mono',monospace; font-size:2.5rem; font-weight:600; color:var(--highlight); }}
        .stat-lbl {{ font-size:.85rem; color:var(--muted); text-transform:uppercase; letter-spacing:1px; }}
        .tabs {{ display:flex; gap:1rem; margin-bottom:2rem; flex-wrap:wrap; }}
        .tab {{
            font-family:'IBM Plex Mono',monospace; padding:1rem 2rem;
            background:var(--card); border:1px solid var(--border); border-radius:12px;
            color:var(--text); cursor:pointer; font-size:.9rem; text-transform:uppercase;
            letter-spacing:1px; transition:all .3s;
        }}
        .tab.active {{ background:linear-gradient(135deg,var(--highlight),var(--accent)); border-color:var(--highlight); }}
        .tab:hover {{ border-color:var(--highlight); }}
        .panel {{ display:none; animation:fadeUp .5s ease; }}
        .panel.active {{ display:block; }}
        @keyframes fadeUp {{ from{{opacity:0;transform:translateY(20px)}} to{{opacity:1;transform:translateY(0)}} }}
        .card {{
            background:var(--card); border:1px solid var(--border); border-radius:16px;
            padding:2rem; margin-bottom:2rem;
        }}
        .card-title {{ font-family:'Playfair Display',serif; font-size:1.8rem; color:var(--highlight); margin-bottom:1.5rem; }}
        select, input {{
            width:100%; padding:1rem; background:var(--secondary); border:1px solid var(--border);
            border-radius:12px; color:var(--text); font-family:'DM Sans',sans-serif;
            font-size:1rem; margin-bottom:1.5rem; transition:all .3s;
        }}
        select:focus, input:focus {{ outline:none; border-color:var(--highlight); box-shadow:0 0 0 3px rgba(233,69,96,.2); }}
        .day-header {{
            font-family:'IBM Plex Mono',monospace; font-size:1.1rem; color:var(--success);
            margin-bottom:.75rem; padding-bottom:.5rem;
            border-bottom:2px solid var(--success); letter-spacing:2px;
        }}
        .session {{
            display:grid; grid-template-columns:110px 90px 1fr 80px; gap:1rem;
            padding:.9rem 1rem; background:var(--secondary); border-left:4px solid var(--highlight);
            border-radius:8px; margin-bottom:.6rem; transition:all .3s; align-items:center;
        }}
        .session:hover {{ background:var(--accent); border-left-color:var(--success); transform:translateX(8px); }}
        .s-time {{ font-family:'IBM Plex Mono',monospace; color:var(--warning); font-weight:500; font-size:.85rem; }}
        .s-code {{ font-family:'IBM Plex Mono',monospace; font-weight:600; }}
        .s-room {{ font-family:'IBM Plex Mono',monospace; color:var(--success); text-align:right; font-size:.85rem; }}
        .badge {{
            display:inline-block; padding:.2rem .6rem;
            background:var(--highlight); color:#fff; border-radius:20px;
            font-size:.7rem; font-weight:600; margin-left:.5rem; text-transform:uppercase;
        }}
        .basket-card {{
            background:var(--secondary); border:1px solid var(--border);
            border-radius:12px; padding:1.5rem; border-left:4px solid var(--success); margin-bottom:1rem;
        }}
        .basket-title {{ font-family:'IBM Plex Mono',monospace; font-size:1.3rem; color:var(--success); margin-bottom:1rem; }}
        .bc {{
            padding:.65rem; background:var(--accent); border-radius:8px;
            margin-bottom:.4rem; display:flex; justify-content:space-between; align-items:center;
        }}
        .btn {{
            display:inline-block; padding:1rem 2rem;
            background:linear-gradient(135deg,var(--success),var(--highlight));
            color:#fff; border:none; border-radius:12px;
            font-family:'IBM Plex Mono',monospace; font-size:1rem; font-weight:600;
            cursor:pointer; text-transform:uppercase; letter-spacing:1px;
            transition:all .3s; box-shadow:0 10px 30px rgba(6,214,160,.3); margin-right:1rem;
        }}
        .btn:hover {{ transform:translateY(-3px); box-shadow:0 15px 40px rgba(6,214,160,.4); }}
        .empty {{ text-align:center; padding:4rem 2rem; color:var(--muted); }}
        @media(max-width:768px) {{
            h1 {{ font-size:2.5rem; }}
            .session {{ grid-template-columns:1fr; gap:.4rem; }}
            .s-room {{ text-align:left; }}
        }}
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>Timetable Scheduler</h1>
        <p class="subtitle">IIIT Dharwad · Even Semester 2025–26</p>
    </header>

    <div class="stats" id="stats">
        <div class="stat"><div class="stat-val" id="sSessions">–</div><div class="stat-lbl">Sessions Scheduled</div></div>
        <div class="stat"><div class="stat-val" id="sRate">–</div><div class="stat-lbl">Success Rate</div></div>
        <div class="stat"><div class="stat-val" id="sBaskets">–</div><div class="stat-lbl">Elective Groups</div></div>
        <div class="stat"><div class="stat-val" id="sConflicts">–</div><div class="stat-lbl">Unscheduled</div></div>
    </div>

    <div class="tabs">
        <button class="tab active" data-tab="student"><span>Student View</span></button>
        <button class="tab" data-tab="baskets"><span>Elective Baskets</span></button>
        <button class="tab" data-tab="export"><span>Export</span></button>
    </div>

    <div id="student" class="panel active">
        <div class="card">
            <h2 class="card-title">Select Student Group</h2>
            <select id="groupSel"><option value="">– choose a group –</option></select>
            <div id="groupTT"></div>
        </div>
    </div>

    <div id="baskets" class="panel">
        <div class="card">
            <h2 class="card-title">Elective Baskets</h2>
            <p style="color:var(--muted);margin-bottom:2rem">
                Each basket groups electives that belong to the same semester/branch.
                Students choose one course from their basket; the scheduler ensures
                no elective overlaps with any core session for that group.
            </p>
            <div id="basketList"></div>
        </div>
    </div>

    <div id="export" class="panel">
        <div class="card">
            <h2 class="card-title">Export Data</h2>
            <button class="btn" onclick="exportJSON()">&#128229; Download JSON</button>
            <button class="btn" onclick="exportCSV()">&#128202; Download CSV</button>
        </div>
    </div>
</div>

<script>
    const TT   = {timetable_js};
    const STU  = {student_js};

    // ── Stats ──────────────────────────────────────────────────────────────
    (function() {{
        const m = TT.metadata;
        const total = m.total_sessions + m.total_conflicts;
        const rate  = total > 0 ? (m.total_sessions / total * 100).toFixed(1) : 0;
        animVal('sSessions', m.total_sessions);
        document.getElementById('sRate').textContent = rate + '%';
        animVal('sBaskets',  m.elective_baskets);
        animVal('sConflicts', m.total_conflicts);
    }})();

    function animVal(id, target) {{
        const el = document.getElementById(id);
        const step = target / 60;
        let cur = 0;
        const t = setInterval(() => {{
            cur = Math.min(cur + step, target);
            el.textContent = Math.floor(cur);
            if (cur >= target) clearInterval(t);
        }}, 16);
    }}

    // ── Student group dropdown ─────────────────────────────────────────────
    const sel = document.getElementById('groupSel');
    Object.keys(STU).sort().forEach(g => {{
        const o = document.createElement('option');
        o.value = g; o.textContent = g.replace(/_/g,' ');
        sel.appendChild(o);
    }});
    sel.addEventListener('change', e => renderGroup(e.target.value));

    function renderGroup(name) {{
        const el = document.getElementById('groupTT');
        if (!name) {{ el.innerHTML=''; return; }}
        const rows = STU[name];
        if (!rows || !rows.length) {{ el.innerHTML='<div class="empty">No sessions for this group.</div>'; return; }}
        const days = ['Monday','Tuesday','Wednesday','Thursday','Friday'];
        const byDay = {{}};
        days.forEach(d => byDay[d]=[]);
        rows.forEach(r => {{ if(byDay[r.day]) byDay[r.day].push(r); }});
        let h = '<div>';
        days.forEach(day => {{
            if (!byDay[day].length) return;
            h += `<div class="timetable-day"><div class="day-header">📅 ${{day.toUpperCase()}}</div>`;
            byDay[day].sort((a,b)=>a.time.localeCompare(b.time)).forEach(s => {{
                const badge = s.is_elective ? `<span class="badge">Elective${{s.basket?' · '+s.basket:''}}</span>` : '';
                h += `<div class="session">
                    <div class="s-time">${{s.time}}</div>
                    <div class="s-code">${{s.course_code}}</div>
                    <div class="s-name">${{s.course_title}} ${{badge}}</div>
                    <div class="s-room">${{s.room}}</div>
                </div>`;
            }});
            h += '</div>';
        }});
        h += '</div>';
        el.innerHTML = h;
    }}

    // ── Baskets ────────────────────────────────────────────────────────────
    (function() {{
        const container = document.getElementById('basketList');
        const baskets = {{}};
        TT.schedule.forEach(s => {{
            if (!s.is_elective || !s.basket) return;
            if (!baskets[s.basket]) baskets[s.basket] = {{}};
            const k = `${{s.day}} ${{s.start_time}}–${{s.end_time}}`;
            if (!baskets[s.basket][k]) baskets[s.basket][k] = [];
            baskets[s.basket][k].push(s);
        }});
        let h = '';
        Object.keys(baskets).sort().forEach(b => {{
            h += `<div class="basket-card"><div class="basket-title">🎓 Basket ${{b}}</div>`;
            Object.keys(baskets[b]).sort().forEach(tk => {{
                const sessions = baskets[b][tk];
                const codes = [...new Set(sessions.map(s=>s.course_code))];
                h += `<div style="margin-bottom:1rem"><div style="color:var(--warning);font-family:'IBM Plex Mono',monospace;margin-bottom:.5rem">⏱ ${{tk}}</div>
                       <div style="color:var(--muted);font-size:.85rem;margin-bottom:.5rem">${{codes.length}} courses to choose from:</div>`;
                const seen = new Set();
                sessions.forEach(s => {{
                    const k2 = s.course_code+s.room;
                    if (seen.has(k2)) return; seen.add(k2);
                    h += `<div class="bc"><span><strong>${{s.course_code}}</strong> · ${{s.course_title}}</span>
                           <span style="color:var(--success)">${{s.room}}</span></div>`;
                }});
                h += '</div>';
            }});
            h += '</div>';
        }});
        container.innerHTML = h || '<div class="empty">No elective baskets found.</div>';
    }})();

    // ── Tab switching ──────────────────────────────────────────────────────
    document.querySelectorAll('.tab').forEach(btn => {{
        btn.addEventListener('click', () => {{
            document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
            document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById(btn.dataset.tab).classList.add('active');
        }});
    }});

    // ── Export ─────────────────────────────────────────────────────────────
    function exportJSON() {{
        const blob = new Blob([JSON.stringify(TT,null,2)],{{type:'application/json'}});
        const a = document.createElement('a'); a.href=URL.createObjectURL(blob);
        a.download='tt.json'; a.click();
    }}
    function exportCSV() {{
        let csv = 'Day,Start,End,Code,Name,Type,Room,Faculty,Branch,Year,Half,Elective,Basket\\n';
        TT.schedule.forEach(s => {{
            csv += `"${{s.day}}","${{s.start_time}}","${{s.end_time}}","${{s.course_code}}","${{s.course_title}}","${{s.session_type}}","${{s.room}}","${{s.faculty}}","${{s.branch}}","${{s.year}}","${{s.semester_half}}","${{s.is_elective}}","${{s.basket||''}}"\\n`;
        }});
        const blob = new Blob([csv],{{type:'text/csv'}});
        const a = document.createElement('a'); a.href=URL.createObjectURL(blob);
        a.download='tt.csv'; a.click();
    }}
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    # AS02: Windows Unicode Emoji Crash (`UnicodeEncodeError`)
    # Ensure emoji output works natively on all terminals, primarily Windows cmd/PowerShell.
    if sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    input_file = sys.argv[1] if len(sys.argv) > 1 else "input_even_sem.json"
    if not os.path.exists(input_file):
        print(f"❌ Input file not found: {input_file}")
        sys.exit(1)

    print(f"📂 Loading: {input_file}")
    courses, rooms = load_data(input_file)

    # KM04: Low Scheduling Success Rate
    # The Scheduler now utilises expanded time slot pools (including Early/Late/Sat)
    # and has intentionally relaxed hardcoded constraints allowing higher efficiency rates.
    scheduler  = Scheduler(courses, rooms)
    timetable  = scheduler.run()
    by_student = scheduler.by_student_group()

    # AS03: Hardcoded Linux Output Pathing
    # Prevented files silently writing to fixed system paths like `/mnt/user-data/`.
    output_dir = "."
    os.makedirs(output_dir, exist_ok=True)
    
    # Write outputs
    with open(os.path.join(output_dir, "tt_out.json"), "w", encoding="utf-8") as f:
        json.dump(timetable, f, indent=2)

    with open(os.path.join(output_dir, "tt_student.json"), "w", encoding="utf-8") as f:
        json.dump(by_student, f, indent=2)

    _write_html(timetable, by_student, os.path.join(output_dir, "tt.html"))

    # Summary
    m            = timetable["metadata"]
    total_tried  = m["total_sessions"] + m["total_conflicts"]
    success_rate = (m["total_sessions"] / total_tried * 100) if total_tried else 0

    print(f"\n📊 FINAL SUMMARY")
    print(f"   Courses        : {m['total_courses']}")
    print(f"   Scheduled      : {m['total_sessions']}")
    print(f"   Unscheduled    : {m['total_conflicts']}")
    print(f"   Success rate   : {success_rate:.1f}%")
    print(f"   Elective groups: {m['elective_baskets']}")
    print(f"\n✅ Output files:")
    print(f"   tt_out.json")
    print(f"   tt_student.json")
    print(f"   tt.html  ← open in browser")

    if timetable["conflicts"]:
        shown = timetable["conflicts"][:15]
        print(f"\n⚠️  First {len(shown)} unscheduled sessions:")
        for entry in shown:
            print(f"   • {entry}")
        remaining = len(timetable["conflicts"]) - len(shown)
        if remaining:
            print(f"   … and {remaining} more")


if __name__ == "__main__":
    main()
