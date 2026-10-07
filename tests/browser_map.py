"""Optional browser regression: PYTHONPATH=. .venv/bin/python tests/browser_map.py.

Requires Playwright and its Chromium browser; Leaflet is loaded from its CDN.
"""
import threading
import time

from playwright.sync_api import sync_playwright

from talli_flug.state import Aircraft, AircraftStore
from talli_flug.web import make_server

store = AircraftStore(ttl=600)
store.aircraft['ABC123'] = Aircraft('ABC123', updated=time.monotonic())
base_row = store.snapshot()[0]


class Snapshot:
    rows = []

    def snapshot(self):
        return [dict(row) for row in self.rows]


snapshot = Snapshot()
row = dict(base_row, callsign='<img src=x onerror=alert(1)>', latitude=51.0,
           longitude=10.0, track=90, altitude=12000, speed=200, vertical_rate=-500,
           position_age_seconds=42)
snapshot.rows = [row, dict(base_row, icao='BAD123', latitude=91, longitude=0),
                 dict(base_row, icao='BAD456', latitude=None, longitude=0)]
server = make_server(('127.0.0.1', 0), snapshot, 'test', lambda: True, map_center=(40, -4))
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1100, 'height': 800})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        # Avoid tile traffic during tests; still exercise the real Leaflet library.
        page.route('https://tile.openstreetmap.org/**', lambda route: route.fulfill(status=204))
        page.goto(f'http://127.0.0.1:{server.server_port}/')
        marker = page.locator('.aircraft-icon')
        marker.wait_for()
        assert marker.count() == 1
        assert marker.locator('path').get_attribute('transform') == 'rotate(90 12 12)'
        marker.click()
        popup = page.locator('.leaflet-popup-content')
        assert popup.locator(':scope > div > div').count() == 7
        assert '<img src=x onerror=alert(1)>' in popup.inner_text()
        assert popup.locator('img').count() == 0
        assert 'Position age: 4' in popup.inner_text()
        page.screenshot(path='/tmp/talli-air-map.png')
        page.evaluate("window.originalMap = document.querySelector('.leaflet-map-pane'); window.originalMarker = document.querySelector('.aircraft-icon')")
        before = marker.get_attribute('style')
        snapshot.rows = [dict(row, latitude=51.1, longitude=10.1, track=180, altitude=13000)]
        page.wait_for_function("document.querySelector('.aircraft-icon path').getAttribute('transform') === 'rotate(180 12 12)'")
        assert page.evaluate("originalMap === document.querySelector('.leaflet-map-pane') && originalMarker === document.querySelector('.aircraft-icon')")
        assert marker.get_attribute('style') != before
        assert '13000 ft' in popup.inner_text()
        assert '13000' in page.locator('tbody').inner_text()
        # Failure retains observations and reports that they are stale.
        page.route(f'http://127.0.0.1:{server.server_port}/', lambda route: route.fulfill(status=503))
        page.wait_for_function("document.querySelector('#refresh-status').textContent.includes('Refresh failed')")
        assert marker.count() == 1
        page.unroute(f'http://127.0.0.1:{server.server_port}/')
        snapshot.rows = []
        marker.wait_for(state='detached')
        assert 'No current aircraft.' in page.locator('tbody').inner_text()
        # Capture the initial view on an empty page without exposing app internals.
        page.add_init_script("""
            Object.defineProperty(window, 'L', {configurable: true, set(lib) {
                Object.defineProperty(window, 'L', {value: lib, writable: true});
                let createMap = lib.map;
                Object.defineProperty(lib, 'map', {configurable: true,
                    set(factory) { createMap = factory; },
                    get() { return (...args) => {
                        window.testMap = createMap(...args); return window.testMap;
                    }; }
                });
            }});
        """)
        page.reload()
        page.wait_for_function('window.testMap && testMap.getCenter().lat === 40')
        assert page.evaluate('testMap.getCenter().lng') == -4
        snapshot.rows = [dict(row, callsign=None, track=None, latitude=0, longitude=0)]
        marker.wait_for()
        assert marker.locator('circle').count() == 1
        assert abs(page.evaluate('testMap.getCenter().lat')) < 0.01
        page.evaluate('testMap.setView([20, 20], 7, {animate: false})')
        snapshot.rows = [dict(row, latitude=1, longitude=1, track=270)]
        page.wait_for_function("document.querySelector('.aircraft-icon path')?.getAttribute('transform') === 'rotate(270 12 12)'")
        assert page.evaluate('testMap.getCenter().lat') == 20
        assert not errors, errors
        page.route('https://unpkg.com/**', lambda route: route.abort())
        page.reload()
        assert 'Map requires' in page.locator('#map').inner_text()
        snapshot.rows = []
        page.wait_for_function("document.querySelector('tbody').textContent.includes('No current aircraft.')")
        assert not errors, errors
        browser.close()
        print('Map browser checks passed')
finally:
    server.shutdown()
    server.server_close()
    worker.join(2)
