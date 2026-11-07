"""extract_response_brands: markdown-link and card-style answer shapes.

Regression for the gpt-4o-mini undercount: web-search answers wrap each
entry name in a markdown link (often bold, without list numbering), and
the extractor previously saw zero brands in them.
"""
from apps.citations.services.url_analytics import extract_response_brands


def _names(text):
    return [e["name"] for e in extract_response_brands(text)]


class TestMarkdownLinkAnswers:
    def test_numbered_items_with_linked_names(self):
        text = (
            "1. **[Rosalind Coffee](https://rosalindcoffeetx.com/?utm_source=openai)** "
            "— a Latino-owned roaster.\n"
            "2. **[Bloom Cafe](http://bloomcafetx.com/)** — family-owned shop.\n"
        )
        assert _names(text) == ["Rosalind Coffee", "Bloom Cafe"]

    def test_card_style_bold_linked_lines_without_numbering(self):
        text = (
            "Here are some coffee shops:\n\n"
            "**[Rosalind Coffee](https://rosalindcoffeetx.com/?utm_source=openai)**\n"
            "Closed · Coffee shop · $10–20 · 4.6 (1194 reviews)\n"
            "A specialty roaster in downtown Garland.\n\n"
            "**[The Vive Coffee](https://www.thevivecoffee.com/)**\n"
            "Closed · Coffee shop · $1–10 · 4.8 (105 reviews)\n"
        )
        assert _names(text) == ["Rosalind Coffee", "The Vive Coffee"]

    def test_bare_link_line_without_bold(self):
        text = (
            "[Beeso Coffee at Firewheel](https://beesocoffee.com/)\n"
            "A welcoming café in Firewheel Town Center.\n"
        )
        assert _names(text) == ["Beeso Coffee at Firewheel"]

    def test_bold_only_line_without_link(self):
        text = "**Oak Cliff Coffee Roasters**\nA Dallas roastery.\n"
        assert _names(text) == ["Oak Cliff Coffee Roasters"]

    def test_headings_and_section_labels_are_not_brands(self):
        text = (
            "**Best places to start:**\n"
            "**A very long bold sentence that is clearly not a brand name at all**\n"
            "**Description**\n"
            "**[Rosalind Coffee](https://rosalindcoffeetx.com/)**\n"
        )
        assert _names(text) == ["Rosalind Coffee"]

    def test_plain_prose_lines_stay_ignored(self):
        text = (
            "These establishments offer a range of coffee experiences.\n"
            "Visit them soon.\n"
        )
        assert _names(text) == []

    def test_classic_numbered_bold_still_works(self):
        text = "1. **Qapital** — rule-based saving.\n2. **Cleo** — chat-first.\n"
        assert _names(text) == ["Qapital", "Cleo"]
