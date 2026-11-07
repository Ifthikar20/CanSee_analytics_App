"""Tests for the RAG chunker."""
from apps.rag.services.chunker import chunk_text


class TestChunkText:
    def test_empty_input(self):
        assert chunk_text("") == []
        assert chunk_text("   \n\n  ") == []

    def test_short_text_one_chunk(self):
        chunks = chunk_text("This is a short paragraph about analytics tools.")
        assert len(chunks) == 1
        assert "analytics" in chunks[0].text
        assert chunks[0].token_count > 0

    def test_long_text_split_with_overlap(self):
        words = " ".join(["word"] * 800)
        chunks = chunk_text(words, target_words=300, overlap_words=40)
        assert len(chunks) >= 3
        # Chunks should each be roughly target_words long.
        for c in chunks[:-1]:
            assert len(c.text.split()) <= 300

    def test_explicit_section_markers_become_labels(self):
        text = (
            "Intro paragraph.\n\n"
            "=== MAIN WEBSITE ===\n"
            "Body about products and features.\n\n"
            "=== WHAT GOOGLE SAYS ===\n"
            "Snippets and competitor names."
        )
        chunks = chunk_text(text)
        labels = {c.section_label for c in chunks}
        assert "MAIN WEBSITE" in labels
        assert "WHAT GOOGLE SAYS" in labels

    def test_default_label_used_when_no_markers(self):
        chunks = chunk_text("Plain text with no headings.", default_label="homepage")
        assert chunks[0].section_label == "homepage"
