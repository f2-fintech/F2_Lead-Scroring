"""
remark_slots.py - sorts a lead into a priority slot (P1 / P2 / P3 / P4) from the
MIS CREDIT remark and the MIS OPS remark.

Put this file in the SAME folder as allocation.py. Nothing here talks to a
database, so it is safe to import and test on its own.

THE SLOTS
  P1  Act now        - the remarks show something we can fix or follow up:
                       eligibility mentioned, MORE THAN 3 enquiries, OGL, RCU,
                       PD call not attended, CIBIL good, documents problem,
                       customer not picking up.
  P2  Fix in 3 months- bounces in the last 3 months, eligibility short /
                       "not eligible", or any other reason that can be sorted out
                       (a remark with text but no known keyword also lands here).
  P3  Hard to fix    - CIBIL low, 3 or more unsecured loans (USL), bounces older
                       than 3 months, or a very negative reason that will not
                       clear in 3 months (written off, settled, legal case,
                       fraud / negative profile, FOIR or income too weak ...).
  P4  No remark      - neither a credit remark nor an ops remark was given.

A lead can match several reasons. CONFLICT_RULE decides which slot wins:
  "worst_wins" - the most serious slot it qualifies for (P3 beats P2 beats P1).
                 So "CIBIL good" + "bounce in last 3 months" -> P2, and
                 "customer not picking up" + "CIBIL low" -> P3.
  "best_wins"  - the best slot it qualifies for (P1 beats P2 beats P3).

HOW THE MATCHING WORKS
  Plain keyword / pattern matching on the lower-cased remark text - no AI.
  Every match is kept, so the dashboard can show WHY a lead is in its slot.
  To tune: change the numbers just below, or add a spelling to a pattern list
  further down. slot_report.py lists the remarks that matched nothing, which is
  the quickest way to find spellings worth adding.
"""
import re

# ===================================================================
#                        SETTINGS - change here
# ===================================================================
CONFLICT_RULE = "worst_wins"      # or "best_wins"

GOOD_CIBIL_MIN = 700              # a written score at or above this = "CIBIL good"  (P1)
LOW_CIBIL_MAX = 650               # a written score at or below this = "CIBIL low"   (P3)
ENQ_MORE_THAN = 3                 # MORE than this many enquiries = P1
USL_MIN = 3                       # this many unsecured loans or more = P3
BOUNCE_OK_MONTHS = 3              # bounces within this many months = P2, older = P3

# A remark that is only one of these counts as "nothing given" (P4).
EMPTY_REMARKS = {
    "", "-", "--", "na", "n/a", "nil", "none", "null", "nan", "no", "no remark",
    "no remarks", "nothing", "not given", "not available", "not mentioned", "blank",
}

SLOT_ORDER = ["P1", "P2", "P3", "P4"]
_RANK = {"P1": 1, "P2": 2, "P3": 3}

# ===================================================================
#                             PATTERNS
# ===================================================================
_CIBIL = r"(?:ci?bil|cibl|cbil)"
_NUM = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}


def _rx(pattern):
    return re.compile(pattern.replace("@C@", _CIBIL).replace("@N@", _NUM), re.I)


def _to_int(text):
    text = str(text).strip().lower()
    return int(text) if text.isdigit() else _WORD_NUM.get(text)


# ---- P1 -------------------------------------------------------------
_PD_NOT_ATTENDED = [_rx(p) for p in (
    r"\bpd\b\W{0,12}(?:call\W{0,6})?(?:not|no|didn'?t|did\s+not|couldn'?t|could\s+not|unable|failed|pending)"
    r"\W{0,6}(?:attend\w*|answer\w*|pick\w*|receiv\w*|respond\w*|done|conduct\w*|complet\w*|connect\w*)?",
    r"(?:not|no|didn'?t|did\s+not|couldn'?t|could\s+not|unable)\W{0,6}(?:attend\w*|answer\w*|pick\w*|receiv\w*|respond\w*)"
    r"(?:\W+(?:on|in|during|for|at|the|of|a|his|her|their|call|pd\s+call))*\W{0,6}\bpd\b",
)]

