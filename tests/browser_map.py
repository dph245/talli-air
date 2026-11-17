"""Optional browser regression: PYTHONPATH=. .venv/bin/python tests/browser_map.py.

Requires Playwright and Chromium; Leaflet is loaded from its CDN.
Set CHROMIUM_EXECUTABLE to use an installed system browser.
"""
import os
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from talli_flug.message_rate import MessageRate
from talli_flug.metadata import AircraftMetadata, LocalAircraftMetadata
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
                 dict(base_row, icao='BAD456', latitude=None, longitude=0, altitude=10000, last_seen=10)]
rates = MessageRate(time_fn=lambda: 42)
rates.increment('roof')
statuses = {'roof': True, '<img src=x>': False}
metadata = LocalAircraftMetadata({'ABC123': AircraftMetadata('D-LIVE', 'A320'),
                                  'BAD456': AircraftMetadata('D-LOW', 'B738')})
server = make_server(('127.0.0.1', 0), snapshot, 'test', lambda: True, metadata, map_center=(40, -4),
                     receiver_status=lambda: statuses, message_rate=rates)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'],
                                    executable_path=os.environ.get('CHROMIUM_EXECUTABLE'))
        page = browser.new_page(viewport={'width': 1100, 'height': 800})
        errors = []
        request_paths = []
        base = f'http://127.0.0.1:{server.server_port}'
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: request_paths.append(urlsplit(request.url).path)
                if request.url.startswith(base + '/') else None)
        # Avoid tile traffic during tests; still exercise the real Leaflet library.
        page.route('https://tile.openstreetmap.org/**', lambda route: route.fulfill(status=204))
        page.goto(base)
        expect(page.locator('#kpi-active')).to_have_text('3')
        expect(page.locator('#kpi-positioned')).to_have_text('1')
        expect(page.locator('#kpi-msg')).to_have_text('0.1')
        expect(page.locator('#kpi-recv')).to_have_text('1 / 2')
        expect(page.locator('.receiver.offline')).to_contain_text('<img src=x>')
        expect(page.locator('.receiver.offline')).to_contain_text('0.0 frames/s')
        expect(page.locator('.receiver img')).to_have_count(0)
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
        page.screenshot(path='/tmp/talli-air-map.png', full_page=True)
        search = page.locator('#search-box')
        links = page.locator('tbody a')
        page.locator('[data-sort="icao"]').click()
        expect(links).to_have_text(['BAD456', 'BAD123', 'ABC123'])
        page.locator('[data-sort="last_seen"]').click()
        expect(links.first).to_have_text('BAD456')
        page.locator('[data-sort="altitude"]').click()
        expect(links).to_have_text(['ABC123', 'BAD456', 'BAD123'])
        page.locator('[data-sort="altitude"]').click()
        expect(links).to_have_text(['BAD456', 'ABC123', 'BAD123'])
        search.fill('aBc123')
        expect(links).to_have_text(['ABC123'])
        search.fill('onerror')
        expect(links).to_have_text(['ABC123'])
        search.fill('d-l')
        expect(links).to_have_text(['BAD456', 'ABC123'])
        page.evaluate("window.originalMap = document.querySelector('.leaflet-map-pane'); window.originalMarker = document.querySelector('.aircraft-icon'); window.originalBody = document.querySelector('tbody')")
        before = marker.get_attribute('style')
        snapshot.rows = [dict(row, latitude=51.1, longitude=10.1, track=180, altitude=13000),
                         dict(snapshot.rows[-1], altitude=14000)]
        rates.increment('roof')
        rates.increment('<img src=x>')
        statuses['<img src=x>'] = True
        page.wait_for_function("document.querySelector('.aircraft-icon path').getAttribute('transform') === 'rotate(180 12 12)'")
        assert page.evaluate("originalMap === document.querySelector('.leaflet-map-pane') && originalMarker === document.querySelector('.aircraft-icon') && originalBody === document.querySelector('tbody')")
        assert marker.get_attribute('style') != before
        assert '13000 ft' in popup.inner_text()
        assert '13000' in page.locator('tbody').inner_text()
        expect(search).to_have_value('d-l')
        expect(links).to_have_text(['ABC123', 'BAD456'])
        expect(links.first).to_have_attribute('href', '/aircraft/ABC123')
        expect(page.locator('#kpi-msg')).to_have_text('0.3')
        expect(page.locator('#kpi-recv')).to_have_text('2 / 2')
        expect(page.locator('.receiver.online').last).to_contain_text('0.1 frames/s')
        assert request_paths.count('/') == 1
        assert request_paths.count('/api/aircraft') == request_paths.count('/api/stats') == 1

        status = page.locator('#refresh-status')
        page.route(base + '/api/stats', lambda route: route.fulfill(status=503))
        expect(status).to_contain_text('stale', timeout=8000)
        assert marker.count() == 1
        page.unroute(base + '/api/stats')
        expect(status).to_be_empty(timeout=8000)
        page.route(base + '/api/aircraft', lambda route: route.fulfill(json=[{'icao': 'invalid'}]))
        expect(status).to_contain_text('stale', timeout=8000)
        expect(links).to_have_count(2)
        page.unroute(base + '/api/aircraft')
        expect(status).to_be_empty(timeout=8000)

        pending = []
        page.route(base + '/api/stats', lambda route: pending.append(route))
        expect(status).to_contain_text('stale', timeout=12000)
        assert len(pending) == 1
        expect(page.locator('#kpi-msg')).to_have_text('0.3')
        pending[0].abort()
        page.unroute(base + '/api/stats')
        snapshot.rows = []
        marker.wait_for(state='detached')
        expect(page.locator('tbody')).to_contain_text('No current aircraft.')
        expect(status).to_be_empty()
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
        assert page.evaluate('testMap.getZoom()') == 7
        page.set_viewport_size({'width': 390, 'height': 844})
        page.evaluate('testMap.invalidateSize().setView([1, 1], 7, {animate: false})')
        assert page.evaluate('document.documentElement.scrollWidth === innerWidth')
        assert page.locator('.table').evaluate('(table) => table.scrollWidth > table.clientWidth')
        assert page.locator('#map').evaluate('(map) => map.clientHeight >= 250')
        page.screenshot(path='/tmp/talli-air-mobile.png', full_page=True)
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
