"""Browser authoring → real worker/model → Supabase → static Next.js test.

Requires the disposable fixture server/worker and built CourtFormsOnline files
served at localhost:3000. Uses synthetic material and a few tiny-model calls.
"""

import json
import os
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    session = json.loads(Path("/tmp/asklit-live-session.json").read_text())
    with sync_playwright() as playwright:
        options = {"headless": True}
        if os.getenv("ASKLIT_BROWSER_EXECUTABLE"):
            options["executable_path"] = os.environ["ASKLIT_BROWSER_EXECUTABLE"]
        browser = playwright.chromium.launch(**options)
        editor_context = browser.new_context(viewport={"width": 1280, "height": 900})
        editor_context.add_cookies(
            [
                {
                    "name": "asklit_session",
                    "value": session["token"],
                    "url": "http://127.0.0.1:8049",
                }
            ]
        )
        page = editor_context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "dialog",
            lambda dialog: (
                dialog.accept("Supabase tiny model E2E")
                if dialog.type == "prompt"
                else dialog.accept()
            ),
        )
        page.goto("http://127.0.0.1:8049")
        page.get_by_role("button", name="New AskLit", exact=True).click()
        page.get_by_role("dialog").get_by_label("Name", exact=True).fill(
            "Supabase tiny model E2E"
        )
        page.get_by_role("button", name="Create AskLit", exact=True).click()
        page.locator("#editor").wait_for(state="visible")
        page.locator("#upload-file").set_input_files(
            {
                "name": "synthetic-guide.txt",
                "mimeType": "text/plain",
                "buffer": b"This is a synthetic test knowledge base, not actual legal information. The synthetic filing color is violet. When asked the synthetic filing color, the answer is violet. ",
            }
        )
        page.get_by_role("button", name="Upload").click()
        expect(page.locator("#documents")).to_contain_text("Ready", timeout=180000)
        page.get_by_role("button", name="Instructions").click()
        page.locator("#prompt").fill(
            "Answer only from the provided synthetic source. When asked the filing color, reply with just the color word, no punctuation."
        )
        page.get_by_role("button", name="Preview", exact=True).click()
        page.locator("#question").fill("What is the synthetic filing color?")
        page.get_by_role("button", name="Send", exact=True).click()
        answer = page.locator("#transcript .assistant")
        expect(answer).to_contain_text("violet", timeout=120000)
        assert "violet" in answer.inner_text().splitlines()[0].lower()
        page.get_by_role("button", name="Evaluate", exact=True).click()
        page.get_by_role("button", name="Add question", exact=True).click()
        page.locator("#scenarios textarea").nth(0).fill(
            "What is the synthetic filing color?"
        )
        page.locator("#scenarios textarea").nth(1).fill("icontains:violet")
        page.get_by_text("Grading & retrieval", exact=True).click()
        page.locator("#rubrics").fill(
            "The answer identifies violet as the synthetic filing color."
        )
        page.get_by_role("button", name="Run evaluation", exact=True).click()
        expect(page.locator("#evaluation-results")).to_contain_text(
            "Pass", timeout=180000
        )
        page.get_by_role("button", name="Publish", exact=True).click()
        expect(page.locator("#notice")).to_contain_text("Published.")
        page.get_by_role("button", name="Settings", exact=True).click()
        bot_id = page.locator("#workspace-download").get_attribute("href").split("/")[3]
        page.get_by_role("button", name="Export Streamlit app", exact=True).click()
        download = page.locator("#export-result").get_by_role(
            "link", name="Download ZIP", exact=True
        )
        download.wait_for(timeout=180000)
        with page.expect_download(timeout=120000) as pending:
            download.click()
        archive = pending.value.path()
        with zipfile.ZipFile(archive) as bundle:
            assert "data/app.sqlite3" in bundle.namelist()
            assert "prompts/default.yml" in bundle.namelist()
        # Change the draft; a new public conversation must still use the publication.
        page.get_by_role("button", name="Instructions").click()
        page.locator("#prompt").fill("Reply orange to every question.")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.locator("#save-status")).to_contain_text("Saved")
        public_context = browser.new_context()
        public = public_context.new_page()
        public.on("pageerror", lambda error: errors.append(str(error)))
        requests = []
        public.on(
            "request",
            lambda request: (
                requests.append(request) if "/api/public/" in request.url else None
            ),
        )
        public.goto(f"http://localhost:3000/asklit?bot={bot_id}")
        public.get_by_role("heading", name="Supabase tiny model E2E").wait_for(
            timeout=30000
        )
        public.get_by_label("Your question").fill("What is the synthetic filing color?")
        public.get_by_role("button", name="Send", exact=True).click()
        expect(public.get_by_role("log")).to_contain_text("violet", timeout=120000)
        assert "orange" not in public.get_by_role("log").inner_text().lower()
        public.get_by_text("synthetic-guide.txt, page 1", exact=True).wait_for()
        public.screenshot(path="/tmp/asklit-supabase-live-chat.png", full_page=True)
        public.get_by_role("button", name="Clear conversation", exact=True).click()
        expect(public.get_by_role("log")).to_be_empty()
        assert all("cookie" not in request.headers for request in requests)
        assert not errors, errors
        result = {
            "bot_id": bot_id,
            "model": "gpt-4.1-nano",
            "storage": "Supabase private bucket",
            "preview": "passed",
            "evaluation_with_rubric": "passed",
            "streamlit_export_download": "passed",
            "static_public_chat": "passed",
            "draft_isolation": "passed",
            "browser_errors": errors,
        }
        Path("/tmp/asklit-live-e2e-result.json").write_text(
            json.dumps(result, indent=2)
        )
        print(json.dumps(result))
        browser.close()


if __name__ == "__main__":
    main()
