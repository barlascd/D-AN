import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import feedparser
import pycountry
import requests
from flask import Flask, Response, jsonify

app = Flask(__name__)
CACHE_SECONDS = 60 * 30

FEEDS = [
    ("UN News - Human Rights", "https://news.un.org/feed/subscribe/en/news/topic/human-rights/feed/rss.xml", "human_rights"),
    ("UN News - Health", "https://news.un.org/feed/subscribe/en/news/topic/health/feed/rss.xml", "health"),
    ("WHO Outbreak News", "https://www.who.int/feeds/entity/csr/don/en/rss.xml", "health"),
    ("UN News FR - Droits de l'homme", "https://news.un.org/feed/subscribe/fr/news/topic/human-rights/feed/rss.xml", "human_rights"),
    ("UN News ES - Derechos humanos", "https://news.un.org/feed/subscribe/es/news/topic/human-rights/feed/rss.xml", "human_rights"),
    ("UN News FR - Santé", "https://news.un.org/feed/subscribe/fr/news/topic/health/feed/rss.xml", "health"),
    ("UN News ES - Salud", "https://news.un.org/feed/subscribe/es/news/topic/health/feed/rss.xml", "health"),
    ("ICRC (via Google News)", "https://news.google.com/rss/search?q=site:icrc.org&hl=en&gl=US&ceid=US:en", "human_rights"),
    ("IFRC (via Google News)", "https://news.google.com/rss/search?q=site:ifrc.org&hl=en&gl=US&ceid=US:en", "health"),
]

KEYWORDS = {
    "human_rights": [
        "human rights", "refugee", "torture", "detention", "freedom of expression",
        "discrimination", "asylum", "war crimes", "civilian", "violence against women",
        "child labour", "press freedom", "genocide", "displacement",
        "international humanitarian law", "detainees", "prisoners of war",
        "missing persons", "family reunification", "protection of civilians",
        "droits de l'homme", "réfugié", "détention", "civils", "déplacés",
        "derechos humanos", "refugiado", "tortura", "detención", "discriminación", "desplazados",
    ],
    "health": [
        "outbreak", "epidemic", "pandemic", "vaccine", "disease", "cholera", "malaria",
        "measles", "ebola", "mpox", "health", "hospital", "mental health", "malnutrition",
        "red cross", "red crescent", "first aid", "medical aid", "ambulance",
        "attacks on health care", "water and sanitation", "psychosocial",
        "épidémie", "vaccin", "maladie", "choléra", "paludisme", "rougeole", "santé", "hôpital",
        "epidemia", "vacuna", "enfermedad", "cólera", "sarampión", "salud", "desnutrición",
    ],
}

EXTRA_NAMES = {
    "Russian Federation": "Russia", "Iran, Islamic Republic of": "Iran",
    "Syrian Arab Republic": "Syria", "Korea, Republic of": "South Korea",
    "Korea, Democratic People's Republic of": "North Korea",
    "Venezuela, Bolivarian Republic of": "Venezuela",
    "Bolivia, Plurinational State of": "Bolivia",
    "Tanzania, United Republic of": "Tanzania", "Viet Nam": "Vietnam",
    "Lao People's Democratic Republic": "Laos", "Moldova, Republic of": "Moldova",
    "Palestine, State of": "Palestine", "Congo, The Democratic Republic of the": "DR Congo",
    "Russia": "Russia", "Iran": "Iran", "Syria": "Syria", "Turkey": "Türkiye",
    "Türkiye": "Türkiye", "Gaza": "Palestine", "West Bank": "Palestine",
    "Palestine": "Palestine", "DR Congo": "DR Congo",
    "Democratic Republic of the Congo": "DR Congo", "Burma": "Myanmar",
    "Soudan": "Sudan", "Syrie": "Syria", "Siria": "Syria", "Ucrania": "Ukraine",
    "Birmanie": "Myanmar", "Afganistán": "Afghanistan", "Irak": "Iraq",
}

