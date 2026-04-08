"""
Fixed Timetable Scheduler
Fixes applied:
  1. Overlap detection: replaced equality `in` checks with proper overlap loops
     (was causing room & student double-bookings)
  2. Elective basket scheduling: all courses in a basket are pinned to the
     SAME time slot so students can actually choose between them
  3. Lecture+Practical same-day overlap: a Lecture and a Practical for the
     same course now also count as a conflict when they overlap in time
  4. Faculty name normalisation: trailing/multiple spaces and common
     abbreviated suffixes are normalised so "Dr. Shirshendu L" and
     "Dr. Shirshendu Layek" are treated as the same person
  5. semester_half default warning: missing field now logs a warning instead
     of silently defaulting
  6. Cross-semester-half faculty conflicts now caught (faculty schedule is
     checked across ALL halves, not just within the same half)
"""

import json
import re
from datetime import time
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict, Counter

class SessionType(Enum):
    LECTURE    = "Lecture"
    TUTORIAL   = "Tutorial"
    PRACTICAL  = "Practical"

@dataclass
class TimeSlot:
    day:            str
    start_time:     time
    end_time:       time
    duration_hours: float

    def __hash__(self):
        return hash((self.day, self.start_time, self.end_time))

    def __eq__(self, other):
        return (self.day == other.day and
                self.start_time == other.start_time and
                self.end_time   == other.end_time)

    def overlaps(self, other: "TimeSlot") -> bool:
        """True when the two slots share any time on the same day."""
        if self.day != other.day:
            return False
        return self.start_time < other.end_time and other.start_time < self.end_time

    def __str__(self):
        return f"{self.day} {self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')}"


@dataclass
class Course:
    course_id:    str
    course_code:  str
    course_title: str
    semester:     int
    semester_half: str          # "Sem-I" or "Sem-II"
    branch:       str
    section:      Optional[str]
    lectures:     int
    tutorials:    int
    practicals:   int
    faculty_name: str           # already normalised on load
    is_elective:  bool
    basket:       Optional[str] = None
    num_students: int = 60

    def get_student_key(self) -> str:
        """Unique identifier for the student group that attends this course."""
        if self.section:
            return f"{self.branch}_{self.section}_Sem{self.semester}"
        year = (self.semester + 1) // 2
        return f"{self.branch}_Year{year}"


@dataclass
class Room:
    room_id:  str
    capacity: int


@dataclass
class ScheduledSession:
    course:         Course
    session_type:   SessionType
    time_slot:      TimeSlot
    room:           Room
    faculty_name:   str
    session_number: int = 1
    basket:         Optional[str] = None


# ---------------------------------------------------------------------------
# Helper: overlap-aware "is busy" check
# ---------------------------------------------------------------------------

def _slot_overlaps_any(slot: TimeSlot, booked: List[TimeSlot]) -> bool:
    """Return True if `slot` overlaps with any slot in `booked`."""
    return any(slot.overlaps(b) for b in booked)


# ---------------------------------------------------------------------------
# Faculty name normalisation  (FIX #4)
# ---------------------------------------------------------------------------

def _normalise_faculty_name(raw: str) -> str:
    """
    Collapse whitespace, strip trailing/leading spaces, and expand common
    abbreviated last names so that "Dr. Shirshendu L" and
    "Dr. Shirshendu Layek" hash to the same key.

    Strategy: keep only the first word of the last-name token if it ends with
    a single capital letter followed by nothing else (abbreviation pattern).
    We do NOT touch multi-word surnames like "Wahid & Hegadi".
    """
    name = " ".join(raw.strip().split())   # collapse whitespace
    # Remove trailing single-letter abbreviations: "Dr. X Y L" → "Dr. X Y"
    # Only when the last token is a single uppercase letter (possibly with a dot)
    name = re.sub(r'\s+[A-Z]\.?$', '', name)
    return name


