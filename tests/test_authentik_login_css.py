"""Check authentik-assets/countdown.css, the sign-in's look, against how Authentik serves a brand's CSS."""

import re
from pathlib import Path

ASSETS = Path(__file__).parent.parent / "authentik-assets"
CSS = ASSETS / "countdown.css"
LOGIN_TF = Path(__file__).parent.parent / "terraform" / "login.tf"


def _rules():
    """Return the CSS without its comments."""
    return re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.DOTALL)


def test_the_css_has_no_characters_authentik_escapes():
    """The page's copy of the CSS has < > & escaped, and in compatibility mode that copy is the only one."""
    assert set(CSS.read_text()) & set("<>&") == set()


def test_every_card_rule_names_a_card_in_a_flow():
    """The user settings page shows a stage's card too: a bare ak-flow-card rule would restyle its form."""
    cards = re.findall(r"(\S*\s*)ak-flow-card\b", _rules())

    assert cards
    assert {before.strip() for before in cards} == {':host([part="challenge"])', '[part="challenge"]'}


def test_every_file_the_sign_in_names_is_shipped():
    """The CSS and the brand name files by their served path; each must be in authentik-assets/, which deploys them."""
    names = set(re.findall(r"/static/dist/assets/countdown/([\w.-]+)", CSS.read_text() + LOGIN_TF.read_text()))

    assert {"wallpaper.jpg", "screen-landscape.png", "countdown-pill.svg"} <= names
    assert sorted(name for name in names if not (ASSETS / name).is_file()) == []
