import json

import pytest

tailor = pytest.importorskip("tailor")

PROSE = ("Be part of the Autonomous Vehicle research team and help advance safe autonomy through "
         "generative world models and synthetic data. You will train models, run experiments, "
         "write code in Python and PyTorch, and work with researchers and engineers. ") * 3
THEME = json.dumps({"themeOptions": {"name": "PCS Default", "customTheme": {
    "varTheme": {f"primary-color-{i}": "#000000" for i in range(10, 101, 10)}}}}) * 5


class Resp:
    def __init__(self, text="", data=None, ok=True):
        self.text, self._data, self.ok = text, data, ok

    def json(self):
        return self._data


def test_looks_like_prose_rejects_theme_json():
    assert tailor.looks_like_prose(PROSE)
    assert not tailor.looks_like_prose(THEME)
    assert not tailor.looks_like_prose("short")


def test_eightfold_pid_uses_job_api(monkeypatch):
    calls = []

    def get(url, params=None, **kw):
        calls.append((url, params))
        return Resp(data={"name": "Research Intern, World Models", "location": "US, CA, Santa Clara",
                          "job_description": f"<p>{PROSE}</p>"})
    monkeypatch.setattr(tailor.requests, "get", get)
    url = "https://jobs.nvidia.com/careers?start=0&pid=893398025428&sort_by=match"
    text = tailor.fetch_job_description(url)
    assert calls[0] == ("https://jobs.nvidia.com/api/apply/v2/jobs/893398025428", {"domain": "nvidia.com"})
    assert text.startswith("Research Intern, World Models\nUS, CA, Santa Clara") and "synthetic data" in text


def test_page_that_is_only_config_json_is_not_a_jd(monkeypatch):
    monkeypatch.setattr(tailor.requests, "get", lambda url, **kw: Resp(text=f"<html><body>{THEME}</body></html>"))
    monkeypatch.setattr(tailor, "fetch_job_description_browser", lambda url: None)
    assert tailor.fetch_job_description("https://example.com/careers/job/1") is None


def test_plain_html_posting_still_read(monkeypatch):
    html = f"<html><body><script>var x = {{}};</script><div class='description'>{PROSE}</div></body></html>"
    monkeypatch.setattr(tailor.requests, "get", lambda url, **kw: Resp(text=html))
    assert "synthetic data" in tailor.fetch_job_description("https://example.com/jobs/1")
