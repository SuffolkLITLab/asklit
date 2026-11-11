import io
import zipfile
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from asklit.hosted.models import Attachment, Conversation, RateBucket
from asklit.hosted.uploads import convert_upload, MAX_BYTES, CONVERSIONS
from test_api import new_bot, publish


def setup(hosted, enabled=True):
    bot = new_bot(hosted)
    bot["config"]["uploads_enabled"] = enabled
    bot["config"]["upload_prompt"] = "Identify deadlines in uploaded documents."
    c = hosted[3]
    saved = c.put(f"/api/bots/{bot['id']}", json={"version": bot["version"], "config": bot["config"]})
    assert saved.status_code == 200
    bot = saved.json()
    publish(hosted, bot)
    conv = c.post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()
    headers = {"Authorization": "Bearer " + conv["token"]}
    return bot, conv, headers


def upload(hosted, conv, headers, data=b"File deadline is Friday.", name="notice.txt"):
    return hosted[3].post(f"/api/public/conversations/{conv['id']}/attachments",
                          headers=headers, files={"file": (name, data)})


def test_upload_is_private_context_with_author_prompt_and_cascades(hosted):
    bot, conv, headers = setup(hosted)
    result = upload(hosted, conv, headers)
    assert result.status_code == 200, result.text
    assert "text" not in result.json()
    assert hosted[5] == []  # Conversion never invokes a model.
    base = f"/api/public/conversations/{conv['id']}"
    answer = hosted[3].post(base + "/messages", headers=headers, json={"message": "When is my deadline?", "request_id": "one"})
    assert answer.status_code == 200
    messages = hosted[5][-1]
    assert any("Identify deadlines" in m["content"] for m in messages if m["role"] == "system")
    assert any("File deadline is Friday." in m["content"] for m in messages if m["role"] == "user")
    assert all("File deadline is Friday." not in m["content"] for m in messages if m["role"] == "system")
    assert hosted[3].get(base + "/attachments").status_code == 404
    other = hosted[3].post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()
    other_headers = {"Authorization": "Bearer " + other["token"]}
    assert hosted[3].get(base + "/attachments", headers=other_headers).status_code == 404
    assert hosted[3].delete(f"/api/public/conversations/{other['id']}/attachments/{result.json()['id']}", headers=other_headers).status_code == 404
    assert "Friday" not in hosted[3].get(f"/api/bots/{bot['id']}").text
    assert hosted[3].delete(base, headers=headers).status_code == 200
    with hosted[1].transaction() as db:
        assert db.scalar(select(func.count()).select_from(Attachment)) == 0


def test_disabled_and_revision_pinning(hosted):
    bot, conv, headers = setup(hosted, enabled=False)
    assert upload(hosted, conv, headers).status_code == 403
    bot["config"]["uploads_enabled"] = True
    saved = hosted[3].put(f"/api/bots/{bot['id']}", json={"version": bot["version"], "config": bot["config"]})
    assert saved.status_code == 200
    publish(hosted, saved.json())
    assert upload(hosted, conv, headers).status_code == 403
    new = hosted[3].post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()
    assert upload(hosted, new, {"Authorization": "Bearer " + new["token"]}).status_code == 200


def test_upload_caps_removal_and_failed_attempts(hosted):
    _, conv, headers = setup(hosted)
    assert upload(hosted, conv, headers, b"x" * (MAX_BYTES + 1)).status_code == 413
    assert upload(hosted, conv, headers, b"x" * 30001).status_code == 422
    files = [upload(hosted, conv, headers, name=f"file{i}.txt").json() for i in range(3)]
    assert upload(hosted, conv, headers).status_code == 422
    base = f"/api/public/conversations/{conv['id']}/attachments"
    assert hosted[3].delete(base + "/" + files[0]["id"], headers=headers).status_code == 200
    assert upload(hosted, conv, headers).status_code == 200
    with hosted[1].transaction() as db:
        assert db.get(Conversation, conv["id"]).busy_until == 0
        assert max(db.scalars(select(RateBucket.count))) >= 6


def test_upload_rechecks_access_after_conversion(hosted, monkeypatch):
    bot, conv, headers = setup(hosted)
    def convert(*args, **kwargs):
        hosted[3].post(f"/api/bots/{bot['id']}/unpublish")
        return {"text": "private"}
    monkeypatch.setattr("asklit.hosted.public.convert_upload", convert)
    assert upload(hosted, conv, headers).status_code == 404
    with hosted[1].transaction() as db:
        assert db.get(Conversation, conv["id"]).busy_until == 0
        assert db.scalar(select(func.count()).select_from(Attachment)) == 0