COUNTRY_NAMES = {}
for c in pycountry.countries:
    common = getattr(c, "common_name", None)
    display = EXTRA_NAMES.get(c.name, common or c.name)
    COUNTRY_NAMES[c.name] = display
    if common:
        COUNTRY_NAMES[common] = display
COUNTRY_NAMES.update(EXTRA_NAMES)

COUNTRY_PATTERNS = [
    (n, re.compile(rf"\b{re.escape(n)}\b", re.IGNORECASE))
    for n in sorted(COUNTRY_NAMES, key=len, reverse=True)
]

def detect_country(text):
    for name, pattern in COUNTRY_PATTERNS:
        if pattern.search(text):
            return COUNTRY_NAMES[name]
    return ""

def classify(text, default):
    text = text.lower()
    s = {cat: sum(text.count(k) for k in words) for cat, words in KEYWORDS.items()}
    hr, hl = s["human_rights"], s["health"]
    if hr == 0 and hl == 0:
        return default
    if hr > 0 and hl > 0 and min(hr, hl) >= 0.7 * max(hr, hl):
        return "both"
    return "human_rights" if hr > hl else "health"

def parse_date(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()

CACHE = {"time": 0, "items": [], "warnings": []}

def fetch_feed(feed_info):
    source, url, default_cat = feed_info
    try: 
        r = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0 (WorldMonitor)"})
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except Exception as ex:
        return [], f"{source}: cant read ({type(ex).__name__})"
    if not parsed.entries:
        return [], f"{source}: get back empty address may changed"
    items = []
    for e in parsed.entries:
        title = e.get("title", "")
        summary = re.sub(r"<[^>]+>", "", e.get("summary", ""))[:400]
        text = f"{title} {summary}"
        items.append({
            "link": e.get("link", ""),
            "title": title,
            "summary": summary,
            "source": source,
            "category": classify(text, default_cat),
            "country": detect_country(text),
            "published": parse_date(e),
        })
    return items, None

def get_data():
    if CACHE["items"] and time.time() - CACHE["time"] < CACHE_SECONDS:
        return CACHE
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_feed, FEEDS))
        seen, items, warnings = set(), [], []
        for feed_items, warn in results:
            if warn:
                warnings.append(warn)
            for it in feed_items:
                if it["link"] and it["link"] not in seen:
                    seen.add(it["link"])
                    items.append(it)
        if items:
            items.sort(key=lambda x: x["published"], reverse=True)
            CACHE.update({"time": time.time(), "items": items[:300], "warnings": warnings})
        else:
            CACHE["warnings"] = warnings
        return CACHE

@app.route("/api/data")
def api_data():
    data = get_data()
    resp = jsonify({
        "items": data["items"],
        "warnings": data["warnings"],
        "updated": datetime.fromtimestamp(data["time"], timezone.utc).isoformat() if data["time"] else None,
    })
    resp.headers["Cache-Control"] = "public, s-maxage=1800, stale-while-revalidate=3600"
    return resp

