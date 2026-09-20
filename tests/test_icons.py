import unittest
from unittest.mock import patch

from herdr_bar.icons import enabled, logo_for
from herdr_bar.items import Item
from herdr_bar.render import Row, render_row
from herdr_bar.textutil import strip_ansi, visible_width
from herdr_bar.theme import Theme


class IconTests(unittest.TestCase):
    def test_auto_falls_back_without_font(self):
        with patch("herdr_bar.icons.font_available", return_value=False):
            self.assertFalse(enabled("auto"))
            self.assertTrue(enabled("font"))
        with patch("herdr_bar.icons.font_available", return_value=True):
            self.assertTrue(enabled("auto"))
            self.assertFalse(enabled("none"))
            self.assertFalse(enabled("invalid"))

    def test_aliases_and_unknown_agents(self):
        self.assertEqual(logo_for("Codex"), "\ue1a1")
        self.assertEqual(logo_for("Claude Code"), "\ue1a0")
        self.assertEqual(logo_for("cursor-agent"), logo_for("cursor"))
        self.assertEqual(logo_for("open-code"), logo_for("opencode"))
        self.assertEqual(logo_for("codex:model"), logo_for("codex"))
        self.assertEqual(logo_for("unrecognised"), "")
        self.assertEqual(logo_for(None), "")

    def test_logos_preserve_width_and_name_highlights(self):
        item = Item("agent", "agent:1", "Fix search", agent="codex", agent_name="helper")
        row = Row(item, (), {item.fields.index("helper"): (0, 1)})
        theme = Theme(selection_background="237")
        theme.agent_icons = True
        for width in (24, 46, 80, 120):
            for selected in (True, False):
                rendered = render_row(theme, row, width, selected, 0, False)
                self.assertEqual(visible_width(rendered), width)
        rendered = render_row(theme, row, 80, True, 0, False)
        self.assertTrue(strip_ansi(rendered).startswith("▌ \ue1a1 "))
        self.assertIn("@helper", strip_ansi(rendered))
        self.assertIn("\x1b[4m", rendered)
        theme.agent_icons = False
        plain = strip_ansi(render_row(theme, row, 80, False, 0, False))
        self.assertNotIn("\ue1a1", plain)
        self.assertIn("@helper", plain)

    def test_leading_logo_replaces_vendor_label_even_on_narrow_rows(self):
        theme = Theme()
        theme.agent_icons = True
        for agent in ("codex", "claude", "unrecognised"):
            row = Row(Item("agent", "agent:1", "Fix search", agent=agent), ())
            for width in (24, 46, 80):
                plain = strip_ansi(render_row(theme, row, width, False, 0, False))
                self.assertEqual(visible_width(plain), width)
                if logo_for(agent):
                    self.assertTrue(plain.startswith("  " + logo_for(agent) + " "))
                    self.assertNotIn(agent, plain)
                elif width == 80:
                    self.assertIn(agent, plain)
        theme.agent_icons = False
        row = Row(Item("agent", "agent:1", "Fix search", agent="codex"), ())
        self.assertIn("codex", strip_ansi(render_row(theme, row, 80, False, 0, False)))