_CIBIL_NUMBER = _rx(r"@C@\D{0,15}?(\d{3})\b")
_CIBIL_LOW = [_rx(p) for p in (
    r"@C@(?:\s*score)?\W{0,6}(?:is|was|are)?\W{0,4}(?:not|isn'?t|n't|no)\W{0,3}(?:good|ok|okay|fine|clean|proper)",
    r"@C@(?:\s*score)?\W{0,6}(?:is|was)?\W{0,4}(?:low|poor|bad|weak|negative|damaged|dropped|less|below|issue|problem)",
    r"\b(?:low|poor|bad|weak|negative|less|damaged)\W{0,3}@C@",
)]
_CIBIL_GOOD = [_rx(p) for p in (
    r"@C@(?:\s*score)?\W{0,6}(?:is|was|are)?\W{0,4}(?:good|ok|okay|fine|clean|healthy|excellent|strong|great|decent|positive|above)",
    r"\b(?:good|clean|healthy|excellent|strong|great|decent|positive)\W{0,3}@C@",
)]

_ELIG_SHORT = [_rx(p) for p in (
    r"\bnot\s+eligible\b|\bineligible\b|\bnon[\s-]?eligible\b|\bno\s+eligib\w*",
    r"\belig(?:ib\w*)?\W{0,12}(?:low|less|short|issue|problem|not|insufficient|nil|zero|lower|reduced|gap)\b",
    r"\b(?:low|less|short|insufficient|lower|reduced)\W{0,4}elig(?:ib\w*)?",
)]
_ELIG = _rx(r"\belig(?:ib\w*)?\b")

_ENQ_BEFORE = _rx(r"(?P<pre>more\s*than|above|over|greater\s*than|>)?\s*(?P<n>@N@)\s*(?P<plus>\+)?\s*"
                  r"(?:hard\s*|credit\s*|cibil\s*|bank\s*)?(?:enq|inq)\w*")
_ENQ_AFTER = _rx(r"(?:enq|inq)\w*\s*(?:count|total|are|is|of|nos?\.?|number)?\W{0,3}(?P<n>\d{1,2})\b"
                 r"(?!\s*(?:months?|mnths?|mths?|m\b|yrs?|years?|days?))")
_ENQ_MANY = _rx(r"\b(?:many|multiple|high|lot\s+of|too\s+many|excess\w*|several)\s+(?:hard\s+|credit\s+)?(?:enq|inq)\w*")

_DOCS = [_rx(p) for p in (
    r"\bdoc(?:ument)?s?\b\W{0,20}(?:issue|problem|pending|missing|mismatch|incomplete|not\s+(?:available|provided|received|"
    r"submitted|clear|proper|ok)|required|short|insufficient|awaited|lacking|gap|error|wrong|unclear)",
    r"(?:issue|problem|pending|missing|mismatch|incomplete|insufficient|awaiting|require\w*|need\w*)\W{0,12}\bdoc(?:ument)?s?\b",
    r"\b(?:kyc|itr|gst|form\s*16|bank\s*statement|salary\s*slip|statement)\b\W{0,20}(?:issue|problem|pending|missing|"
    r"mismatch|incomplete|not\s+(?:available|provided|received|submitted|clear|proper))",
)]
_RCU = _rx(r"\brcu\b")
_OGL = _rx(r"\bogl\b")
_NOT_PICKUP = [_rx(p) for p in (
    r"(?:not|no|didn'?t|did\s+not|doesn'?t|does\s+not|isn'?t|never)\W{0,3}"
    r"(?:pick\w*|answer\w*|respond\w*|receiv\w*\W{0,3}call|attend\w*\W{0,3}call|reply\w*)",
    r"\bnot\s+reachable\b|\bunreachable\b|\bnot\s+contactable\b|\bswitch(?:ed)?\s*off\b|\bno\s+response\b|"
    r"\bno\s+answer\b|\bnot\s+responding\b|\bcall\s+not\s+connect\w*\b|\bringing\b",
    r"\bnp\b|\bnr\b|\bdnp\b",
)]

# ---- P2 / P3 --------------------------------------------------------
_BOUNCE = _rx(r"\bbounc\w*")
_WINDOW = _rx(r"(?P<n>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s*\+?\s*"
              r"(?P<u>months?|mnths?|mths?|mon|mos?|m|yrs?|years?)\b")

_RECENT = _rx(r"\b(?:last|past|previous|prev|this|current)\s+(?:month|mnth|week)\b")

_USL_BEFORE = _rx(r"(?P<pre>more\s*than|above|over|greater\s*than|>)?\s*(?P<n>@N@)\s*(?P<plus>\+)?\s*"
                  r"(?:running\s+|active\s+|live\s+)?(?:usl|usls|unsecured|unsec|usecured)\w*")
_USL_AFTER = _rx(r"\b(?:usl|unsecured|unsec)\w*\s*(?:count|loans?|running|active|are|is|of|:|-|=)?\W{0,3}(?P<n>\d{1,2})\b"
                 r"(?!\s*(?:months?|mnths?|mths?|m\b|yrs?|years?|days?|l\b|lakh|k\b|cr))")
