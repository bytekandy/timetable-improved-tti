# Modifications & Bug Report

Here is a comprehensive summary of the issues encountered when trying to run the timetable scheduler with `input_large.json`, and the specific modifications made to `timetable_scheduler_changed.py` to resolve them.

This report frames the problems as bugs that were fixed to make the system more robust.

---

## 🐛 Bug 1: JSON Schema Incompatibility (KeyError)
**Issue Details:** When loading the `input_large.json` dataset, the scheduler immediately crashed with a `KeyError: 'Courses'`.

**Previously (What it was):** 
The `load_data()` function strictly expected the input JSON data to use Capitalized keys (e.g., `"Courses"`, `"Rooms"`, `"Course Code"`, `"Semester Half"`). Specifically, the logic looked like this:
```python
# Old code
for c in data["Courses"]:
    course_code = c["Course Code"].strip()
    semester = int(c["Semester"])
```
The new dataset uses entirely different field names and lowercase keys (e.g., `"courses"`, `"rooms"`, `"course_id"`, `"semester"`).

**What was modified:**
Added schema detection logic to `load_data()`. The function now checks `if "courses" in data:` to determine which format the user provided. It loops through the `courses` list and cleanly maps the new (`input_large.json`) lowercase keys directly into the fields that the Python class constructor expects. It also dynamically determines the `Semester Half` by checking if the semester number is even or odd, preventing crashes from missing fields.

---

## 🐛 Bug 2: Windows Unicode Emoji Crash (`UnicodeEncodeError`)
**Issue Details:** Running the script on Windows PowerShell natively resulted in a crash right after the initialization: `UnicodeEncodeError: 'charmap' codec can't encode character '\U0001f4c2'`.

**Previously (What it was):**
The script used several graphical emojis in its print statements:
```python
# Old code
print(f"📂 Loading data from: {input_file}")
```
Windows consoles default to using the `cp1252` encoding instead of `UTF-8`. When the python interpreter attempted to push the folder emoji (📂) to the terminal, the `cp1252` character map couldn't compute it, ending the execution fatally.

**What was modified:**
Instead of requiring users to remember to type `python -X utf8` in their terminals every time, the top of `main()` was modified to automatically wrap the standard output buffer in UTF-8:
```python
# New code
import sys, os, io
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
```
This gracefully forces the terminal to render the modern characters or silently mask them, bringing cross-platform terminal compatibility.

---

## 🐛 Bug 3: Hardcoded Linux Output Pathing
**Issue Details:** The generated output files (`timetable_output.json`, `timetable_by_student.json`, `timetable_standalone.html`) were not predictably appearing in the project.

**Previously (What it was):**
The script had an absolute Linux volume path hardcoded for its outputs:
```python
# Old code
output_dir = "/mnt/user-data/outputs"
os.makedirs(output_dir, exist_ok=True)
```
On Windows environments, this either tries to silently generate a `C:\mnt\user-data\outputs` directory somewhere at the base of the hard drive, or it crashes based on permissions.

**What was modified:**
Changed the hardcoded output path string to `.`, representing the current working directory:
```python
# New code
output_dir = "."
os.makedirs(output_dir, exist_ok=True)
```
This ensures the files write to the exact same folder as the script itself, matching the behavior described in the project's README.

---

> [!SUCCESS] Result
> By applying these three targeted changes, `timetable_scheduler_changed.py` is now a significantly more resilient script that adapts to both data schemas, operates natively on Windows cmd/Powershell, and correctly outputs local files.
