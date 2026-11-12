"""Both team members' About texts are present, rendered (no raw Markdown) and laid out the same way."""

import re


def _about_blocks(html):
    team = html[html.index('<section class="teamsec"') :]
    return re.findall(r'<details class="about">(.*?)</details>', team, re.S)


def test_both_cards_have_a_symmetric_rendered_about(env):
    html = env["client"].get("/").text
    blocks = _about_blocks(html)
    assert len(blocks) == 2
    sangeeta, chittranjan = blocks
    assert "coming soon" not in html.lower()
    for b in blocks:
        assert '<p class="q">“<em>' in b and "<ul>" in b and '<p class="ct">' in b
        assert "*" not in re.sub(r"<[^>]+>", "", b)  # italics and bold are HTML, not asterisks
    assert "I build AI that knows when to hand the decision back to a human." in sangeeta
    assert "<strong>AI &amp; agents:</strong>" in sangeeta
    assert "Never trust the channel." in chittranjan
    assert "Open to Solutions Architect / Forward Deployed Engineer roles from May 2027" in chittranjan
    assert "Open to Forward Deployed Engineer (FDE) consulting" not in html  # README only
    assert "MBBS" not in html