_USL_MANY = _rx(r"\b(?:many|multiple|too\s+many|high|several)\s+(?:usl|unsecured)\w*")

# (label, pattern). Each one that matches puts the lead in P3.
_VERY_NEGATIVE = [(label, _rx(p)) for label, p in (
    ("Written off / settled loan", r"write\s*-?\s*off|written\s*-?\s*off|\bsettled\b|\bsettlement\b"),
    ("Legal case", r"suit\s*filed|legal\s*(?:case|notice|action)|court\s*case|lok\s*adalat"),
    ("Default / overdue", r"\bnpa\b|\bsma\b|\bdefault(?:er|ed|s)?\b|\boverdue\b|over\s*due|"
                          r"\bdpd\b\W{0,6}(?:[6-9]\d|\d{3})|\b(?:60|90|120|180)\s*\+?\s*dpd"),
    ("Fraud / negative profile", r"fraud|forged|\bfake\b|fabricat\w*|black\s*list\w*|"
                                 r"\bnegative\s+(?:profile|area|pin\s*code|pincode|pin|employer|company|occupation|industry|list)"),
    ("FOIR / obligations too high", r"\bfoir\W{0,10}(?:high|exceed\w*|more|over|crossed|breach\w*)|"
                                    r"\b(?:high|over|exceed\w*)\W{0,4}foir|obligation\w*\W{0,10}(?:high|more|exceed\w*)|"
                                    r"over\s*leverag\w*"),
    ("Income too low", r"\bincome\W{0,10}(?:low|less|insufficient|not\s+(?:sufficient|enough|matching|proper))|"
                       r"\b(?:low|less|insufficient)\W{0,4}income"),
    ("Age issue", r"\bage\W{0,6}(?:issue|problem|limit|criteria|more|high|bar)|over\s*age"),
    ("Outside policy / high risk", r"not\s+(?:in\s+)?policy|policy\W{0,6}(?:issue|reject\w*|not\s+match\w*)|\bhigh\s*risk\b"),
    ("Negative remark", r"\bnegative\b"),
)]


