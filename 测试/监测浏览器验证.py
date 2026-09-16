"""真实隔离服务验证合成时序；保留证据，仅终止本脚本启动的进程。"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from 测试.监测接入验证 import prepare_batch


def main():
    batch = prepare_batch()
    report = {"checks": [], "page_errors": [], "screenshots": []}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = "http://127.0.0.1:" + str(port)
    environment = {**os.environ, "SELINCO_MONITOR_DIR": str(batch / "接入资料")}
    with (batch / "服务日志.log").open("xb") as log:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "后端.应用:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=environment, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for _ in range(60):
                try:
                    if httpx.get(url + "/api/v1/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if process.poll() is not None:
                    raise RuntimeError("隔离服务启动失败")
                time.sleep(.25)
            else:
                raise RuntimeError("隔离服务启动超时")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
                    page.on("response", lambda r: print("请求失败：", r.url, r.text()) if r.status >= 400 and "/api/v1/observations" in r.url else None)
                    page.goto(url)
                    expect(page.locator(".object-title code")).to_have_text("lake-selinco")
                    page.locator(".monitoring-link").click()
                    expect(page.get_by_label("监测空间对象")).to_have_value("lake-selinco")
                    expect(page.locator(".monitor-records tbody tr")).to_have_count(50)
                    expect(page.locator(".monitor-metadata")).to_contain_text("合成演示")
                    expect(page.locator(".monitor-records tbody tr").first).to_contain_text("2025/01/01 08:00:00")
                    expect(page.locator(".monitor-records tbody tr").first).to_contain_text("缺测")
                    page.get_by_role("button", name="下一页", exact=True).click()
                    expect(page.locator(".monitor-records tbody tr")).to_have_count(12)
                    expect(page.locator(".pagination")).to_contain_text("51–62")
                    page.get_by_role("button", name="上一页", exact=True).click()
                    expect(page.locator(".monitor-records tbody tr")).to_have_count(50)
                    page.get_by_label("质量状态", exact=True).select_option("missing")
                    page.get_by_role("button", name="查询记录", exact=True).click()
                    expect(page.locator(".monitor-records tbody tr")).to_have_count(13)
                    page.get_by_label("质量状态", exact=True).select_option("all")
                    expect(page.get_by_label("质量状态", exact=True)).to_have_value("all")
                    page.get_by_label("监测开始时间").fill("2025-01-01T08:00")
                    page.get_by_label("监测结束时间").fill("2025-01-01T08:00")
                    page.get_by_role("button", name="查询记录", exact=True).click()
                    expect(page.locator(".monitor-records tbody tr")).to_have_count(1)
                    page.get_by_label("监测开始时间").fill("2025-01-02T08:00")
                    page.get_by_role("button", name="查询记录", exact=True).click()
                    expect(page.locator(".monitor-workspace .error-banner")).to_contain_text("开始时间不能晚于结束时间")
                    expect(page.locator(".monitor-records")).to_have_count(0)
                    page.get_by_label("监测开始时间").fill("")
                    page.get_by_label("监测结束时间").fill("")
                    page.get_by_role("button", name="重试记录查询").click()
                    expect(page.locator(".monitor-records tbody tr")).to_have_count(50)
                    page.locator(".monitor-provenance summary").click()
                    expect(page.locator(".monitor-provenance")).to_contain_text("合成演示，仅用于隔离测试")
                    expect(page.locator(".monitor-provenance")).to_contain_text("7200 秒")
                    report["checks"].append("对象联动、分页、缺测、质量筛选、北京时间闭区间、反向时间拒绝、来源哈希与缺口")
                    for name, width, height in [("监测桌面", 1440, 1000), ("监测平板", 768, 1024), ("监测手机", 390, 844)]:
                        page.set_viewport_size({"width": width, "height": height})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name + "横向溢出"
                        filename = name + ".png"
                        page.screenshot(path=str(batch / filename), full_page=True)
                        report["screenshots"].append(filename)
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.get_by_label("观测平台").select_option("uav")
                    page.get_by_role("button", name="查询资料", exact=True).click()
                    expect(page.locator(".monitor-empty h3")).to_have_text("当前筛选下没有监测产品")
                    expect(page.locator(".monitor-readiness")).to_have_count(0)
                    page.get_by_role("button", name="查看色林错空间位置").click()
                    expect(page.locator(".object-title code")).to_have_text("lake-selinco")
                    page.locator(".monitoring-link").click()
                    expect(page.locator(".monitor-records tbody tr")).to_have_count(50)
                    page.route("**/api/v1/observations?**", lambda route: route.fulfill(status=503, json={"detail": "测试记录故障"}))
                    page.get_by_role("button", name="查询记录", exact=True).click()
                    expect(page.locator(".monitor-workspace .error-banner")).to_contain_text("测试记录故障")
                    page.unroute("**/api/v1/observations?**")
                    page.get_by_role("button", name="重试记录查询").click()
                    expect(page.locator(".monitor-records tbody tr")).to_have_count(50)
                    page.route("**/api/v1/monitoring/products?**", lambda route: route.fulfill(status=503, json={"detail": "测试监测目录故障"}))
                    page.get_by_role("button", name="查询资料", exact=True).click()
                    expect(page.locator(".monitor-workspace .error-banner")).to_contain_text("测试监测目录故障")
                    expect(page.locator(".monitor-records")).to_have_count(0)
                    page.unroute("**/api/v1/monitoring/products?**")
                    page.get_by_role("button", name="重试监测查询").click()
                    expect(page.locator(".monitor-records tbody tr")).to_have_count(50)
                    report["checks"].append("筛选空状态、地图往返、记录及目录故障恢复、三个视口无页面溢出")
                    assert not report["page_errors"], report["page_errors"]
                    report["status"] = "passed"
                finally:
                    browser.close()
        finally:
            process.terminate()
            process.wait(timeout=15)
            with (batch / "浏览器结果.json").open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
    print(batch)


if __name__ == "__main__":
    main()
