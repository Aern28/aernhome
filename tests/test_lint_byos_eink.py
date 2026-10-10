# created-by: opus
# created: 2026-10-10
# purpose: push-time lint for TRMNL BYOS templates - 1-bit e-ink has no grey: text colour <= #555 or #fff, text >= 12px (ratchet over a 10/10 baseline)
# lifespan: helper
# project: rules-to-lint
"""rules/projects.md (AernHome): "1-bit e-ink has no grey - <=#555 on white, #fff on black,
hard threshold not dither, >=12px". A grey text colour lands on one side of the
threshold or the other, so it either vanishes or turns black; text under 12px breaks up.

RATCHET: the templates already broke the rule on 10/10 (BASELINE below). Changing
those is a visual call for Aern, so they are listed, not fixed. Any NEW violation,
or more of an existing one, fails the push. When a baseline line gets fixed, lower
or delete it here - a stale baseline also fails, so it cannot quietly grow back.
"""
import collections
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "byos_templates")
# value runs to ; or a quote; braces allowed so Jinja {% if %}#aaa{% else %}#888 is read
COLOR = re.compile(r"(?<![-\w])color\s*:\s*([^;\"']+)", re.I)
HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
SIZE = re.compile(r"font-size\s*:\s*([0-9.]+)px", re.I)
MIN_PX = 12

# (template, finding) -> count. Emptied 2026-10-10: Aern approved fixing all 19 (#666 and
# #888 on white -> #555; #ddd and #aaa on black -> #fff). Any violation now fails the push.
BASELINE = {}


def _bright(h):
    h = h if len(h) == 6 else "".join(c * 2 for c in h)
    return max(int(h[i:i + 2], 16) for i in (0, 2, 4))


def scan_text(name, text):
    found = collections.Counter()
    for line in text.splitlines():
        for m in COLOR.finditer(line):
            for h in HEX.findall(m.group(1)):
                if 0x55 < _bright(h) < 0xFF:
                    found[(name, "grey text #" + h.lower())] += 1
        for m in SIZE.finditer(line):
            if float(m.group(1)) < MIN_PX:
                found[(name, "text %spx" % m.group(1))] += 1
    return found


def scan():
    found = collections.Counter()
    for p in sorted(glob.glob(os.path.join(TEMPLATES, "*.html"))):
        with open(p, encoding="utf-8") as f:
            found += scan_text(os.path.basename(p), f.read())
    return found


def worse_than(found, baseline):
    """Violations beyond the baseline: new kinds, or more of an existing one."""
    return {k: v for k, v in found.items() if v > baseline.get(k, 0)}


def stale_lines(found, baseline):
    """Baseline lines that got (partly) fixed and must be lowered or deleted."""
    return {k: v for k, v in baseline.items() if found.get(k, 0) < v}


class EinkTemplates(unittest.TestCase):
    def test_no_new_eink_violations(self):
        worse = worse_than(scan(), BASELINE)
        self.assertFalse(worse, "e-ink rule broken (grey text or <12px) beyond the 10/10 baseline: %s. "
                                "Use #000-#555 on white or #fff on black, and >=12px." % worse)

    def test_baseline_is_not_stale(self):
        stale = stale_lines(scan(), BASELINE)
        self.assertFalse(stale, "fixed - lower or delete these BASELINE lines: %s" % stale)

    def test_ratchet_logic(self):
        # mutation pass 10/10: nothing in the real templates is fixed yet, so prove both
        # directions of the ratchet on a fixture
        base = {("a.html", "grey text #666"): 2}
        self.assertEqual(stale_lines(collections.Counter({("a.html", "grey text #666"): 1}), base),
                         {("a.html", "grey text #666"): 2})
        self.assertEqual(stale_lines(collections.Counter(), base), {("a.html", "grey text #666"): 2})
        self.assertEqual(stale_lines(collections.Counter({("a.html", "grey text #666"): 2}), base), {})
        self.assertEqual(worse_than(collections.Counter({("a.html", "grey text #666"): 3}), base),
                         {("a.html", "grey text #666"): 3})
        self.assertEqual(worse_than(collections.Counter({("b.html", "text 10px"): 1}), base), {("b.html", "text 10px"): 1})
        self.assertEqual(worse_than(collections.Counter({("a.html", "grey text #666"): 2}), base), {})

    def test_scanner_catches_the_real_shapes(self):
        sample = ('<div style="color:#666;font-size:11px">x</div>\n'
                  '<div style="color:{% if a %}#aaa{% else %}#888{% endif %};">y</div>\n'
                  '<div style="color: #555; background-color:#ddd; font-size:12px">ok</div>\n'
                  '<div style="color:#fff">ok on black</div>')
        f = scan_text("t", sample)
        self.assertEqual(set(f), {("t", "grey text #666"), ("t", "text 11px"), ("t", "grey text #aaa"), ("t", "grey text #888")})


if __name__ == "__main__":
    unittest.main()
