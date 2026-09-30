"""A successful click must reveal the next action, not just fill hidden fields."""
import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("viewport", [{"width": 1724, "height": 864}, {"width": 390, "height": 844}])
@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_demo_entry_reveals_next_action_without_starting_run(page, app_url, viewport, motion):
    page.set_viewport_size(viewport)
    page.emulate_media(reduced_motion=motion)
    writes = []
    page.on("request", lambda r: writes.append(r.url) if r.method == "POST" else None)
    page.goto(app_url)
    entry = page.get_by_role("button", name="体验问题与证据新流程", exact=True)
    entry.wait_for()
    if motion == "reduce":
        entry.focus()
        page.keyboard.press("Enter")
    else:
        entry.click()
    analyze = page.get_by_role("button", name="分析问题", exact=True)
    expect(analyze).to_be_in_viewport(ratio=1)
    expect(analyze).to_be_focused()
    expect(page.get_by_role("status").filter(has_text="已载入教学问题")).to_be_in_viewport()
    expect(page.get_by_role("button", name="开始预测 →", exact=True)).to_be_disabled()
    assert writes == [], "Loading a demo must not start analysis or prediction automatically"
