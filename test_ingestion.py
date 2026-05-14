import unittest

from asklit.ingestion import chunk_pages, chunk_text


def test_chunk_text_keeps_words_intact_at_boundaries():
    case = unittest.TestCase()
    text = " ".join(f"word{i}" for i in range(80))

    chunks = chunk_text(text, target_size=120, max_size=160)

    case.assertGreater(len(chunks), 1)
    case.assertEqual(" ".join(chunks).split(), text.split())


def test_chunk_text_prefers_paragraph_boundaries():
    case = unittest.TestCase()
    text = "\n\n".join([
        "First paragraph has a complete thought.",
        "Second paragraph should stay together.",
        "Third paragraph also stays together.",
    ])

    chunks = chunk_text(text, target_size=40, max_size=90)

    case.assertEqual(
        chunks,
        [
            "First paragraph has a complete thought.\n\nSecond paragraph should stay together.",
            "Third paragraph also stays together.",
        ],
    )


def test_chunk_pages_tracks_chunk_indices_across_pages():
    case = unittest.TestCase()
    pages = [
        {"text": " ".join(f"alpha{i}" for i in range(40)), "page_number": 1},
        {"text": " ".join(f"beta{i}" for i in range(40)), "page_number": 2},
    ]

    chunks = chunk_pages(pages, target_size=100, max_size=140)

    case.assertEqual(
        [chunk["chunk_index"] for chunk in chunks],
        list(range(len(chunks))),
    )
    case.assertEqual({chunk["page_number"] for chunk in chunks}, {1, 2})
