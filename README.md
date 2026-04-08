# מוניטור ועדות הכנסת — תחבורה ואנרגיה

מערכת אוטומטית שסורקת דיוני ועדות הכנסת העתידיים, מסננת רק דיונים בתחומי תחבורה ואנרגיה, ומייצרת Dashboard ודוח אימייל.

---

## תוכן עניינים
1. [התקנה מקומית](#1-התקנה-מקומית)
2. [יצירת Gmail App Password](#2-יצירת-gmail-app-password)
3. [הגדרת GitHub Secrets](#3-הגדרת-github-secrets)
4. [הפעלת GitHub Pages](#4-הפעלת-github-pages)
5. [הפעלה ידנית מ-GitHub Actions](#5-הפעלה-ידנית-מ-github-actions)
6. [הפעלה מהטרמינל](#6-הפעלה-מהטרמינל)
7. [ה-Dashboard — כתובת וגישה](#7-ה-dashboard--כתובת-וגישה)

---

## 1. התקנה מקומית

```bash
# שכפל את הריפו
git clone https://github.com/izikdaniel-alt/knesset-committee-tracker.git
cd knesset-committee-tracker

# צור סביבה וירטואלית (מומלץ)
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows

# התקן תלויות
pip install -r requirements.txt

# צור קובץ .env מתוך הדוגמה
cp .env.example .env
```

ערוך את קובץ `.env` ומלא את הפרטים:

```
GEMINI_API_KEY=AIza...
GMAIL_USER=your_gmail@gmail.com
GMAIL_APP_PASS=xxxx xxxx xxxx xxxx
RECIPIENT_EMAIL=recipient@example.com
GITHUB_PAGES_URL=https://izikdaniel-alt.github.io/knesset-committee-tracker
```

---

## 2. יצירת Gmail App Password

> **חשוב:** App Password שונה מסיסמת ה-Gmail הרגילה. הוא נוצר ספציפית עבור אפליקציות.

**שלב 1** — הכנס לחשבון Google שלך:
[myaccount.google.com](https://myaccount.google.com)

**שלב 2** — לחץ על **"Security"** (אבטחה) בתפריט השמאלי.

**שלב 3** — ודא ש-**"2-Step Verification"** (אימות דו-שלבי) **מופעל**.
אם לא — הפעל אותו (חובה לפני יצירת App Password).

**שלב 4** — בשורת החיפוש של הגדרות Google, חפש **"App passwords"** ולחץ עליו.
(או גש ישירות: Security → How you sign in to Google → App passwords)

**שלב 5** — בשדה **"App name"** הקלד שם כלשהו (למשל: `knesset-monitor`).

**שלב 6** — לחץ **"Create"** — תקבל סיסמה של 16 תווים (ללא רווחים).

**שלב 7** — העתק את הסיסמה לשדה `GMAIL_APP_PASS` בקובץ `.env`.

---

## 3. הגדרת GitHub Secrets

ב-GitHub, גש ל-**Settings → Secrets and variables → Actions → New repository secret**
והוסף את הסודות הבאים:

| Secret name | ערך |
|---|---|
| `GEMINI_API_KEY` | המפתח מ-aistudio.google.com |
| `GMAIL_USER` | כתובת ה-Gmail ששולחת (למשל `my@gmail.com`) |
| `GMAIL_APP_PASS` | ה-App Password שיצרת בשלב 2 |
| `RECIPIENT_EMAIL` | כתובת המייל שמקבלת את הדוח |
| `GITHUB_PAGES_URL` | `https://izikdaniel-alt.github.io/knesset-committee-tracker` |

---

## 4. הפעלת GitHub Pages

לאחר ה-push הראשון שיצר את תיקיית `docs/`:

1. גש ב-GitHub ל-**Settings** של הריפו
2. בתפריט הצד בחר **Pages**
3. תחת **Source** בחר **"Deploy from a branch"**
4. Branch: **main** | Folder: **`/docs`**
5. לחץ **Save**

לאחר כ-2 דקות, ה-Dashboard יהיה זמין בכתובת:
```
https://izikdaniel-alt.github.io/knesset-committee-tracker
```

---

## 5. הפעלה ידנית מ-GitHub Actions

1. גש ב-GitHub ל-**Actions**
2. בחר את ה-workflow **"Knesset Committee Monitor"**
3. לחץ **"Run workflow"** ← **"Run workflow"**
4. המערכת תרוץ, תעדכן את ה-Dashboard, ותשלח אימייל

---

## 6. הפעלה מהטרמינל

**הרצה מלאה** (מייצר Dashboard + שולח אימייל):
```bash
python knesset_monitor.py
```

**מצב Preview** (מדפיס לטרמינל בלבד, ללא שמירה או אימייל):
```bash
python knesset_monitor.py --preview
```

---

## 7. ה-Dashboard — כתובת וגישה

ה-Dashboard הוא קובץ HTML סטטי שנשמר ב-`docs/index.html` ומוגש דרך GitHub Pages.

**כתובת ה-Dashboard:**
```
https://izikdaniel-alt.github.io/knesset-committee-tracker
```

**מה מוצג ב-Dashboard:**
- **4 כרטיסי סיכום** — סה"כ רלוונטיים, תחבורה, אנרגיה, וסה"כ שנסרקו
- **כפתורי פילטר** — ניתן לסנן בין "הכל" / "תחבורה" / "אנרגיה" בלחיצה
- **טבלת דיונים** — עם נושא, ועדה, תאריך, תחום, וקישור ישיר לאתר הכנסת
- **עדכון אוטומטי** — ה-Dashboard מתעדכן בכל הרצה (ראשון ורביעי ב-07:00 ישראל)

---

## לוח זמנים אוטומטי

| יום | שעה (ישראל) | שעה (UTC) |
|---|---|---|
| ראשון | 07:00 | 05:00 |
| רביעי | 07:00 | 05:00 |

---

## מבנה הפרויקט

```
.
├── knesset_monitor.py          # הקוד הראשי
├── requirements.txt
├── .env.example                # תבנית לקובץ .env
├── .env                        # (לא ב-git) פרטי ה-credentials
├── docs/
│   └── index.html              # ה-Dashboard (מתעדכן אוטומטית)
└── .github/
    └── workflows/
        └── monitor.yml         # GitHub Actions workflow
```