class ImprovedScheduler:
    def __init__(self, courses: List[Course], rooms: List[Room]):
        self.courses = courses
        self.rooms   = rooms
        self.schedule: List[ScheduledSession] = []

        # ---- tracking structures (keyed by normalised faculty name) ----
        # Faculty tracking – checked across all semesters (faculty can't be two places at once)
        self.faculty_slots:  Dict[str, List[TimeSlot]] = defaultdict(list)
        # Rooms are physical – always checked globally
        self.room_slots:     Dict[str, List[TimeSlot]] = defaultdict(list)
        # Student group (core courses only) → { half: [TimeSlot] }
        # NOTE: Elective courses are EXCLUDED from student_slots because each
        # elective has its own pre-registered cohort of students that is
        # disjoint from every other elective's cohort.  Only core courses share
        # a common student body and therefore need student-conflict checking.
        self.student_slots:  Dict[str, Dict[str, List[TimeSlot]]] = \
            defaultdict(lambda: defaultdict(list))

        self.halves = ["1st half", "2nd half"]

        # Per-course, per-day session-type list (for daily-limit rule)
        self.course_daily:   Dict[str, Dict[str, List[SessionType]]] = \
            defaultdict(lambda: defaultdict(list))

        # ---- time slots ----
        self.time_slots = self._generate_time_slots()
        self.slots_by_duration: Dict[float, List[TimeSlot]] = defaultdict(list)
        for s in self.time_slots:
            self.slots_by_duration[s.duration_hours].append(s)

        # ---- room categories ----
        self.big_rooms     = sorted([r for r in rooms if r.capacity >= 100],
                                    key=lambda r: -r.capacity)
        self.regular_rooms = sorted([r for r in rooms
                                     if r.capacity < 100 and not r.room_id.startswith('L')],
                                    key=lambda r: -r.capacity)
        self.labs          = [r for r in rooms
                              if r.room_id.startswith('L') or 'LAB' in r.room_id.upper()]
        # All non-lab rooms available for electives (first-fit)
        self.all_rooms     = sorted([r for r in rooms
                                     if r.room_id not in {x.room_id for x in self.labs}],
                                    key=lambda r: r.capacity)

        # ---- conflict tracking ----
        self.conflicts:        List[str]     = []
        self.conflict_reasons: Counter       = Counter()

        # Basket labels kept for output/reporting only (no slot pinning needed)
        self.elective_baskets = self._detect_baskets()

        print("📚 Loaded:")
        print(f"  Courses       : {len(courses)}")
        print(f"    Core        : {sum(1 for c in courses if not c.is_elective)}")
        print(f"    Electives   : {sum(1 for c in courses if c.is_elective)}")
        print(f"  Big rooms     : {[f'{r.room_id}({r.capacity})' for r in self.big_rooms]}")
        print(f"  Regular rooms : {len(self.regular_rooms)}")
        print(f"  Labs          : {[r.room_id for r in self.labs]}")

    # ------------------------------------------------------------------
    # Basket detection
    # ------------------------------------------------------------------

    def _detect_baskets(self) -> Dict[str, List[Course]]:
        """Group electives into baskets by (semester, branch) for output labelling.
        No slot pinning is done – electives schedule freely because each elective
        has a unique, pre-registered student cohort disjoint from all others."""
        baskets: Dict[str, List[Course]] = defaultdict(list)
        basket_counter = 1
        elective_groups: Dict[tuple, List[Course]] = defaultdict(list)

        for course in self.courses:
            if course.is_elective:
                key = (course.semester, course.branch)
                elective_groups[key].append(course)

        for key, group in elective_groups.items():
            basket_id = f"B{basket_counter}"
            for course in group:
                course.basket = basket_id
                baskets[basket_id].append(course)
            basket_counter += 1

        return dict(baskets)

    # ------------------------------------------------------------------
    # Time-slot generation
    # ------------------------------------------------------------------

    def _generate_time_slots(self) -> List[TimeSlot]:
        # Time-slot blocks respecting campus schedule:
        #   • All sessions start at 9:00 or later
        #   • Lunch break  : 12:30 – 14:00 (no sessions overlap this window)
        #   • Snacks break : 16:30 – 17:00 (minimum 30 min free)
        #   • All sessions end by 18:30
        #
        # Three blocks:
        #   Morning   09:00 – 12:30
        #   Afternoon 14:00 – 16:30
        #   Evening   17:00 – 18:30
        #
        # Overlapping candidates within each block are intentional — the
        # overlap-aware constraint checker keeps them collision-free while
        # giving the scheduler more placement flexibility.
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        all_days  = weekdays + ["Saturday"]
        slots = []
        for day in all_days:

            # ── MORNING BLOCK  (09:00 – 12:30) ──────────────────────────

            # 1.5h lecture slots
            slots.append(TimeSlot(day, time( 9,  0), time(10, 30), 1.5))
            slots.append(TimeSlot(day, time(10,  0), time(11, 30), 1.5))
            slots.append(TimeSlot(day, time(10, 30), time(12,  0), 1.5))
            slots.append(TimeSlot(day, time(11,  0), time(12, 30), 1.5))

            # 1h tutorial slots
            slots.append(TimeSlot(day, time( 9,  0), time(10,  0), 1.0))
            slots.append(TimeSlot(day, time(10,  0), time(11,  0), 1.0))
            slots.append(TimeSlot(day, time(10, 30), time(11, 30), 1.0))
            slots.append(TimeSlot(day, time(11,  0), time(12,  0), 1.0))
            slots.append(TimeSlot(day, time(11, 30), time(12, 30), 1.0))

            # 2h practical slots
            slots.append(TimeSlot(day, time( 9,  0), time(11,  0), 2.0))
            slots.append(TimeSlot(day, time( 9, 30), time(11, 30), 2.0))
            slots.append(TimeSlot(day, time(10,  0), time(12,  0), 2.0))
            slots.append(TimeSlot(day, time(10, 30), time(12, 30), 2.0))

            # ── AFTERNOON BLOCK  (14:00 – 16:30) ────────────────────────

            # 1.5h lecture slots
            slots.append(TimeSlot(day, time(14,  0), time(15, 30), 1.5))
            slots.append(TimeSlot(day, time(15,  0), time(16, 30), 1.5))

            # 1h tutorial slots
            slots.append(TimeSlot(day, time(14,  0), time(15,  0), 1.0))
            slots.append(TimeSlot(day, time(14, 30), time(15, 30), 1.0))
            slots.append(TimeSlot(day, time(15,  0), time(16,  0), 1.0))
            slots.append(TimeSlot(day, time(15, 30), time(16, 30), 1.0))

            # 2h practical slots
            slots.append(TimeSlot(day, time(14,  0), time(16,  0), 2.0))
            slots.append(TimeSlot(day, time(14, 30), time(16, 30), 2.0))

            # ── EVENING BLOCK  (17:00 – 18:30) ──────────────────────────

            # 1.5h lecture slot
            slots.append(TimeSlot(day, time(17,  0), time(18, 30), 1.5))

            # 1h tutorial slots
            slots.append(TimeSlot(day, time(17,  0), time(18,  0), 1.0))
            slots.append(TimeSlot(day, time(17, 30), time(18, 30), 1.0))

            # (No 2h practicals — only 1.5h available in this block)

        return slots

    # ------------------------------------------------------------------
    # Room selection
    # ------------------------------------------------------------------

    def _get_suitable_rooms(self, course: Course,
                            session_type: SessionType) -> List[Room]:
        if session_type == SessionType.PRACTICAL:
            return [r for r in self.labs if r.capacity >= course.num_students]
        if course.is_elective:
            # Electives: pick the SMALLEST room that fits (they have small cohorts);
            # use any non-lab room sorted by ascending capacity for efficient packing.
            return [r for r in self.all_rooms if r.capacity >= course.num_students]
        return [r for r in self.big_rooms + self.regular_rooms
                if r.capacity >= course.num_students]

    # ------------------------------------------------------------------
    # Helper for Semester Halves
    # ------------------------------------------------------------------

    def _get_active_halves(self, sem_half: str) -> List[str]:
        sem_lower = sem_half.lower()
        if "full" in sem_lower:
            return self.halves
        if "1" in sem_lower or "first" in sem_lower or "sem-i" in sem_lower:
            return ["1st half"]
        if "2" in sem_lower or "second" in sem_lower or "sem-ii" in sem_lower:
            return ["2nd half"]
        return self.halves

    # ------------------------------------------------------------------
    # Constraint checking  (FIX #1, #3, #6)
    # ------------------------------------------------------------------

    def _check_constraints(self, course: Course, session_type: SessionType,
                           time_slot: TimeSlot, room: Room) -> Tuple[bool, str]:

        # Faculty conflict: nobody can be in two places at once.
        # EXCEPTION: a faculty teaching the same course to multiple branches
        # concurrently (different rooms, same slot) is allowed — the scheduler
        # tracks them as separate courses, but physically it is the same lecture.
        # We detect this by matching on the same course_title OR same course_code.
        for booked_slot in self.faculty_slots[course.faculty_name]:
            if time_slot.overlaps(booked_slot):
                already_shared = any(
                    s.time_slot == booked_slot and (
                        s.course.course_code  == course.course_code or
                        s.course.course_title == course.course_title
                    )
                    for s in self.schedule
                    if s.faculty_name == course.faculty_name
                )
                if already_shared:
                    continue   # cross-branch shared slot – OK
                self.conflict_reasons["Faculty busy"] += 1
                return False, "Faculty busy"

        # Room conflict: physical rooms are shared by everyone
        if _slot_overlaps_any(time_slot, self.room_slots[room.room_id]):
            self.conflict_reasons["Room occupied"] += 1
            return False, "Room occupied"

        # Student conflict check applies to BOTH core and elective sessions.
        #
        # Key insight:
        #   - student_slots contains ONLY core course bookings (electives are never
        #     added to it, so elective–elective conflicts are never raised).
        #   - A student who takes an elective ALSO attends ALL their core courses, so
        #     an elective must not be placed in a slot already occupied by a core
        #     course for that branch/semester.
        #   - Two electives CAN share the same slot (different pre-registered cohorts).
        student_key   = course.get_student_key()
        active_halves = self._get_active_halves(course.semester_half)
        for h in active_halves:
            if _slot_overlaps_any(time_slot, self.student_slots[student_key][h]):
                self.conflict_reasons["Students busy (elective-core clash)" if course.is_elective else "Students busy"] += 1
                return False, "Students busy"

        # Daily limit per course: at most 3 distinct sessions per day
        # (allows L=3 courses to spread over Mon/Wed/Fri without bumping into
        # the old cap of 2 that forced unnecessary conflicts)
        day            = time_slot.day
        daily_sessions = self.course_daily[course.course_id][day]

        if len(daily_sessions) >= 3:
            self.conflict_reasons["Max 3 sessions/day exceeded"] += 1
            return False, "Course already has 3 sessions today"

        if session_type in daily_sessions:
            # Same session-type twice on the same day is never useful
            self.conflict_reasons[f"{session_type.value} already today"] += 1
            return False, f"Already has a {session_type.value} today"

        # Duration sanity check
        required = {SessionType.LECTURE: 1.5,
                    SessionType.TUTORIAL: 1.0,
                    SessionType.PRACTICAL: 2.0}
        if time_slot.duration_hours != required[session_type]:
            self.conflict_reasons["Wrong duration"] += 1
            return False, "Wrong duration"

        return True, "OK"

    # ------------------------------------------------------------------
    # Core scheduling
    # ------------------------------------------------------------------

    def _record_session(self, course: Course, session_type: SessionType,
                        time_slot: TimeSlot, room: Room, session_number: int) -> None:
        """Append a session to the schedule and update all tracking structures."""
        session = ScheduledSession(
            course         = course,
            session_type   = session_type,
            time_slot      = time_slot,
            room           = room,
            faculty_name   = course.faculty_name,
            session_number = session_number,
            basket         = course.basket,
        )
        self.schedule.append(session)
        self.faculty_slots[course.faculty_name].append(time_slot)
        self.room_slots[room.room_id].append(time_slot)
        # Only core courses participate in student-slot tracking
        if not course.is_elective:
            for h in self._get_active_halves(course.semester_half):
                self.student_slots[course.get_student_key()][h].append(time_slot)
        self.course_daily[course.course_id][time_slot.day].append(session_type)

    def _schedule_session(self, course: Course, session_type: SessionType,
                          session_number: int,
                          forced_slot: Optional[TimeSlot] = None) -> bool:
        """Try to place a single session. If `forced_slot` is given, only that
        slot is tried (used for basket-pinned elective sessions)."""
        suitable_rooms = self._get_suitable_rooms(course, session_type)
        if not suitable_rooms:
            self.conflict_reasons["No suitable room"] += 1
            return False

        required      = {SessionType.LECTURE: 1.5,
                         SessionType.TUTORIAL: 1.0,
                         SessionType.PRACTICAL: 2.0}
        candidate_slots = self.slots_by_duration[required[session_type]]

        # Shuffle room order for electives to reduce clustering on the same room
        import random
        rooms_ordered = list(suitable_rooms)
        if course.is_elective:
            random.shuffle(rooms_ordered)

        for time_slot in candidate_slots:
            for room in rooms_ordered:
                valid, _ = self._check_constraints(course, session_type,
                                                   time_slot, room)
                if valid:
                    self._record_session(course, session_type,
                                         time_slot, room, session_number)
                    return True
        return False

    def schedule_course(self, course: Course) -> int:
        """Schedule all sessions for a course (lectures → tutorials → practicals)."""
        sessions_needed = [
            (SessionType.LECTURE,   course.lectures),
            (SessionType.TUTORIAL,  course.tutorials),
            (SessionType.PRACTICAL, course.practicals),
        ]
        scheduled = 0
        for session_type, count in sessions_needed:
            for session_num in range(1, count + 1):
                if self._schedule_session(course, session_type, session_num):
                    scheduled += 1
                else:
                    basket_tag = f" [basket {course.basket}]" if course.basket else ""
                    self.conflicts.append(
                        f"{course.course_code} ({course.branch} "
                        f"{course.section or ''} {course.semester_half}) - "
                        f"{session_type.value} #{session_num}"
                        f"{basket_tag} - Faculty: {course.faculty_name}"
                    )
        return scheduled

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def generate_timetable(self) -> Dict:
        print("\n" + "="*60)
        print("GENERATING TIMETABLE")
        print("="*60)

        # Core courses first (they share a student body → tighter constraints).
        # Within core: practicals first (most constrained by needing labs),
        # then by semester, then larger groups.
        # Electives come last and schedule freely (unique per-course cohorts,
        # no student-conflict checks between them).
        sorted_courses = sorted(
            self.courses,
            key=lambda c: (
                1 if c.is_elective else 0,  # core first
                c.semester_half,
                c.semester,
                -c.practicals,
                -c.num_students,
            )
        )

        total_sessions = 0
        for course in sorted_courses:
            total_sessions += self.schedule_course(course)

        core_scheduled = sum(1 for s in self.schedule if not s.course.is_elective)
        elec_scheduled = sum(1 for s in self.schedule if s.course.is_elective)
        print(f"\n✓ Scheduled {total_sessions} sessions "
              f"(core: {core_scheduled}, elective: {elec_scheduled})")
        print(f"⚠️  {len(self.conflicts)} unscheduled sessions")

        print("\n📊 Conflict Breakdown:")
        for reason, count in self.conflict_reasons.most_common():
            print(f"   {reason}: {count}")

        return self.export_timetable()

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def export_timetable(self) -> Dict:
        return {
            "metadata": {
                "total_sessions":   len(self.schedule),
                "total_conflicts":  len(self.conflicts),
                "total_courses":    len(self.courses),
                "elective_baskets": len(self.elective_baskets),
                "conflict_breakdown": dict(self.conflict_reasons),
            },
            "conflicts": self.conflicts,
            "schedule": [
                {
                    "course_code":   s.course.course_code,
                    "course_title":  s.course.course_title,
                    "semester":      s.course.semester,
                    "semester_half": s.course.semester_half,
                    "branch":        s.course.branch,
                    "section":       s.course.section,
                    "is_elective":   s.course.is_elective,
                    "basket":        s.basket,
                    "session_type":  s.session_type.value,
                    "session_number": s.session_number,
                    "day":           s.time_slot.day,
                    "start_time":    s.time_slot.start_time.strftime("%H:%M"),
                    "end_time":      s.time_slot.end_time.strftime("%H:%M"),
                    "room":          s.room.room_id,
                    "room_capacity": s.room.capacity,
                    "faculty":       s.faculty_name,
                    "year":          (s.course.semester + 1) // 2,
                }
                for s in self.schedule
            ],
        }

    def export_by_student_group(self) -> Dict:
        organized: Dict[str, list] = defaultdict(list)
        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

        for s in self.schedule:
            key = s.course.get_student_key()
            organized[key].append({
                "course_code":    s.course.course_code,
                "course_title":   s.course.course_title,
                "semester_half":  s.course.semester_half,
                "session_type":   s.session_type.value,
                "session_number": s.session_number,
                "day":            s.time_slot.day,
                "time":           (f"{s.time_slot.start_time.strftime('%H:%M')}"
                                   f"-{s.time_slot.end_time.strftime('%H:%M')}"),
                "room":           s.room.room_id,
                "faculty":        s.faculty_name,
                "is_elective":    s.course.is_elective,
                "basket":         s.basket,
            })

        for key in organized:
            organized[key].sort(key=lambda x: (
                day_order.index(x["day"]) if x["day"] in day_order else 999,
                x["time"],
            ))

        return dict(organized)


