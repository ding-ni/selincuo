"""真实服务端到端验证；独立批次保存截图，不清理测试产物。"""
import argparse
import io
import json
from pathlib import Path
from uuid import uuid4

from PIL import Image
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def water_pixels(screenshot):
    image = Image.open(io.BytesIO(screenshot)).convert("RGB")
    return sum(1 for r, g, b in image.getdata() if b > r + 35 and g > r + 25)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    output = ROOT / "成果/测试验证" / ("浏览器验证-" + uuid4().hex[:12])
    output.mkdir(parents=True, exist_ok=False)
    report = {"screenshots": [], "checks": [], "page_errors": [], "external_requests": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
            page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
            page.on("request", lambda r: report["external_requests"].append(r.url) if r.url.startswith("http") and not r.url.startswith(args.url) else None)
            page.goto(args.url)
            page.wait_for_load_state("networkidle")
            expect(page.locator(".object-title code")).to_have_text("lake-selinco")
            expect(page.locator(".map-message")).to_have_count(0)
            page.wait_for_timeout(600)
            canvas = page.locator("canvas.maplibregl-canvas")
            before = canvas.screenshot()
            assert water_pixels(before) > 300, "真实湖界未渲染"
            page.get_by_role("checkbox", name="湖泊边界", exact=True).uncheck()
            page.wait_for_timeout(300)
            assert water_pixels(canvas.screenshot()) < 10, "图层隐藏后湖界仍可见"
            page.get_by_role("checkbox", name="湖泊边界", exact=True).check()
            page.wait_for_timeout(300)
            assert water_pixels(canvas.screenshot()) > 300
            report["checks"].append("湖界像素非空，图层隐藏与恢复有效")
            before_zoom = canvas.screenshot()
            page.get_by_role("button", name="Zoom in", exact=True).click()
            page.wait_for_timeout(600)
            assert canvas.screenshot() != before_zoom
            page.get_by_role("button", name="显示全部范围").click()
            page.wait_for_timeout(600)
            page.get_by_role("textbox", name="检索空间对象").fill("115")
            expect(page.locator(".object-row")).to_have_count(1)
            page.locator(".object-row").click()
            expect(page.locator(".object-title code")).to_have_text("station-002")
            expect(page.locator(".detail-panel")).to_contain_text("临近流域")
            page.get_by_role("textbox", name="检索空间对象").fill("没有这个对象")
            expect(page.locator(".object-list")).to_contain_text("没有匹配")
            page.get_by_role("textbox", name="检索空间对象").fill("")
            page.get_by_role("button", name="显示全部范围").click()
            page.wait_for_timeout(600)
            page.get_by_title("查看色林错", exact=True).click()
            expect(page.locator(".object-title code")).to_have_text("lake-selinco")
            report["checks"].append("地图缩放定位、检索空状态、站点原表归属与地图点选")
            for name, width, height in [("桌面", 1440, 1000), ("平板", 768, 1024), ("手机", 390, 844)]:
                page.set_viewport_size({"width": width, "height": height})
                page.get_by_role("button", name="显示全部范围").click()
                page.wait_for_timeout(700)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name + "页面横向溢出"
                assert water_pixels(canvas.screenshot()) > 100, name + "地图为空"
                filename = name + ".png"
                page.screenshot(path=str(output / filename), full_page=True)
                report["screenshots"].append(filename)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator(".related-dataset").click()
            expect(page.locator(".dataset-detail")).to_contain_text("来源文件与 SHA-256")
            expect(page.locator(".dataset-detail")).to_contain_text("湖泊边界.shp")
            page.screenshot(path=str(output / "资料来源.png"), full_page=True)
            page.get_by_role("textbox", name="检索资料").fill("not-found")
            expect(page.locator(".table-scroll")).to_contain_text("没有匹配的资料")
            page.get_by_role("textbox", name="检索资料").fill("")
            for name, text in [("天空地监测", "监测资料尚未接入"), ("模型预报", "尚无可运行的预报方案"), ("漫溢风险", "漫溢风险待研判"), ("预警管理", "暂无业务预警依据")]:
                page.get_by_role("button", name=name).click()
                expect(page.locator(".pending-section h2")).to_have_text(text)
            report["checks"].append("资料来源与哈希、资料检索、未接入模块状态")
            page.get_by_role("button", name="流域一张图", exact=True).click()
            page.route("**/api/v1/objects/station-001", lambda route: route.fulfill(status=503, json={"detail": "测试详情故障"}))
            page.locator(".object-row").filter(has_text="巴勒切").click()
            expect(page.locator(".detail-error")).to_contain_text("测试详情故障")
            page.unroute("**/api/v1/objects/station-001")
            page.route("**/api/v1/layers/D03", lambda route: route.fulfill(status=503, json={"detail": "测试图层故障"}))
            page.get_by_role("button", name="重新加载资料").click()
            expect(page.locator(".map-message")).to_contain_text("测试图层故障")
            expect(page.locator(".object-row")).to_have_count(10)
            page.unroute("**/api/v1/layers/D03")
            page.route("**/api/v1/objects", lambda route: route.fulfill(status=503, json={"detail": "测试目录故障"}))
            page.get_by_role("button", name="重新加载资料").click()
            expect(page.locator(".error-banner")).to_contain_text("测试目录故障")
            expect(page.locator(".object-row")).to_have_count(0)
            page.screenshot(path=str(output / "服务故障.png"), full_page=True)
            page.unroute("**/api/v1/objects")
            page.get_by_role("button", name="重试", exact=True).click()
            expect(page.locator(".object-title code")).to_have_text("lake-selinco")
            expect(page.locator(".error-banner")).to_have_count(0)
            expect(page.locator(".map-message")).to_have_count(0)
            report["checks"].append("详情、图层和目录故障明确显示；恢复可重试且不残留旧数据")
            assert not report["page_errors"], report["page_errors"]
            assert not report["external_requests"], report["external_requests"]
            report["status"] = "passed"
        finally:
            browser.close()
            with (output / "验证结果.json").open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
    print(output)


if __name__ == "__main__":
    main()
