"""
generate_fixtures.py - Creates HTML and PDF fixtures for Admission Deadline Tracker.
Used for offline evaluation and deterministic automated test runs (TC1 - TC4).
"""

import os
import pymupdf

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
os.makedirs(FIXTURES_DIR, exist_ok=True)

# 1. College A Initial Notice (15 May 2026)
college_a_initial = """<!DOCTYPE html>
<html>
<head>
    <title>NIT Alpha Admissions 2026</title>
</head>
<body>
    <header>
        <div class="banner">NIT Alpha Official Admission Portal</div>
        <div class="timestamp">Generated: 2026-03-01 10:00:00</div>
    </header>
    <main>
        <h1>ADMISSION NOTIFICATION 2026</h1>
        <div class="notice-section">
            <p>Applications are invited for admission into B.Tech Computer Science for Academic Year 2026.</p>
            <p><strong>The last date for submission of online application forms for B.Tech Computer Science (2026) is 15 May 2026.</strong></p>
            <p>Candidates must ensure all qualifying documents are verified before final submission.</p>
        </div>
    </main>
    <footer>
        <p>&copy; 2026 NIT Alpha. All rights reserved.</p>
    </footer>
</body>
</html>
"""

# 2. College A Amendment / Corrigendum Notice (Extended to 25 May 2026)
college_a_amendment = """<!DOCTYPE html>
<html>
<head>
    <title>NIT Alpha Admissions 2026 - Corrigendum</title>
</head>
<body>
    <header>
        <div class="banner">NIT Alpha Official Admission Portal - Updated</div>
        <div class="timestamp">Generated: 2026-05-10 14:30:00</div>
    </header>
    <main>
        <h1>CORRIGENDUM & DEADLINE EXTENSION NOTICE</h1>
        <div class="notice-section">
            <p>Attention Candidates for B.Tech Computer Science (Academic Year 2026):</p>
            <p><strong>In continuation of the previous notification, the deadline for submission of online application forms for B.Tech Computer Science (2026) has been extended up to 25 May 2026.</strong></p>
            <p>No further extensions will be granted under any circumstances.</p>
        </div>
    </main>
    <footer>
        <p>&copy; 2026 NIT Alpha. All rights reserved.</p>
    </footer>
</body>
</html>
"""

# 3. College B B.Tech Notice (10 June 2026)
college_b_btech = """<!DOCTYPE html>
<html>
<head>
    <title>Apex Technical University - B.Tech Admissions 2026</title>
</head>
<body>
    <h1>Apex Technical University Admissions</h1>
    <div class="notice-section">
        <h2>Engineering Faculty Admissions 2026</h2>
        <p>Official Notification: The application deadline for B.Tech Mechanical Engineering is 10 June 2026.</p>
    </div>
</body>
</html>
"""

# 4. College B B.Sc Notice (20 June 2026)
college_b_bsc = """<!DOCTYPE html>
<html>
<head>
    <title>Apex Technical University - B.Sc Admissions 2026</title>
</head>
<body>
    <h1>Apex Technical University Admissions</h1>
    <div class="notice-section">
        <h2>Science Faculty Admissions 2026</h2>
        <p>Official Notification: The application deadline for B.Sc Data Science is 20 June 2026.</p>
    </div>
</body>
</html>
"""

# 5. Simulated 403 Forbidden marker file
simulated_403 = """<!DOCTYPE html>
<html>
<head><title>403 Forbidden</title></head>
<body>
    <h1>403 Forbidden</h1>
    <p>Access to Metro State Polytechnic admissions portal is restricted by WAF security policy.</p>
</body>
</html>
"""

# Write HTML files
with open(os.path.join(FIXTURES_DIR, "college_a_initial.html"), "w", encoding="utf-8") as f:
    f.write(college_a_initial)

with open(os.path.join(FIXTURES_DIR, "college_a_amendment.html"), "w", encoding="utf-8") as f:
    f.write(college_a_amendment)

with open(os.path.join(FIXTURES_DIR, "college_b_btech.html"), "w", encoding="utf-8") as f:
    f.write(college_b_btech)

with open(os.path.join(FIXTURES_DIR, "college_b_bsc.html"), "w", encoding="utf-8") as f:
    f.write(college_b_bsc)

with open(os.path.join(FIXTURES_DIR, "simulated_403.html"), "w", encoding="utf-8") as f:
    f.write(simulated_403)

# 6. Generate real PDF Notice using pymupdf
pdf_path = os.path.join(FIXTURES_DIR, "college_d_notice.pdf")
doc = pymupdf.open()
page = doc.new_page(width=595, height=842) # A4
text_content = """INSTITUTE OF CHEMICAL TECHNOLOGY
OFFICIAL ADMISSION NOTIFICATION 2026

POSTGRADUATE ADMISSIONS (AUTUMN CYCLE)
Programme: M.Tech Chemical Engineering
Academic Year: 2026

It is hereby notified for information of all prospective candidates that the
official deadline for submission of completed online application forms for
M.Tech Chemical Engineering (2026) is 30 June 2026.

Incomplete applications or applications received after 30 June 2026 shall be summarily rejected.
Approved by Office of Admissions, ICT.
"""
rect = pymupdf.Rect(50, 50, 545, 792)
page.insert_textbox(rect, text_content, fontsize=12, fontname="helv")
doc.save(pdf_path)
doc.close()

print(f"Generated all synthetic fixtures in {FIXTURES_DIR}:")
for fname in os.listdir(FIXTURES_DIR):
    print(f" - {fname} ({os.path.getsize(os.path.join(FIXTURES_DIR, fname))} bytes)")
