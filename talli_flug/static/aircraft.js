/* Keep the server-rendered table and the map on the same five-second snapshot. */
(() => {
    const initial = JSON.parse(document.getElementById('map-data').textContent);
    const markers = new Map();
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
                entry.row = row;
                entry.marker.setLatLng(position).setIcon(icon(row.track));
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
    }

    // Age continues increasing even if the receiver or refresh request stops.
    setInterval(() => {
        for (const entry of markers.values()) {
            if (entry.marker.isPopupOpen()) entry.marker.setPopupContent(() => popup(entry.row));
        }
    }, 1000);

    async function refresh() {
        try {
            const response = await fetch('/', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
            if (!response.ok) throw new Error('Refresh failed');
            const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
            const data = JSON.parse(doc.getElementById('map-data').textContent);
            document.querySelector('tbody').replaceWith(doc.querySelector('tbody'));
            document.getElementById('receiver-status').textContent = doc.getElementById('receiver-status').textContent;
            sampledAt = performance.now();
            updateMap(data.aircraft);
            document.getElementById('refresh-status').textContent = '';
        } catch (error) {
            document.getElementById('refresh-status').textContent = 'Refresh failed; displayed observations may be stale. Retrying.';
        } finally {
            setTimeout(refresh, 5000);
        }
    }
    setTimeout(refresh, 5000);
})();