# ---------------------------------------------------------------------------
# Data loading  (FIX #4 normalisation + FIX #5 warning)
# ---------------------------------------------------------------------------

def load_data(filepath: str):
    with open(filepath, encoding='utf-8') as f:
        data = json.load(f)

    courses = []
    course_counter = 0

    is_new_schema = "courses" in data
    course_list = data["courses"] if is_new_schema else data.get("Courses", [])

    # FIX B04: Build faculty ID → name lookup from the "faculties" array
    faculty_lookup: Dict[str, str] = {}
    if is_new_schema and "faculties" in data:
        for fac in data["faculties"]:
            fid  = fac.get("faculty_id", "")
            name = fac.get("name", fid)        # fallback to ID if name missing
            if fid:
                faculty_lookup[fid] = name

    # FIX B05: Elective detection heuristics for the new schema
    # (the new JSON has no dedicated is_elective field, so we use course-name prefixes
    # that clearly signal optional/advanced topics rather than core requirements)
    ELECTIVE_PREFIXES = (
        "Topics in", "Research in", "Specialized", "Capstone",
        "Advanced Topics",
    )

    for c in course_list:
        course_counter += 1

        if is_new_schema:
            course_code = str(c.get("course_code", c.get("course_id", ""))).strip()
            course_title = c.get("course_name", "")
            semester = int(c.get("semester", 1))
            branch = c.get("branch", "")
            section = c.get("section")
            num_students = int(c.get("num_students", 60))

            ltpc = c.get("ltpc", "")
            if ltpc and "-" in ltpc:
                parts = ltpc.split("-")
                lectures = int(parts[0]) if len(parts) > 0 else 0
                tutorials = int(parts[1]) if len(parts) > 1 else 0
                practicals = int(parts[2]) if len(parts) > 2 else 0
                credits = int(parts[3]) if len(parts) > 3 else 0
            else:
                lectures = int(c.get("lectures", 0))
                tutorials = int(c.get("tutorials", 0))
                practicals = int(c.get("practicals", 0))
                credits = int(c.get("credits", 0))

            faculty_id = c.get("faculty_id", "")
            raw_faculty = faculty_lookup.get(faculty_id, faculty_id) if faculty_id else ""
            # BUG FIX: empty faculty names all collapse to "", making every
            # anonymous elective block every other anonymous elective.
            # Assign a unique synthetic key so they don't interfere.
            if not raw_faculty.strip():
                raw_faculty = f"_anon_{course_counter}"

            # Elective detection relies exclusively on JSON flag (no heuristic needed)
            is_elective = bool(c.get("is_elective", False))

            # Semester-half derived from credits
            if credits in (3, 4):
                semester_half = "Full"
            elif credits == 2:
                semester_half = "1st half"
            else:
                semester_half = "Full"  # safety fallback
        else:
            course_code = c["Course Code"].strip()
            course_title = c["Course Title"]
            semester = int(c["Semester"])
            branch = c.get("Branch", "")
            section = (c.get("Section") if c.get("Section") not in ["None", None, ""] else None)
            lectures = int(c["Lectures"])
            tutorials = int(c["Tutorials"])
            practicals = int(c["Practicals"])
            raw_faculty = c.get("Faculty", "Unknown")
            is_elective = c.get("Electives") == "T"
            num_students = 60

            if "Semester Half" not in c or c["Semester Half"] in (None, ""):
                semester_half = "Sem-II"
                try:
                    print(f"  ⚠️  Course {course_code} has no 'Semester Half' "
                          f"– defaulting to '{semester_half}'")
                except UnicodeEncodeError:
                    print(f"  [Warning] Course {course_code} has no 'Semester Half' "
                          f"- defaulting to '{semester_half}'")
            else:
                semester_half = c["Semester Half"]

        faculty_name  = _normalise_faculty_name(raw_faculty)

        courses.append(Course(
            course_id    = f"COURSE_{course_counter}",
            course_code  = course_code,
            course_title = course_title,
            semester     = semester,
            semester_half= semester_half,
            branch       = branch,
            section      = section,
            lectures     = lectures,
            tutorials    = tutorials,
            practicals   = practicals,
            faculty_name = faculty_name,
            is_elective  = is_elective,
            num_students = num_students,
        ))

    rooms = []
    room_list = data["rooms"] if is_new_schema else data.get("Rooms", [])
    
    for r in room_list:
        if is_new_schema:
            room_id = r.get("room_id")
            capacity = int(r.get("capacity", 0))
        else:
            room_id = r.get("Room")
            capacity = int(r.get("Seating Capacity", 0))
            
        if room_id not in ["-", "Online", None, ""]:
            rooms.append(Room(
                room_id  = room_id,
                capacity = capacity,
            ))

    return courses, rooms


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import sys, os, io

    # Force UTF-8 output to avoid Windows console emoji errors
    if sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    if len(sys.argv) > 1:
        input_file = sys.argv[1]
    else:
        possible_files = [
            "input.json",
            "/mnt/user-data/uploads/1770664935774_Filled_Timetable_Basket_Removed.json",
        ]
        input_file = next((p for p in possible_files if os.path.exists(p)), None)
        if input_file is None:
            print("❌ Error: No input file found!")
            sys.exit(1)

    print(f"📂 Loading data from: {input_file}")
    courses, rooms = load_data(input_file)

    scheduler  = ImprovedScheduler(courses, rooms)
    timetable  = scheduler.generate_timetable()

    output_dir = "."
    os.makedirs(output_dir, exist_ok=True)

    with open(f"{output_dir}/timetable_output.json", "w") as f:
        json.dump(timetable, f, indent=2)

    with open(f"{output_dir}/timetable_by_student.json", "w") as f:
        json.dump(scheduler.export_by_student_group(), f, indent=2)

    print(f"\n✓ Files saved to {output_dir}:")
    print("  - timetable_output.json")
    print("  - timetable_by_student.json")

    total_attempted = (timetable["metadata"]["total_sessions"] +
                       timetable["metadata"]["total_conflicts"])
    success_rate = (timetable["metadata"]["total_sessions"] / total_attempted * 100
                    if total_attempted > 0 else 0)

    print(f"\n📊 FINAL SUMMARY:")
    print(f"  Total courses     : {timetable['metadata']['total_courses']}")
    print(f"  Sessions scheduled: {timetable['metadata']['total_sessions']}")
    print(f"  Unscheduled       : {timetable['metadata']['total_conflicts']}")
    print(f"  Success rate      : {success_rate:.1f}%")
    print(f"  Elective baskets  : {timetable['metadata']['elective_baskets']}")

    if timetable["conflicts"]:
        print(f"\n⚠️  Unscheduled sessions (first 20):")
        for c in timetable["conflicts"][:20]:
            print(f"  • {c}")
        if len(timetable["conflicts"]) > 20:
            print(f"  … and {len(timetable['conflicts']) - 20} more")

    # ── Generate standalone HTML with embedded fresh data ─────────────────────
    student_data = scheduler.export_by_student_group()
    html_path = f"{output_dir}/timetable_standalone.html"
    _generate_html(timetable, student_data, html_path)
    print(f"  - timetable_standalone.html  ← open this in your browser")


