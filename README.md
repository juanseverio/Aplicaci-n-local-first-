# Presencialidades

**Presencialidades** is a local-first school attendance and student management application designed for educational institutions.

It provides teachers, administrative staff, and school authorities with tools to manage attendance, classrooms, students, private teacher notes, reports, grades, calendars, and institutional records from a single interface.

> The application interface is available in **Spanish and English**.  
> Additional language support is also included for Japanese, Korean, Mandarin Chinese, Russian, and Arabic.

## About the Project

Presencialidades was created as a lightweight alternative to spreadsheets, generic forms, and overly complex school management platforms.

Its main goal is to make everyday school tasks faster and easier while keeping institutional data organized locally.

The application can run on a local computer using its own backend and SQLite database, without requiring an external online service.

Presencialidades was developed with the assistance of **Artificial Intelligence**.

## Main Features

### Attendance

- Daily attendance registration
- Present
- Absent
- Late
- Excused absence
- Early departure
- Mark all students as present
- Arrival and departure times
- Absence reasons
- Attendance history
- Automatic drafts and recovery
- Individual attendance statistics
- Attendance alerts

### Students

- Student records
- Student ID / school record number
- National ID information
- Date of birth
- Classroom assignment
- Parent or guardian information
- Emergency contact information
- Individual attendance history
- Grade history
- Reports and teacher notes
- Student search

### Classrooms

- Create and manage classrooms
- Rename classrooms
- Assign students
- Assign teachers and staff
- Archive and restore classrooms

### Private Teacher Notes

Teachers can create private notes associated with students.

Private notes are separate from formal institutional reports and are only available to the teacher who created them.

### Student Reports

Teachers and authorized staff can record situations related to:

- Behavior
- School coexistence
- Academic performance
- Attendance
- Late arrivals
- Health or emergencies
- Other situations

Reports can include priority levels, comments, responsible staff members, and follow-up status.

### Grades

- Create assessments
- Register student grades
- Review student progress
- View individual grade history

Presencialidades is not intended to replace a complete Learning Management System. Its grading tools are designed for simple institutional tracking.

### Calendar

The application includes a lightweight calendar for:

- Classes
- Exams
- Meetings
- Reminders
- Institutional activities

### Family Notices

Teachers can generate structured notices for parents or guardians.

Notices can be stored as records and optionally sent through a connected Gmail account.

### Google and Gmail Integration

Google integration is optional.

Users can connect a Google Account to their profile.

Google authentication can provide:

- Google account linking
- Email identification
- Profile information
- Google profile picture

Gmail access is requested separately.

When enabled, Presencialidades can use the Gmail API to send school notices without requiring permission to read the user's inbox.

Google OAuth credentials are **not included in the repository** and must be configured by the institution.

## User Accounts and Roles

Presencialidades supports different permission levels.

### Teacher

Teachers can:

- Access assigned classrooms
- Register attendance
- View authorized student information
- Create private notes
- Create student reports
- Register grades
- Use the calendar
- Generate family notices

### School Supervisor / Preceptor

Authorized staff can:

- Review attendance
- Access assigned classrooms
- Review student reports
- View authorized contact information
- Follow student attendance and school situations

### School Director

Directors can access institutional management tools, reports, users, classrooms, and configuration.

### Administrator

Administrators have access to system configuration, account management, permissions, backups, and technical tools.

Permissions are validated by the backend and are not based only on hidden interface elements.

## Languages

Presencialidades includes multilingual interface support.

Current languages include:

- 🇦🇷 Spanish
- 🇬🇧 English
- 🇯🇵 Japanese
- 🇰🇷 Korean
- 🇨🇳 Mandarin Chinese
- 🇷🇺 Russian
- 🇸🇦 Arabic

Arabic includes right-to-left interface support.

The selected language is saved for each user or installation.

## Appearance

Users can customize the interface with:

- Light mode
- Dark mode
- Multiple accent colors
- Reduced motion support

Attendance status colors remain independent from the selected interface theme.

## Local-First Design

Presencialidades is designed to work locally.