@app.route("/")
def index():
    resp = Response(HTML, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, s-maxage=3600"
    return resp

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DİAN</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #0f172a; color: #e2e8f0; }
  header { padding: 20px 28px 12px; border-bottom: 1px solid #1e293b; }
  h1 { margin: 0; font-size: 22px; }
  #status { font-size: 12px; color: #94a3b8; margin-top: 6px; }
  .mapwrap { position: relative; margin: 16px 28px 0; border-radius: 12px; overflow: hidden; border: 1px solid #1e293b; }
  #map { height: 420px; background: #0b1220; }
  .dark-tiles { filter: invert(1) hue-rotate(180deg) brightness(.9) contrast(.9) saturate(.6); }
  .mapbar { position: absolute; top: 10px; right: 10px; z-index: 1000; display: flex; gap: 6px; }
  .mapbar button { padding: 6px 12px; border: 0; border-radius: 20px; background: #1e293bdd; color: #e2e8f0; cursor: pointer; font-size: 13px; backdrop-filter: blur(4px); }
  .mapbar button:hover { background: #3b82f6; }
  .maplegend { position: absolute; bottom: 10px; left: 10px; z-index: 1000; background: #0f172acc; padding: 6px 10px; border-radius: 8px; font-size: 12px; color: #cbd5e1; }
  .leaflet-popup-content-wrapper { background: #1e293b; color: #e2e8f0; }
  .leaflet-popup-tip { background: #1e293b; }
  .leaflet-popup-content { font-size: 13px; line-height: 1.4; }
  .leaflet-popup-content b { color: #f1f5f9; }
  .layout { display: grid; grid-template-columns: 260px 1fr; gap: 20px; padding: 20px 28px; }
  .panel { background: #1e293b; border-radius: 10px; padding: 14px; }
  .filters button { margin: 0 6px 6px 0; padding: 6px 12px; border: 0; border-radius: 20px; background: #334155; color: #e2e8f0; cursor: pointer; }
  .filters button.active { background: #3b82f6; }
  .country-list { max-height: 300px; overflow-y: auto; margin-top: 10px; }
  .country { display: flex; justify-content: space-between; padding: 6px 0; cursor: pointer; border-bottom: 1px solid #334155; font-size: 14px; }
  .country:hover, .country.active { color: #60a5fa; }
  .items-container { max-height: 600px; overflow-y: auto; }
  .item { background: #1e293b; border-left: 4px solid #64748b; border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; }
  .item.human_rights { border-color: #f59e0b; }
  .item.health { border-color: #10b981; }
  .item.both { border-color: #a855f7; }
  .item a { color: #f1f5f9; text-decoration: none; font-weight: 600; }
  .meta { font-size: 12px; color: #94a3b8; margin-top: 4px; }
  .warn { font-size: 12px; color: #fbbf24; margin-top: 10px; }
  @media (max-width: 800px) {
    .layout { grid-template-columns: 1fr; }
    .mapwrap { margin: 12px 12px 0; }
    #map { height: 300px; }
  }
</style>
</head>
<body>
<header>
  <h1>🌍 DİAN</h1>
  <div id="status">Loading…</div>
</header>

<div class="mapwrap">
  <div id="map"></div>
  <div class="mapbar">
    <button id="tour-btn">▶ Tour</button>
  </div>
  <div class="maplegend">
    <div>🟠 Human Rights</div>
    <div>🟢 Health</div>
    <div>🟣 Both</div>
  </div>
</div>

<div class="layout">
  <div class="panel">
    <div class="filters" id="cat-filters">
      <button class="active" data-cat="all">All</button>
      <button data-cat="human_rights">Human Rights</button>
      <button data-cat="health">Health</button>
      <button data-cat="both">Both</button>
    </div>
    <div style="margin-top:12px; font-size:14px; font-weight:600;">Countries</div>
    <div class="country-list" id="country-list"></div>
  </div>
  <div class="panel items-container" id="items-container"></div>
</div>

<script>
  let allItems = [];
  let markers = [];
  let map;
  let activeCat = 'all';
  let activeCountry = 'all';
  let tourInterval = null;

  async function loadData() {
    try {
      const res = await fetch('/api/data');
      const data = await res.json();
      allItems = data.items || [];
      const updated = data.updated ? new Date(data.updated).toLocaleString() : 'Unknown';
      document.getElementById('status').textContent = `Updated: ${updated} | Items: ${allItems.length}`;
      if (data.warnings && data.warnings.length > 0) {
        document.getElementById('status').innerHTML += `<div class="warn">Warnings: ${data.warnings.join(', ')}</div>`;
      }
      initMap();
      renderCountries();
      renderItems();
    } catch (e) {
      document.getElementById('status').textContent = 'Error loading data: ' + e.message;
    }
  }

  function initMap() {
    map = L.map('map').setView([20, 0], 2);
    // API anahtarı gerektirmeyen, tamamen ücretsiz OpenStreetMap kullanıldı.
    // 'className: dark-tiles' eklenerek orijinal koyu tema tasarımı korundu.
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors',
      subdomains: 'abc',
      maxZoom: 19,
      className: 'dark-tiles'
    }).addTo(map);
  }

  function getCountryCoords(countryName) {
    const coords = {
      "Syria": [34.8021, 38.9968], "Ukraine": [48.3794, 31.1656], "Palestine": [31.9522, 35.2332],
      "Myanmar": [21.9162, 95.9560], "Sudan": [12.8628, 30.2176], "Afghanistan": [33.9391, 67.7100],
      "Iraq": [33.2232, 43.6793], "Russia": [61.5240, 105.3188], "Iran": [32.4279, 53.6880],
      "DR Congo": [-4.0383, 21.7587], "Venezuela": [6.4238, -66.5897], "Türkiye": [38.9637, 35.2433],
      "Yemen": [15.5527, 48.5164], "Somalia": [5.1521, 46.1996], "South Sudan": [6.8770, 31.3070],
      "Nigeria": [9.0820, 8.6753], "Ethiopia": [9.1450, 40.4897], "Mali": [17.5707, -3.9962],
      "Gaza": [31.5017, 34.4668], "West Bank": [31.9522, 35.2332], "Lebanon": [33.8547, 35.8623],
      "Israel": [31.0461, 34.8516], "Egypt": [26.8206, 30.8025], "Libya": [26.3351, 17.2283],
      "Pakistan": [30.3753, 69.3451], "India": [20.5937, 78.9629], "China": [35.8617, 104.1954],
      "United States": [37.0902, -95.7129], "United Kingdom": [55.3781, -3.4360], "France": [46.2276, 2.2137],
      "Germany": [51.1657, 10.4515], "Poland": [51.9194, 19.1451], "Romania": [45.9432, 24.9668],
      "Brazil": [-14.2350, -51.9253], "Colombia": [4.5709, -74.2973], "Mexico": [23.6345, -102.5528],
      "South Africa": [-30.5595, 22.9375], "Kenya": [-0.0236, 37.9062], "Niger": [17.6078, 8.0817],
      "Burkina Faso": [12.2383, -1.5616], "Cameroon": [7.3697, 12.3547], "Chad": [15.4542, 18.7322],
      "Haiti": [18.9712, -72.2852], "Cuba": [21.5218, -77.7812], "Nicaragua": [12.8654, -85.2072],
      "Honduras": [15.2000, -86.2419], "Guatemala": [15.7835, -90.2308], "El Salvador": [13.7942, -88.8965],
      "Peru": [-9.1900, -75.0152], "Ecuador": [-1.8312, -78.1834], "Bolivia": [-16.2902, -63.5887],
      "Paraguay": [-23.4425, -58.4438], "Uruguay": [-32.5228, -55.7658], "Argentina": [-38.4161, -63.6167],
      "Chile": [-35.6751, -71.5430], "Australia": [-25.2744, 133.7751], "New Zealand": [-40.9006, 174.8860],
      "Japan": [36.2048, 138.2529], "South Korea": [35.9078, 127.7669], "North Korea": [40.3399, 127.5101],
      "Vietnam": [14.0583, 108.2772], "Thailand": [15.8700, 100.9925], "Philippines": [12.8797, 121.7740],
      "Indonesia": [-0.7893, 113.9213], "Malaysia": [4.2105, 101.9758], "Singapore": [1.3521, 103.8198],
      "Saudi Arabia": [23.8859, 45.0792], "Jordan": [30.5852, 36.2384], "Turkey": [38.9637, 35.2433],
      "Greece": [39.0742, 21.8243], "Italy": [41.8719, 12.5674], "Spain": [40.4637, -3.7492],
      "Portugal": [39.3999, -8.2245], "Netherlands": [52.1326, 5.2913], "Belgium": [50.5039, 4.4699],
      "Sweden": [60.1282, 18.6435], "Norway": [60.4720, 8.4689], "Finland": [61.9241, 25.7482],
      "Denmark": [56.2639, 9.5018], "Ireland": [53.4129, -8.2439], "Iceland": [64.9631, -19.0208],
      "Canada": [56.1304, -106.3468], "Algeria": [28.0339, 1.6596], "Morocco": [31.7917, -7.0926],
      "Tunisia": [33.8869, 9.5375], "Senegal": [14.4974, -14.4524], "Ghana": [7.9465, -1.0232],
      "Zimbabwe": [-19.0154, 29.1549], "Mozambique": [-18.6657, 35.5296], "Angola": [-11.2027, 17.8739],
      "Zambia": [-13.1339, 27.8493], "Tanzania": [-6.3690, 34.8888], "Uganda": [1.3733, 32.2903],
      "Rwanda": [-1.9403, 29.8739], "Burundi": [-3.3731, 29.9189], "Madagascar": [-18.7669, 46.8691],
      "Mauritania": [21.0079, -10.9408], "Central African Republic": [6.6111, 20.9394],
      "Republic of the Congo": [-0.2280, 15.8277], "Gabon": [-0.8037, 11.6094],
      "Equatorial Guinea": [1.6508, 10.2679], "Togo": [8.6195, 0.8248], "Benin": [9.3077, 2.3158],
      "Ivory Coast": [7.5400, -5.5471], "Liberia": [6.4281, -9.4295], "Sierra Leone": [8.4606, -11.7799],
      "Guinea": [9.9456, -9.6966], "Guinea-Bissau": [11.8037, -15.1804], "Gambia": [13.4432, -15.3101],
      "Cape Verde": [16.5388, -23.0418], "Sao Tome and Principe": [0.1864, 6.6131],
      "Seychelles": [-4.6796, 55.4920], "Mauritius": [-20.3484, 57.5522], "Comoros": [-11.6455, 43.3333],
      "Djibouti": [11.8251, 42.5903], "Eritrea": [15.1794, 39.7823], "Malawi": [-13.2543, 34.3015],
      "Botswana": [-22.3285, 24.6849], "Namibia": [-22.9576, 18.4904], "Lesotho": [-29.6100, 28.2336],
      "Eswatini": [-26.5225, 31.4659], "Sri Lanka": [7.8731, 80.7718], "Bangladesh": [23.6850, 90.3563],
      "Nepal": [28.3949, 84.1240], "Bhutan": [27.5142, 90.4336], "Maldives": [3.2028, 73.2207],
      "Laos": [19.8563, 102.4955], "Cambodia": [12.5657, 104.9910], "Mongolia": [46.8625, 103.8467],
      "Kazakhstan": [48.0196, 66.9237], "Uzbekistan": [41.3775, 64.5853], "Turkmenistan": [38.9697, 59.5563],
      "Kyrgyzstan": [41.2044, 74.7661], "Tajikistan": [38.8610, 71.2761], "Armenia": [40.0691, 45.0382],
      "Azerbaijan": [40.1431, 47.5769], "Georgia": [42.3154, 43.3569], "Belarus": [53.7098, 27.9534],
      "Moldova": [47.4116, 28.3699], "Latvia": [56.8796, 24.6032], "Lithuania": [55.1694, 23.8813],
      "Estonia": [58.5953, 25.0136], "Albania": [41.1533, 20.1683], "Bosnia and Herzegovina": [43.9159, 17.6791],
      "Croatia": [45.1000, 15.2000], "Serbia": [44.0165, 21.0059], "Montenegro": [42.7087, 19.3744],
      "North Macedonia": [41.6086, 21.7453], "Slovenia": [46.1512, 14.9955], "Slovakia": [48.6690, 19.6990],
      "Czech Republic": [49.8175, 15.4730], "Hungary": [47.1625, 19.5033], "Austria": [47.5162, 14.5501],
      "Switzerland": [46.8182, 8.2275], "Luxembourg": [49.8153, 6.1296], "Malta": [35.9375, 14.3754],
      "Cyprus": [35.1264, 33.4299], "Kuwait": [29.3117, 47.4818], "Bahrain": [26.0667, 50.5577],
      "Qatar": [25.3548, 51.1839], "United Arab Emirates": [23.4241, 53.8478], "Oman": [21.4735, 55.9754]
    };
    return coords[countryName] || [20, 0];
  }

  function renderMarkers(items) {
    markers.forEach(m => map.removeLayer(m));
    markers = [];
    items.forEach(item => {
      if (item.country) {
        const coords = getCountryCoords(item.country);
        const color = item.category === 'human_rights' ? '#f59e0b' : item.category === 'health' ? '#10b981' : '#a855f7';
        const marker = L.circleMarker(coords, {
          radius: 8, fillColor: color, color: '#fff', weight: 1, opacity: 1, fillOpacity: 0.8
        }).addTo(map);
        marker.bindPopup(`<b>${item.title}</b><br>${item.source}<br><a href="${item.link}" target="_blank" style="color:#60a5fa">Read more</a>`);
        markers.push(marker);
      }
    });
  }

  function renderCountries() {
    const countries = [...new Set(allItems.map(i => i.country).filter(Boolean))].sort();
    const container = document.getElementById('country-list');
    container.innerHTML = `<div class="country active" data-country="all">All Countries <span>${allItems.length}</span></div>`;
    countries.forEach(c => {
      const count = allItems.filter(i => i.country === c).length;
      const div = document.createElement('div');
      div.className = 'country';
      div.dataset.country = c;
      div.innerHTML = `<span>${c}</span><span>${count}</span>`;
      div.onclick = () => {
        document.querySelectorAll('.country').forEach(el => el.classList.remove('active'));
        div.classList.add('active');
        activeCountry = c;
        renderItems();
      };
      container.appendChild(div);
    });
  }

  function renderItems() {
    const container = document.getElementById('items-container');
    container.innerHTML = '';
    const filtered = allItems.filter(item => {
      const catMatch = activeCat === 'all' || item.category === activeCat;
      const countryMatch = activeCountry === 'all' || item.country === activeCountry;
      return catMatch && countryMatch;
    });
    renderMarkers(filtered);
    if (filtered.length === 0) {
      container.innerHTML = '<div style="padding:20px; text-align:center; color:#94a3b8;">No items found.</div>';
      return;
    }
    filtered.forEach(item => {
      const div = document.createElement('div');
      div.className = `item ${item.category}`;
      div.innerHTML = `
        <a href="${item.link}" target="_blank">${item.title}</a>
        <div class="meta">${item.source} | ${item.country || 'Global'} | ${new Date(item.published).toLocaleDateString()}</div>
        <div style="margin-top:6px; font-size:13px; color:#cbd5e1;">${item.summary}</div>
      `;
      container.appendChild(div);
    });
  }

  document.getElementById('cat-filters').addEventListener('click', (e) => {
    if (e.target.tagName === 'BUTTON') {
      document.querySelectorAll('#cat-filters button').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      activeCat = e.target.dataset.cat;
      renderItems();
    }
  });

  document.getElementById('tour-btn').addEventListener('click', () => {
    if (tourInterval) {
      clearInterval(tourInterval);
      tourInterval = null;
      document.getElementById('tour-btn').textContent = '▶ Tour';
      return;
    }
    document.getElementById('tour-btn').textContent = '⏸ Stop';
    let idx = 0;
    const tourItems = allItems.filter(i => i.country);
    tourInterval = setInterval(() => {
      if (idx >= tourItems.length) idx = 0;
      const item = tourItems[idx];
      const coords = getCountryCoords(item.country);
      map.flyTo(coords, 5, { duration: 1.5 });
      const marker = markers.find(m => {
        const ll = m.getLatLng();
        return Math.abs(ll.lat - coords[0]) < 0.1 && Math.abs(ll.lng - coords[1]) < 0.1;
      });
      if (marker) marker.openPopup();
      idx++;
    }, 3000);
  });

  loadData();
</script>
</body>
</html>"""

