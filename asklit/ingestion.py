import os
import hashlib
import re
from pypdf import PdfReader
import docx2txt
from bs4 import BeautifulSoup
import markdown


def get_content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_text_from_pdf(file_path):
    reader = PdfReader(file_path)
    text = ""
    pages = []
    for i, page in enumerate(reader.pages):
        page_text = page.extract_text()
        text += page_text
        pages.append({"text": page_text, "page_number": i + 1})
    return text, pages


def extract_text_from_docx(file_path):
    text = docx2txt.process(file_path)
    return text, [{"text": text, "page_number": 1}]


def extract_text_from_html(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")
        text = soup.get_text(separator="\n")
    return text, [{"text": text, "page_number": 1}]


def extract_text_from_txt(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return text, [{"text": text, "page_number": 1}]


def extract_text(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    elif ext == ".docx":
        return extract_text_from_docx(file_path)
    elif ext in [".html", ".htm"]:
        return extract_text_from_html(file_path)
    elif ext in [".txt", ".md"]:
        return extract_text_from_txt(file_path)
    else:
        raise ValueError(f"Unsupported file extension: {ext}")


def normalize_text(text):
    text = text or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_paragraphs(text):
    return [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", normalize_text(text))
        if paragraph.strip()
    ]


def split_sentences(text):
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])|(?<=\n)\s+", text.strip())
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def split_long_text_on_words(text, max_chars):
    words = text.split()
    if not words:
        return []

    chunks = []
    current_words = []
    current_length = 0

    for word in words:
        if len(word) > max_chars:
            if current_words:
                chunks.append(" ".join(current_words))
                current_words = []
                current_length = 0
            chunks.append(word)
            continue

        separator_length = 1 if current_words else 0
        next_length = current_length + separator_length + len(word)
        if current_words and next_length > max_chars:
            chunks.append(" ".join(current_words))
            current_words = [word]
            current_length = len(word)
        else:
            current_words.append(word)
            current_length = next_length

    if current_words:
        chunks.append(" ".join(current_words))

    return chunks


def split_oversized_block(block, max_chars):
    if len(block) <= max_chars:
        return [block]

    chunks = []
    current_parts = []
    current_length = 0

    for sentence in split_sentences(block):
        if len(sentence) > max_chars:
            if current_parts:
                chunks.append(" ".join(current_parts))
                current_parts = []
                current_length = 0
            chunks.extend(split_long_text_on_words(sentence, max_chars))
            continue

        separator_length = 1 if current_parts else 0
        next_length = current_length + separator_length + len(sentence)
        if current_parts and next_length > max_chars:
            chunks.append(" ".join(current_parts))
            current_parts = [sentence]
            current_length = len(sentence)
        else:
            current_parts.append(sentence)
            current_length = next_length

    if current_parts:
        chunks.append(" ".join(current_parts))

    return chunks


def chunk_text(text, target_size=3000, max_size=5000):
    chunks = []
    current_parts = []
    current_length = 0

    for paragraph in split_paragraphs(text):
        blocks = split_oversized_block(paragraph, max_size)
        for block in blocks:
            separator_length = 2 if current_parts else 0
            next_length = current_length + separator_length + len(block)

            if current_parts and current_length >= target_size:
                chunks.append("\n\n".join(current_parts))
                current_parts = [block]
                current_length = len(block)
            elif current_parts and next_length > max_size:
                chunks.append("\n\n".join(current_parts))
                current_parts = [block]
                current_length = len(block)
            else:
                current_parts.append(block)
                current_length = next_length

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    return chunks


def chunk_pages(pages, target_size=3000, max_size=5000):
    all_chunks = []
    global_chunk_index = 0
    for page in pages:
        page_text = page["text"]
        page_num = page["page_number"]
        page_chunks = chunk_text(page_text, target_size, max_size)
        for chunk in page_chunks:
            all_chunks.append(
                {
                    "content": chunk,
                    "page_number": page_num,
                    "chunk_index": global_chunk_index,
                }
            )
            global_chunk_index += 1
    return all_chunks
