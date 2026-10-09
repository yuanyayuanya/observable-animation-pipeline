from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = PROJECT_ROOT / "app" / "static"
HTML_PATH = STATIC_ROOT / "index.html"
CSS_PATH = STATIC_ROOT / "styles.css"
JS_PATH = STATIC_ROOT / "app.js"


class StaticDocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.attributes_by_id: dict[str, dict[str, str | None]] = {}
        self.resource_references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)
            self.attributes_by_id[element_id] = attributes

        attribute_name = "href" if tag in {"link", "a"} else "src"
        reference = attributes.get(attribute_name)
        if reference:
            self.resource_references.append(reference)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_required_static_files_exist() -> None:
    assert HTML_PATH.is_file()
    assert CSS_PATH.is_file()
    assert JS_PATH.is_file()


def test_console_exposes_required_dom_regions() -> None:
    parser = StaticDocumentParser()
    parser.feed(read_text(HTML_PATH))

    required_ids = {
        "devModeBanner",
        "newTaskForm",
        "taskFile",
        "frameCount",
        "analysisProfile",
        "faultPlan",
        "taskList",
        "taskDetail",
        "taskStatus",
        "taskProgressBar",
        "stepTimeline",
        "retryStepButton",
        "attemptTableBody",
        "stepErrorPanel",
        "logOutput",
        "artifactTableBody",
        "rerunTaskButton",
        "rerunNoCacheButton",
    }

    assert required_ids <= parser.ids
    assert "resumeTaskButton" not in parser.ids


def test_html_references_only_local_static_resources() -> None:
    parser = StaticDocumentParser()
    parser.feed(read_text(HTML_PATH))

    external = []
    for reference in parser.resource_references:
        if reference.startswith(("#", "/api/")):
            continue
        parsed = urlparse(reference)
        if parsed.scheme or parsed.netloc or reference.startswith("//"):
            external.append(reference)

    assert external == []
    assert "/static/styles.css" in parser.resource_references
    assert "/static/app.js" in parser.resource_references


def test_assets_do_not_import_external_resources() -> None:
    combined = "\n".join(read_text(path) for path in (HTML_PATH, CSS_PATH, JS_PATH))
    forbidden_patterns = (
        r"https?://",
        r"@import\s+url",
        r"cdn\.",
        r"unpkg\.",
        r"jsdelivr\.",
        r"fonts\.googleapis\.",
    )
    for pattern in forbidden_patterns:
        assert re.search(pattern, combined, flags=re.IGNORECASE) is None


def test_api_client_covers_documented_console_actions() -> None:
    javascript = read_text(JS_PATH)
    required_endpoint_fragments = {
        'const API_BASE = "/api"',
        'this.request("/tasks?limit=50")',
        'this.request("/tasks", { method: "POST"',
        '/events?after_sequence=',
        '/steps/${encodeURIComponent(stepKey)}/retry',
        '/rerun${query}`, { method: "POST"',
        '?ignore_cache=${ignoreCache ? "true" : "false"}',
        '/artifacts/${encodeURIComponent(artifactId)}',
    }
    for fragment in required_endpoint_fragments:
        assert fragment in javascript

    assert '/resume`' not in javascript


def test_fault_plan_options_match_supported_backend_values() -> None:
    html = read_text(HTML_PATH)
    supported = {
        "none",
        "analyze_fail_once_at_50",
        "analyze_timeout_once",
        "contact_sheet_permanent_fail",
        "worker_exit_at_40",
    }
    options = set(re.findall(r'<option value="([^"]+)"', html))
    assert supported <= options
    assert "contact_sheet_timeout" not in options
    assert "worker_exit_during_analysis" not in options


def test_schema_field_adapters_cover_events_artifacts_and_filenames() -> None:
    javascript = read_text(JS_PATH)
    assert "task.filename || task.input_filename" in javascript
    assert "stepKeysById.get(event.step_id)" in javascript
    assert "stepKeysById.get(artifact.step_id)" in javascript
    assert "artifact.download_url || state.api.artifactUrl" in javascript
    assert 'typeof error === "string"' in javascript


def test_polling_backoff_and_url_restoration_are_present() -> None:
    javascript = read_text(JS_PATH)
    assert "const POLL_INTERVAL_MS = 1000" in javascript
    assert "const BACKOFF_MS = [1000, 2000, 4000, 8000]" in javascript
    assert '.get("task_id")' in javascript
    assert "new URLSearchParams(window.location.search)" in javascript
    assert "TERMINAL_TASK_STATES.has(task.status)" in javascript
    assert "state.pollFailures += 1" in javascript


def test_mock_mode_is_visible_and_contains_all_pipeline_steps() -> None:
    html = read_text(HTML_PATH)
    javascript = read_text(JS_PATH)
    assert "开发模式" in html
    assert 'state.mode = "mock"' in javascript

    for step_key in (
        "validate_input",
        "extract_frames",
        "analyze_frames",
        "build_contact_sheet",
        "package_result",
    ):
        assert step_key in javascript


def test_accessible_status_and_error_regions_are_declared() -> None:
    parser = StaticDocumentParser()
    parser.feed(read_text(HTML_PATH))

    assert parser.attributes_by_id["globalMessage"]["aria-live"] == "polite"
    assert parser.attributes_by_id["formMessage"]["role"] == "status"
    assert parser.attributes_by_id["stepErrorPanel"]["role"] == "alert"
    assert parser.attributes_by_id["stepErrorPanel"]["aria-live"] == "polite"


def test_css_contains_mobile_and_desktop_layout_contracts() -> None:
    css = read_text(CSS_PATH)
    assert "grid-template-columns: minmax(280px, 340px) minmax(0, 1fr)" in css
    assert "@media (max-width: 520px)" in css
    assert "@media (max-width: 700px)" in css
    assert "overflow-wrap: anywhere" in css
    assert ".task-actions" in css
    assert ".step-timeline" in css