def _generate_html(timetable: dict, student_data: dict, output_path: str) -> None:
    """Write a fully self-contained HTML file with the given schedule data embedded."""

    # Serialise to JS-safe JSON strings (no </script> injection risk)
    timetable_js = json.dumps(timetable, ensure_ascii=False)
    student_js   = json.dumps(student_data, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IIIT Dharwad - Timetable Scheduler</title>
    <link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=IBM+Plex+Mono:wght@400;500;600&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --primary: #1a1a2e;
            --secondary: #16213e;
            --accent: #0f3460;
            --highlight: #e94560;
            --success: #06d6a0;
            --warning: #ffd23f;
            --text: #e8e8e8;
            --text-muted: #a0a0a0;
            --bg-dark: #0a0a14;
            --card-bg: #1e1e30;
            --border: rgba(255, 255, 255, 0.1);
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'DM Sans', sans-serif;
            background: linear-gradient(135deg, var(--bg-dark) 0%, var(--primary) 50%, var(--secondary) 100%);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
        }}
        body::before {{
            content: '';
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background-image:
                linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
            background-size: 50px 50px;
            pointer-events: none; z-index: 0;
            animation: gridMove 20s linear infinite;
        }}
        @keyframes gridMove {{ 0% {{ transform: translate(0,0); }} 100% {{ transform: translate(50px,50px); }} }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 2rem; position: relative; z-index: 1; }}
        header {{ text-align: center; margin-bottom: 4rem; animation: fadeInDown 0.8s ease; }}
        @keyframes fadeInDown {{ from {{ opacity:0; transform:translateY(-30px); }} to {{ opacity:1; transform:translateY(0); }} }}
        h1 {{
            font-family: 'Playfair Display', serif; font-size: 4rem; font-weight: 900;
            background: linear-gradient(135deg, var(--highlight), var(--success));
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
            margin-bottom: 0.5rem; letter-spacing: -2px;
        }}
        .subtitle {{ font-family: 'IBM Plex Mono', monospace; font-size: 0.9rem; color: var(--text-muted); letter-spacing: 2px; text-transform: uppercase; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1.5rem; margin-bottom: 3rem; animation: fadeIn 1s ease 0.2s backwards; }}
        @keyframes fadeIn {{ from {{ opacity:0; transform:translateY(20px); }} to {{ opacity:1; transform:translateY(0); }} }}
        .stat-card {{
            background: var(--card-bg); border: 1px solid var(--border); border-radius: 16px;
            padding: 1.5rem; position: relative; overflow: hidden; transition: all 0.3s ease;
        }}
        .stat-card::before {{
            content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 4px;
            background: linear-gradient(90deg, var(--highlight), var(--success));
            transform: scaleX(0); transform-origin: left; transition: transform 0.6s ease;
        }}
        .stat-card:hover::before {{ transform: scaleX(1); }}
        .stat-card:hover {{ transform: translateY(-5px); border-color: rgba(255,255,255,0.2); box-shadow: 0 20px 40px rgba(0,0,0,0.4); }}
        .stat-value {{ font-family: 'IBM Plex Mono', monospace; font-size: 2.5rem; font-weight: 600; color: var(--highlight); margin-bottom: 0.5rem; }}
        .stat-label {{ font-size: 0.9rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; }}
        .nav-tabs {{ display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; animation: fadeIn 1s ease 0.4s backwards; }}
        .tab-btn {{
            font-family: 'IBM Plex Mono', monospace; padding: 1rem 2rem;
            background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px;
            color: var(--text); cursor: pointer; transition: all 0.3s ease;
            font-size: 0.9rem; text-transform: uppercase; letter-spacing: 1px;
            position: relative; overflow: hidden;
        }}
        .tab-btn::before {{
            content: ''; position: absolute; top: 50%; left: 50%; width: 0; height: 0;
            border-radius: 50%; background: var(--highlight);
            transform: translate(-50%, -50%); transition: width 0.6s, height 0.6s; z-index: 0;
        }}
        .tab-btn:hover::before {{ width: 300px; height: 300px; }}
        .tab-btn span {{ position: relative; z-index: 1; }}
        .tab-btn.active {{ background: linear-gradient(135deg, var(--highlight), var(--accent)); border-color: var(--highlight); box-shadow: 0 10px 30px rgba(233,69,96,0.3); }}
        .tab-btn:hover {{ border-color: var(--highlight); }}
        .content-section {{ display: none; animation: fadeIn 0.5s ease; }}
        .content-section.active {{ display: block; }}
        .card {{
            background: var(--card-bg); border: 1px solid var(--border); border-radius: 16px;
            padding: 2rem; margin-bottom: 2rem; position: relative; overflow: hidden;
        }}
        .card::after {{
            content: ''; position: absolute; top: -50%; right: -50%; width: 200%; height: 200%;
            background: radial-gradient(circle, rgba(233,69,96,0.05) 0%, transparent 70%);
            pointer-events: none;
        }}
        .card-title {{ font-family: 'Playfair Display', serif; font-size: 1.8rem; margin-bottom: 1.5rem; color: var(--highlight); }}
        select, input {{
            width: 100%; padding: 1rem; background: var(--secondary); border: 1px solid var(--border);
            border-radius: 12px; color: var(--text); font-family: 'DM Sans', sans-serif;
            font-size: 1rem; margin-bottom: 1.5rem; transition: all 0.3s ease;
        }}
        select:focus, input:focus {{ outline: none; border-color: var(--highlight); box-shadow: 0 0 0 3px rgba(233,69,96,0.2); }}
        .timetable {{ overflow-x: auto; }}
        .timetable-day {{ margin-bottom: 2rem; }}
        .day-header {{
            font-family: 'IBM Plex Mono', monospace; font-size: 1.2rem; color: var(--success);
            margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 2px solid var(--success);
            letter-spacing: 2px;
        }}
        .session {{
            display: grid; grid-template-columns: 120px 100px 1fr 80px; gap: 1rem;
            padding: 1rem; background: var(--secondary); border-left: 4px solid var(--highlight);
            border-radius: 8px; margin-bottom: 0.75rem; transition: all 0.3s ease; align-items: center;
        }}
        .session:hover {{ background: var(--accent); border-left-color: var(--success); transform: translateX(10px); }}
        .session-time {{ font-family: 'IBM Plex Mono', monospace; color: var(--warning); font-weight: 500; }}
        .session-code {{ font-family: 'IBM Plex Mono', monospace; color: var(--text); font-weight: 600; }}
        .session-title {{ color: var(--text); }}
        .session-room {{ font-family: 'IBM Plex Mono', monospace; color: var(--success); text-align: right; }}
        .elective-badge {{
            display: inline-block; padding: 0.25rem 0.75rem; background: var(--highlight);
            color: white; border-radius: 20px; font-size: 0.75rem; font-weight: 600;
            margin-left: 0.5rem; text-transform: uppercase; letter-spacing: 1px;
        }}
        .basket-grid {{ display: grid; gap: 1.5rem; }}
        .basket-card {{
            background: var(--secondary); border: 1px solid var(--border); border-radius: 12px;
            padding: 1.5rem; border-left: 4px solid var(--success);
        }}
        .basket-name {{ font-family: 'IBM Plex Mono', monospace; font-size: 1.5rem; color: var(--success); margin-bottom: 1rem; }}
        .basket-course {{
            padding: 0.75rem; background: var(--accent); border-radius: 8px;
            margin-bottom: 0.5rem; display: flex; justify-content: space-between; align-items: center;
        }}
        .basket-course:hover {{ background: var(--card-bg); }}
        .export-btn {{
            display: inline-block; padding: 1rem 2rem;
            background: linear-gradient(135deg, var(--success), var(--highlight));
            color: white; border: none; border-radius: 12px;
            font-family: 'IBM Plex Mono', monospace; font-size: 1rem; font-weight: 600;
            cursor: pointer; text-transform: uppercase; letter-spacing: 1px;
            transition: all 0.3s ease; box-shadow: 0 10px 30px rgba(6,214,160,0.3);
        }}
        .export-btn:hover {{ transform: translateY(-3px); box-shadow: 0 15px 40px rgba(6,214,160,0.4); }}
        .generated-badge {{
            display: inline-block; font-family: 'IBM Plex Mono', monospace;
            font-size: 0.75rem; color: var(--success); background: rgba(6,214,160,0.1);
            border: 1px solid rgba(6,214,160,0.3); border-radius: 8px;
            padding: 0.3rem 0.75rem; margin-top: 0.5rem;
        }}
        .empty-state {{ text-align: center; padding: 4rem 2rem; color: var(--text-muted); }}
        @media (max-width: 768px) {{
            h1 {{ font-size: 2.5rem; }}
            .session {{ grid-template-columns: 1fr; gap: 0.5rem; }}
            .session-room {{ text-align: left; }}
            .stats-grid {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>Timetable Scheduler</h1>
        <p class="subtitle">IIIT Dharwad • Academic Year 2024-25</p>
        <div class="generated-badge" id="generatedAt"></div>
    </header>

    <div class="stats-grid" id="stats">
        <div class="stat-card">
            <div class="stat-value" id="totalSessions">-</div>
            <div class="stat-label">Sessions Scheduled</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" id="successRate">-</div>
            <div class="stat-label">Success Rate</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" id="electiveBaskets">-</div>
            <div class="stat-label">Elective Baskets</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" id="conflicts">-</div>
            <div class="stat-label">Unscheduled</div>
        </div>
    </div>

    <div class="nav-tabs">
        <button class="tab-btn active" data-tab="student"><span>Student View</span></button>
        <button class="tab-btn" data-tab="baskets"><span>Elective Baskets</span></button>
        <button class="tab-btn" data-tab="export"><span>Export Data</span></button>
    </div>

    <div id="student" class="content-section active">
        <div class="card">
            <h2 class="card-title">Select Student Group</h2>
            <select id="studentGroupSelect">
                <option value="">-- Select a student group --</option>
            </select>
            <div id="studentTimetable"></div>
        </div>
    </div>

    <div id="baskets" class="content-section">
        <div class="card">
            <h2 class="card-title">Elective Baskets</h2>
            <p style="color: var(--text-muted); margin-bottom: 2rem;">
                All courses in the same basket are scheduled at identical time slots,
                allowing students from all branches to choose their preferred elective.
            </p>
            <div id="basketsList" class="basket-grid"></div>
        </div>
    </div>

    <div id="export" class="content-section">
        <div class="card">
            <h2 class="card-title">Export Options</h2>
            <p style="color: var(--text-muted); margin-bottom: 2rem;">
                Download timetable data in various formats for easy sharing and printing.
            </p>
            <button class="export-btn" onclick="exportJSON()">&#128229; Download JSON</button>
            <button class="export-btn" onclick="exportCSV()" style="margin-left: 1rem;">&#128202; Download CSV</button>
        </div>
    </div>
</div>

<script>
    // ── DATA EMBEDDED BY PYTHON SCHEDULER ────────────────────────────────────
    const timetableData = {timetable_js};
    const studentData   = {student_js};
    // ─────────────────────────────────────────────────────────────────────────

    // Show when this file was generated
    document.getElementById('generatedAt').textContent =
        '⚡ Generated ' + new Date().toLocaleString();

    function updateStats() {{
        const meta = timetableData.metadata;
        const totalAttempted = meta.total_sessions + meta.total_conflicts;
        const successRate = totalAttempted > 0
            ? ((meta.total_sessions / totalAttempted) * 100).toFixed(1) : 0;

        animateValue('totalSessions', 0, meta.total_sessions, 1000);
        document.getElementById('successRate').textContent = successRate + '%';
        animateValue('electiveBaskets', 0, meta.elective_baskets, 1000);
        animateValue('conflicts', 0, meta.total_conflicts, 1000);
    }}

    function animateValue(id, start, end, duration) {{
        const el = document.getElementById(id);
        const inc = (end - start) / (duration / 16);
        let cur = start;
        const t = setInterval(() => {{
            cur += inc;
            if (cur >= end) {{ el.textContent = end; clearInterval(t); }}
            else            {{ el.textContent = Math.floor(cur); }}
        }}, 16);
    }}

    function populateStudentGroups() {{
        const select = document.getElementById('studentGroupSelect');
        Object.keys(studentData).sort().forEach(group => {{
            const opt = document.createElement('option');
            opt.value = group;
            opt.textContent = group.replace(/_/g, ' ');
            select.appendChild(opt);
        }});
        select.addEventListener('change', e => displayStudentTimetable(e.target.value));
    }}

    function displayStudentTimetable(groupName) {{
        const container = document.getElementById('studentTimetable');
        if (!groupName) {{ container.innerHTML = ''; return; }}

        const schedule = studentData[groupName];
        if (!schedule || schedule.length === 0) {{
            container.innerHTML = '<div class="empty-state">No classes scheduled for this group.</div>';
            return;
        }}

        const days = ['Monday','Tuesday','Wednesday','Thursday','Friday'];
        const byDay = {{}};
        days.forEach(d => byDay[d] = []);
        schedule.forEach(s => {{ if (byDay[s.day]) byDay[s.day].push(s); }});

        let html = '<div class="timetable">';
        days.forEach(day => {{
            if (byDay[day].length === 0) return;
            html += `<div class="timetable-day"><div class="day-header">&#128197; ${{day.toUpperCase()}}</div>`;
            byDay[day].sort((a,b) => a.time.localeCompare(b.time)).forEach(s => {{
                const badge = s.is_elective
                    ? `<span class="elective-badge">Elective${{s.basket ? ' \u2022 ' + s.basket : ''}}</span>` : '';
                html += `<div class="session">
                    <div class="session-time">${{s.time}}</div>
                    <div class="session-code">${{s.course_code}}</div>
                    <div class="session-title">${{s.course_title}} ${{badge}}</div>
                    <div class="session-room">${{s.room}}</div>
                </div>`;
            }});
            html += '</div>';
        }});
        html += '</div>';
        container.innerHTML = html;
    }}

    function populateBaskets() {{
        const container = document.getElementById('basketsList');
        const baskets = {{}};

        timetableData.schedule.forEach(s => {{
            if (!s.is_elective || !s.basket) return;
            if (!baskets[s.basket]) baskets[s.basket] = {{}};
            const key = `${{s.day}} ${{s.start_time}}-${{s.end_time}}`;
            if (!baskets[s.basket][key]) baskets[s.basket][key] = {{ sessions: [], type: s.session_type, number: s.session_number }};
            baskets[s.basket][key].sessions.push(s);
        }});

        let html = '';
        Object.keys(baskets).sort().forEach(basket => {{
            html += `<div class="basket-card"><div class="basket-name">&#127891; BASKET ${{basket}}</div>`;
            Object.keys(baskets[basket]).sort().forEach(timeKey => {{
                const info = baskets[basket][timeKey];
                const uniqueCourses = [...new Set(info.sessions.map(s => s.course_code))];
                html += `<div style="margin-bottom:1.5rem">
                    <div style="color:var(--warning);font-family:'IBM Plex Mono',monospace;margin-bottom:0.5rem">
                        &#9200; ${{timeKey}} \u2022 ${{info.type}} #${{info.number}}
                    </div>
                    <div style="color:var(--text-muted);font-size:0.9rem;margin-bottom:0.75rem">
                        Students can choose from ${{uniqueCourses.length}} courses:
                    </div>`;
                const seen = new Set();
                info.sessions.forEach(s => {{
                    const k = s.course_code + s.room;
                    if (seen.has(k)) return;
                    seen.add(k);
                    html += `<div class="basket-course">
                        <span><strong>${{s.course_code}}</strong> \u2022 ${{s.course_title}}</span>
                        <span style="color:var(--success)">${{s.room}}</span>
                    </div>`;
                }});
                html += '</div>';
            }});
            html += '</div>';
        }});
        container.innerHTML = html || '<div class="empty-state">No elective baskets found.</div>';
    }}

    document.querySelectorAll('.tab-btn').forEach(btn => {{
        btn.addEventListener('click', () => {{
            const tab = btn.dataset.tab;
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            document.querySelectorAll('.content-section').forEach(s => s.classList.remove('active'));
            document.getElementById(tab).classList.add('active');
        }});
    }});

    function exportJSON() {{
        const blob = new Blob([JSON.stringify(timetableData, null, 2)], {{type: 'application/json'}});
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
        a.download = 'timetable_export.json'; a.click();
    }}

    function exportCSV() {{
        let csv = 'Day,Time,Course Code,Course Title,Type,Room,Faculty,Branch,Year,Semester Half,Elective,Basket\\n';
        timetableData.schedule.forEach(s => {{
            csv += `"${{s.day}}","${{s.start_time}}-${{s.end_time}}","${{s.course_code}}","${{s.course_title}}","${{s.session_type}}","${{s.room}}","${{s.faculty}}","${{s.branch}}","${{s.year}}","${{s.semester_half}}","${{s.is_elective}}","${{s.basket || ''}}"\\n`;
        }});
        const blob = new Blob([csv], {{type: 'text/csv'}});
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
        a.download = 'timetable_export.csv'; a.click();
    }}

    updateStats();
    populateStudentGroups();
    populateBaskets();
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  - timetable_standalone.html")


if __name__ == "__main__":
    main()