def test_upload_lease_blocks_messages_and_deletion(hosted, monkeypatch):
    _, conv, headers = setup(hosted)
    base = f"/api/public/conversations/{conv['id']}"
    def convert(*args, **kwargs):
        assert hosted[3].post(base + "/messages", headers=headers, json={"message":"Hi", "request_id":"busy"}).status_code == 409
        assert hosted[3].delete(base, headers=headers).status_code == 409
        assert upload(hosted, conv, headers).status_code == 409
        return {"text":"safe"}
    monkeypatch.setattr("asklit.hosted.public.convert_upload", convert)
    assert upload(hosted, conv, headers).status_code == 200


def test_changed_attachments_cannot_replay_cached_answer(hosted):
    _, conv, headers = setup(hosted)
    file = upload(hosted, conv, headers).json()
    base = f"/api/public/conversations/{conv['id']}"
    body = {"message":"Hi", "request_id":"same"}
    assert hosted[3].post(base + "/messages", headers=headers, json=body).status_code == 200
    hosted[3].delete(base + "/attachments/" + file["id"], headers=headers)
    assert hosted[3].post(base + "/messages", headers=headers, json=body).status_code == 409


@pytest.mark.parametrize("filename,data", [
    ("file.exe", b"x"), ("file.pdf", b"not a pdf"), ("file.docx", b"not zip"),
    ("file.txt", b"\x00binary"), ("file.txt", b"\xff"), ("file.txt", b""),
])
def test_rejects_invalid_documents(filename, data):
    with pytest.raises(HTTPException):
        convert_upload(data, filename)


def docx(extra=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Office document deadline Friday.</w:t></w:r></w:p></w:body></w:document>')
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return out.getvalue()


def test_anydoc_and_archive_limits():
    assert "Friday" in convert_upload(docx(), "notice.docx")["text"]
    for extra in [{"word/vbaProject.bin": b"macro"}, {"word/embeddings/file.bin":b"file"}, {"big.txt": b"x" * (26 * 1024 * 1024)}]:
        with pytest.raises(HTTPException):
            convert_upload(docx(extra), "notice.docx")


def test_pdf_pages_and_scans():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 50 700 Td (Deadline Friday) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    assert "Friday" in convert_upload(out.getvalue(), "notice.pdf")["text"]
    with pytest.raises(HTTPException):
        convert_upload(out.getvalue(), "notice.pdf", max_pages=0)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    out = io.BytesIO()
    writer.write(out)
    with pytest.raises(HTTPException) as exc:
        convert_upload(out.getvalue(), "scan.pdf")
    assert "OCR" in exc.value.detail


def test_conversion_concurrency_limit():
    CONVERSIONS.acquire()
    CONVERSIONS.acquire()
    try:
        with pytest.raises(HTTPException) as exc:
            convert_upload(b"Hello", "a.txt")
        assert exc.value.status_code == 429
    finally:
        CONVERSIONS.release()
        CONVERSIONS.release()


def test_preview_uses_same_converter_and_prompt(hosted):
    bot, _, _ = setup(hosted)
    c = hosted[3]
    file = c.post(f"/api/bots/{bot['id']}/preview/attachments", files={"file":("notes.txt",b"Deadline Friday")})
    assert file.status_code == 200
    response = c.post(f"/api/bots/{bot['id']}/preview", json={"message":"When?", "attachments":[file.json()]})
    assert response.status_code == 200
    assert any("Deadline Friday" in m["content"] for m in hosted[5][-1])


def test_failed_upload_quota_and_expired_lease(hosted):
    _, conv, headers = setup(hosted)
    with hosted[1].transaction() as db:
        current = db.get(Conversation, conv["id"])
        current.busy_until, current.active_request = 1, "crashed-upload"
    for _ in range(12):
        assert upload(hosted, conv, headers, b"x", "bad.exe").status_code == 422
    assert upload(hosted, conv, headers).status_code == 429
    with hosted[1].transaction() as db:
        assert db.get(Conversation, conv["id"]).busy_until == 0


def test_remove_refunds_text_budget_without_keeping_raw_bytes(hosted):
    _, conv, headers = setup(hosted)
    result = upload(hosted, conv, headers, b"x" * 29999, "../../private\u202e.txt")
    assert result.status_code == 200
    assert "/" not in result.json()["filename"]
    assert "\u202e" not in result.json()["filename"]
    assert upload(hosted, conv, headers, b"extra").status_code == 422
    base = f"/api/public/conversations/{conv['id']}/attachments"
    hosted[3].delete(base + "/" + result.json()["id"], headers=headers)
    assert upload(hosted, conv, headers, b"extra").status_code == 200