# ===================================================================
#                             HELPERS
# ===================================================================
def _norm(text):
    t = str(text or "").lower()
    t = re.sub(r"[_\r\n\t]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _is_empty(norm_text):
    plain = re.sub(r"[^\w/ ]+", " ", norm_text)
    plain = " ".join(plain.split())
    return plain in EMPTY_REMARKS


def _negated(t, start):
    """True when a 'no / nil / without' sits right before the match ('no bounces')."""
    before = t[max(0, start - 14):start]
    return bool(re.search(r"(?:\bno|\bnil|\bzero|\bwithout|\bnot\s+any)\W*$", before))


def _analyse(text, source):
    """All the reasons found in ONE remark. Returns a list of
    {"slot", "label", "source", "text"}."""
    t = text
    found = []

    def add(slot, label, snippet):
        found.append({"slot": slot, "label": label, "source": source,
                      "text": str(snippet).strip()[:60]})

    def blank(m):
        nonlocal t
        t = t[:m.start()] + " " * (m.end() - m.start()) + t[m.end():]

    def hits(patterns, check_negation=False):
        out = []
        for rx in (patterns if isinstance(patterns, list) else [patterns]):
            for m in rx.finditer(t):
                if check_negation and _negated(t, m.start()):
                    continue
                out.append(m)
        return out

    def mark(slot, label, patterns, check_negation=False):
        ms = hits(patterns, check_negation)
        if ms:
            add(slot, label, ms[0].group(0))
            for m in ms:
                blank(m)
        return bool(ms)

    # --- PD call not attended (P1)
    mark("P1", "PD call not attended", _PD_NOT_ATTENDED)

    # --- CIBIL: written low wording, a written score, written good wording
    mark("P3", "CIBIL low", _CIBIL_LOW, check_negation=True)
    got_good = False
    for m in list(_CIBIL_NUMBER.finditer(t)):
        score = int(m.group(1))
        if not 300 <= score <= 900:
            continue
        if score <= LOW_CIBIL_MAX:
            add("P3", f"CIBIL {score} (low)", m.group(0))
        elif score >= GOOD_CIBIL_MIN:
            add("P1", f"CIBIL {score} (good)", m.group(0))
            got_good = True
        blank(m)
    if mark("P1", "CIBIL good", _CIBIL_GOOD):
        got_good = True

    # --- Eligibility: shortfall (P2) vs just mentioned (P1)
    mark("P2", "Eligibility short / not eligible", _ELIG_SHORT)
    mark("P1", "Eligibility mentioned", _ELIG)

    # --- Enquiries: MORE than ENQ_MORE_THAN (P1)
    best_enq, enq_text = 0, ""
    for rx in (_ENQ_BEFORE, _ENQ_AFTER):
        for m in rx.finditer(t):
            n = _to_int(m.group("n"))
            if n is None:
                continue
            gd = m.groupdict()
            if gd.get("pre") or gd.get("plus"):
                n += 1                       # "more than 3" / "3+" -> at least 4
            if n > best_enq:
                best_enq, enq_text = n, m.group(0)
    if best_enq > ENQ_MORE_THAN:
        add("P1", f"More than {ENQ_MORE_THAN} enquiries", enq_text)
    elif _ENQ_MANY.search(t):
        add("P1", "Many enquiries", _ENQ_MANY.search(t).group(0))

    # --- Unsecured loans: USL_MIN or more (P3)
    best_usl, usl_text = 0, ""
    for rx in (_USL_BEFORE, _USL_AFTER):
        for m in rx.finditer(t):
            n = _to_int(m.group("n"))
            if n is None:
                continue
            if m.groupdict().get("pre"):
                n += 1
            if n > best_usl:
                best_usl, usl_text = n, m.group(0)
    if best_usl >= USL_MIN:
        add("P3", f"{USL_MIN}+ unsecured loans", usl_text)
    elif _USL_MANY.search(t):
        add("P3", "Many unsecured loans", _USL_MANY.search(t).group(0))

    # --- Bounces: within BOUNCE_OK_MONTHS (P2), older (P3)
    for m in hits(_BOUNCE, check_negation=True):
        context = t[max(0, m.start() - 25): m.end() + 45]
        months = None
        for w in _WINDOW.finditer(context):
            n = _to_int(w.group("n"))
            if n is None:
                continue
            value = n * 12 if w.group("u").lower().startswith("y") else n
            months = value if months is None else max(months, value)
        if months is None and _RECENT.search(context):
            months = 1                       # "last month" / "this month"
        if months is None:
            add("P2", "Bounce mentioned (period not given)", context.strip())
        elif months <= BOUNCE_OK_MONTHS:
            add("P2", f"Bounces in the last {months} month{'' if months == 1 else 's'}", context.strip())
        else:
            add("P3", f"Bounces in the last {months} months", context.strip())
        break
    for m in hits(_BOUNCE):
        blank(m)

    # --- Operational reasons (P1)
    mark("P1", "Documents problem", _DOCS, check_negation=True)
    mark("P1", "RCU mentioned", _RCU)
    mark("P1", "OGL mentioned", _OGL)
    mark("P1", "Customer not picking up", _NOT_PICKUP)

    # --- Very negative reasons (P3)
    for label, rx in _VERY_NEGATIVE:
        mark("P3", label, [rx], check_negation=True)

    return found


# ===================================================================
#                              MAIN
# ===================================================================
def classify_remarks(credit_remark, ops_remark, conflict_rule=None):
    """
    -> {"slot": "P1".."P4",
        "reasons": ["RCU mentioned (ops)", ...],     # the ones that decided the slot first
        "signals": [{"slot", "label", "source", "text"}, ...],
        "other": True when there was text but no known keyword (so it fell to P2)}
    """
    rule = conflict_rule or CONFLICT_RULE
    signals, any_text = [], False
    for source, raw in (("credit", credit_remark), ("ops", ops_remark)):
        t = _norm(raw)
        if _is_empty(t):
            continue
        any_text = True
        signals += _analyse(t, source)

    if not any_text:
        return {"slot": "P4", "reasons": ["No credit or ops remark given"], "signals": [], "other": False}
    if not signals:
        return {"slot": "P2", "reasons": ["Other reason (no known keyword)"], "signals": [], "other": True}

    ranks = [_RANK[s["slot"]] for s in signals]
    chosen = max(ranks) if rule == "worst_wins" else min(ranks)
    slot = f"P{chosen}"

    # one line per distinct reason, the deciding ones first
    seen, reasons = set(), []
    for s in sorted(signals, key=lambda s: (_RANK[s["slot"]] != chosen, _RANK[s["slot"]])):
        line = f"{s['label']} ({s['source']})"
        if line not in seen:
            seen.add(line)
            reasons.append(line)
    return {"slot": slot, "reasons": reasons, "signals": signals, "other": False}