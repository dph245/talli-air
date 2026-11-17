(() => {
    const initial = JSON.parse(document.getElementById('map-data').textContent);
    const body = document.getElementById('aircraft-rows');
    const search = document.getElementById('search-box');
    const sortButtons = document.querySelectorAll('[data-sort]');
    const status = document.getElementById('refresh-status');
    const refreshInterval = 5000;
    const markers = new Map();
    let aircraft = initial.aircraft;
    let sortKey = 'icao';
    let ascending = true;
    let map;
    let fitted = false;
    let sampledAt = performance.now();

    function popup(row) {
        const content = document.createElement('div');
        const age = Number.isFinite(row.position_age_seconds)
            ? Math.floor(row.position_age_seconds + (performance.now() - sampledAt) / 1000) + ' s'
            : '—';
        const fields = [];
        if (row.callsign) fields.push(['Callsign', row.callsign]);
        fields.push(['ICAO', row.icao], ['Barometric altitude', value(row.altitude, ' ft')],
            ['Ground speed', value(row.speed, ' kt')], ['Track', value(row.track, '°')],
            ['Vertical rate', value(row.vertical_rate, ' ft/min')], ['Position age', age]);
        for (const [label, text] of fields) {
            const line = document.createElement('div');
            line.textContent = `${label}: ${text}`;
            content.append(line);
        }
        return content;
    }

    function value(number, unit) {
        return Number.isFinite(number) ? `${Math.round(number * 10) / 10}${unit}` : '—';
    }

    function icon(track) {
        const shape = Number.isFinite(track)
            ? `<path transform="rotate(${track % 360} 12 12)" d="M12 2 L14 10 L22 15 L22 17 L14 15 L14 20 L17 22 L7 22 L10 20 L10 15 L2 17 L2 15 L10 10 Z"/>`
            : '<circle cx="12" cy="12" r="5"/>';
        return L.divIcon({className: 'aircraft-icon', iconSize: [24, 24], iconAnchor: [12, 12],
            html: `<svg viewBox="0 0 24 24" aria-hidden="true" fill="#222" stroke="white" stroke-width="1">${shape}</svg>`});
    }

    function updateMap(rows) {
        if (!map) return;
        const current = new Set();
        const positions = [];
        for (const row of rows) {
            if (!Number.isFinite(row.latitude) || !Number.isFinite(row.longitude) ||
                Math.abs(row.latitude) > 90 || Math.abs(row.longitude) > 180) continue;
            current.add(row.icao);
            const position = [row.latitude, row.longitude];
            positions.push(position);
            let entry = markers.get(row.icao);
            if (!entry) {
                const marker = L.marker(position, {icon: icon(row.track), title: row.icao,
                    alt: `Aircraft ${row.icao}`, riseOnHover: false}).addTo(map);
                entry = {marker, row};
                marker.bindPopup(() => popup(entry.row), {autoPan: false});
                markers.set(row.icao, entry);
            } else {
                if (entry.row.latitude !== row.latitude || entry.row.longitude !== row.longitude) {
                    entry.marker.setLatLng(position);
                }
                if (entry.row.track !== row.track) entry.marker.setIcon(icon(row.track));
                entry.row = row;
                if (entry.marker.isPopupOpen()) entry.marker.setPopupContent(() => popup(entry.row));
            }
        }
        for (const [icao, entry] of markers) {
            if (!current.has(icao)) {
                entry.marker.remove();
                markers.delete(icao);
            }
        }
        if (!fitted && positions.length) {
            map.fitBounds(positions, {padding: [24, 24], maxZoom: 10, animate: false});
            fitted = true;
        }
    }

    if (window.L) {
        document.getElementById('map').textContent = '';
        map = L.map('map', {zoomAnimation: false, fadeAnimation: false,
            markerZoomAnimation: false, scrollWheelZoom: false, inertia: false});
        map.setView(initial.center, 6);
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        }).addTo(map);
        updateMap(initial.aircraft);
        if (!fitted) map.once('movestart', () => { fitted = true; });
    }

    // Age continues increasing even if the receiver or refresh request stops.
    setInterval(() => {
        for (const entry of markers.values()) {
            if (entry.marker.isPopupOpen()) entry.marker.setPopupContent(() => popup(entry.row));
        }
    }, 1000);

    function element(tag, text) {
        const node = document.createElement(tag);
        node.textContent = text;
        return node;
    }

    function compareRows(left, right) {
        const byIcao = left.icao.localeCompare(right.icao);
        const direction = ascending ? 1 : -1;
        if (sortKey === 'icao') return direction * byIcao;
        const leftValue = left[sortKey];
        const rightValue = right[sortKey];
        if (leftValue == null) return rightValue == null ? byIcao : 1;
        if (rightValue == null) return -1;
        return direction * (leftValue - rightValue) || byIcao;
    }

    function updateTable() {
        const query = search.value.trim().toLowerCase();
        const rows = aircraft.filter(row => [row.icao, row.callsign, row.external_metadata?.registration]
            .some(text => text?.toLowerCase().includes(query))).sort(compareRows);
        const fragment = document.createDocumentFragment();
        const focused = body.contains(document.activeElement) ? document.activeElement.textContent : null;
        let focusedLink;
        for (const row of rows) {
            const tr = document.createElement('tr');
            const icao = tr.insertCell();
            const link = element('a', row.icao);
            link.href = `/aircraft/${encodeURIComponent(row.icao)}`;
            icao.append(link);
            if (row.icao === focused) focusedLink = link;
            const values = [row.external_metadata?.registration, row.external_metadata?.type_designator,
                row.df, row.callsign, row.on_ground == null ? null : row.on_ground ? 'Ground' : 'Airborne',
                row.latitude, row.longitude, row.altitude, row.selected_altitude, row.speed, row.track,
                row.vertical_rate, new Date(row.last_seen * 1000).toISOString().replace(/\.\d{3}Z$/, '+00:00'),
                row.receiver_id, row.latest_raw_frame,
                row.receiver_metadata?.length ? JSON.stringify(row.receiver_metadata) : null];
            for (const item of values) {
                tr.insertCell().textContent = item == null || item === '' ? '—'
                    : typeof item === 'number' ? Number(item.toFixed(5)).toString() : item;
            }
            fragment.append(tr);
        }
        if (!rows.length) {
            const tr = document.createElement('tr');
            const cell = tr.insertCell();
            cell.colSpan = body.closest('table').tHead.rows[0].cells.length;
            cell.textContent = aircraft.length ? 'No matching aircraft.' : 'No current aircraft.';
            fragment.append(tr);
        }
        body.replaceChildren(fragment);
        focusedLink?.focus({preventScroll: true});
    }

    for (const button of sortButtons) {
        button.addEventListener('click', () => {
            ascending = sortKey === button.dataset.sort ? !ascending : button.dataset.sort === 'icao';
            sortKey = button.dataset.sort;
            for (const current of sortButtons) {
                const heading = current.closest('th');
                const selected = current === button;
                if (selected) heading.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
                else heading.removeAttribute('aria-sort');
                current.querySelector('span').textContent = selected ? (ascending ? ' ▲' : ' ▼') : '';
            }
            updateTable();
        });
    }
    search.disabled = false;
    search.addEventListener('input', updateTable);
    if (search.value) updateTable();

    function updateStats(stats) {
        document.getElementById('kpi-active').textContent = stats.aircraft_count;
        document.getElementById('kpi-positioned').textContent = stats.positioned_aircraft_count;
        document.getElementById('kpi-msg').textContent = stats.messages_per_second.toFixed(1);
        document.getElementById('rate-window').textContent = `(${stats.message_rate_window_seconds} s)`;
        document.getElementById('kpi-recv').textContent = `${stats.receivers_online} / ${stats.receiver_count}`;
        const cards = stats.receivers.map(receiver => {
            const card = document.createElement('div');
            card.className = receiver.connected ? 'receiver online' : 'receiver offline';
            card.append(element('strong', receiver.receiver_id),
                element('span', receiver.connected ? 'Online' : 'Offline (disconnected)'),
                element('span', `${receiver.messages_per_second.toFixed(1)} frames/s`));
            return card;
        });
        document.getElementById('receivers').replaceChildren(
            ...(cards.length ? cards : [element('p', 'No receivers configured.')]));
    }

    const nonnegative = number => Number.isFinite(number) && number >= 0;
    const count = number => Number.isSafeInteger(number) && number >= 0;
    const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
    const text = value => value == null || typeof value === 'string';

    function validStats(stats) {
        return object(stats) && count(stats.aircraft_count) && count(stats.positioned_aircraft_count) &&
            stats.positioned_aircraft_count <= stats.aircraft_count && count(stats.receiver_count) &&
            count(stats.receivers_online) && stats.receivers_online <= stats.receiver_count &&
            nonnegative(stats.messages_per_second) && stats.message_rate_window_seconds > 0 &&
            Number.isFinite(stats.message_rate_window_seconds) && Array.isArray(stats.receivers) &&
            stats.receivers.length === stats.receiver_count && stats.receivers.every(receiver =>
                object(receiver) && typeof receiver.receiver_id === 'string' &&
                typeof receiver.connected === 'boolean' && nonnegative(receiver.messages_per_second));
    }

    function validAircraft(rows) {
        return Array.isArray(rows) && rows.every(row => object(row) && typeof row.icao === 'string' &&
            /^[0-9a-f]{6}$/i.test(row.icao) && Number.isFinite(row.last_seen) &&
            Number.isFinite(new Date(row.last_seen * 1000).getTime()) &&
            [row.callsign, row.receiver_id, row.latest_raw_frame].every(text) &&
            (row.on_ground == null || typeof row.on_ground === 'boolean') &&
            ['latitude', 'longitude', 'df', 'altitude', 'selected_altitude', 'speed', 'track', 'vertical_rate',
                'position_age_seconds'].every(key => row[key] === null || Number.isFinite(row[key])) &&
            Array.isArray(row.receiver_metadata) && row.receiver_metadata.every(item => typeof item === 'string') &&
            object(row.external_metadata) && text(row.external_metadata.registration) &&
            text(row.external_metadata.type_designator));
    }

    async function getJSON(path, signal) {
        const response = await fetch(path, {cache: 'no-store', signal});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
    }

    async function refresh() {
        const startedAt = performance.now();
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 4500);
        try {
            const results = await Promise.allSettled([
                getJSON('/api/aircraft', controller.signal), getJSON('/api/stats', controller.signal),
            ]);
            const [rows, stats] = results.map(result => result.value);
            if (!validAircraft(rows) || !validStats(stats)) throw new Error('Invalid snapshot');
            aircraft = rows;
            sampledAt = performance.now();
            updateMap(aircraft);
            updateTable();
            updateStats(stats);
            if (status.textContent) status.textContent = '';
        } catch (error) {
            if (!status.textContent) status.textContent = 'Refresh failed; displayed data may be stale. Retrying.';
        } finally {
            clearTimeout(timeout);
            setTimeout(refresh, Math.max(0, refreshInterval - (performance.now() - startedAt)));
        }
    }
    setTimeout(refresh, refreshInterval);
})();
