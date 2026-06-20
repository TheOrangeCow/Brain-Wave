# 🌊 Brain Wave

A YouTube video study tool: paste a link, get a summary, quiz, flashcards, mindmap,
and exam questions — all generated in a single AI call to save on free-tier limits.

## Features
- Account system (signup/login) — each user stores **their own** Gemini API key
- Home page with a "Welcome back" greeting and a daily streak counter
- Floating sidebar navigation (Home / Video / Exam Timetable / Settings / Log out)
- Video page: video preview + transcript on the left, tabbed results panel on the right
  (Summary / Quiz / Flashcards / Mindmap / Exam questions)
- Exam Timetable page: upload a photo of your exam schedule, AI reads the text off the
  image (subjects + dates), you rate how comfortable you are with each subject (1–5),
  then a plain-Python algorithm (no AI) builds a day-by-day revision plan — subjects
  you're less comfortable with get more revision sessions
- Purple-blue rounded theme using the Fredoka font

## Setup

```bash
pip install -r requirements.txt
```

Set a session secret (any random string):

```bash
export SECRET_KEY="something-random"
```

Run:

```bash
python app.py
```

Visit http://localhost:5000 — sign up for an account, then go to **Settings** to add
your free Gemini API key (get one at https://aistudio.google.com/apikey).

## Notes

- User data (including each user's API key) is stored in a local SQLite file,
  `brainwave.db`, created automatically on first run. This is fine for personal/small-group
  use; for anything public-facing, the API keys should be encrypted at rest rather than
  stored in plain text.
- The combined AI call (`generate_all` in `app.py`) asks the model for the summary, quiz,
  flashcards, mindmap, and exam questions all in one request/response, to use far fewer
  calls against the free-tier daily limit than calling separately for each feature.
- The Exam Timetable feature uses AI for exactly one thing: reading the subjects/dates off
  your uploaded image (`extract_exams_from_image`). The actual revision schedule
  (`build_revision_timetable`) is plain Python — no AI call, no extra quota used. It works
  by weighting each subject inversely to your comfort rating (less comfortable = more
  sessions) and picking, for each day, whichever subject is most "owed" revision relative
  to its weight, while never scheduling a subject after its own exam date has passed.
- Streaks increment once per calendar day, and reset if a day is missed.
- If a video has no captions/transcript available, you'll get a clear error message.
