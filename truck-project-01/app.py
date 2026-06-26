import copy
import html as _html_mod
import math
import os
import time
import yaml
import streamlit as st
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

import datetime
from src.models import Order, Truck
from src.parser import parse_and_verify
from src.import_fenevision import import_fenevision_xlsx, parse_route_truck_summary
from src.export_html import generate_html_routes
from src.optimizer import (
    solve,
    validate_inputs,
    check_route_cap,
    geocode_address,
    GEOCODING_AVAILABLE,
)

try:
    import folium
    from streamlit_folium import st_folium
    FOLIUM_AVAILABLE = True
except ImportError:
    FOLIUM_AVAILABLE = False

CONFIG_PATH = Path("config.yaml")

_SOLVE_ANIMATION_HTML = """
<div style="text-align:center;padding:2.5rem 0;font-family:Arial,sans-serif;">
  <svg width="72" height="72" viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg"
       style="display:block;margin:0 auto 1.25rem;">
    <style>
      @keyframes _fp{0%,100%{opacity:.2}50%{opacity:.9}}
      .wp{fill:#c8e6f7;animation:_fp 2.5s ease-in-out infinite;}
      .wf{fill:none;stroke:#1a5fa8;stroke-width:3;}
      .wb{stroke:#1a5fa8;stroke-width:2;}
    </style>
    <rect class="wf" x="10" y="10" width="60" height="60" rx="3"/>
    <rect class="wp" x="14" y="14" width="24" height="24" style="animation-delay:0s"/>
    <rect class="wp" x="42" y="14" width="24" height="24" style="animation-delay:.35s"/>
    <rect class="wp" x="14" y="42" width="24" height="24" style="animation-delay:.7s"/>
    <rect class="wp" x="42" y="42" width="24" height="24" style="animation-delay:1.05s"/>
    <line class="wb" x1="40" y1="10" x2="40" y2="70"/>
    <line class="wb" x1="10" y1="40" x2="70" y2="40"/>
  </svg>
  <div style="position:relative;height:1.8rem;overflow:hidden;">
    <style>
      @keyframes _m1{0%{opacity:0}3%{opacity:1}17%{opacity:1}20%{opacity:0}100%{opacity:0}}
      @keyframes _m2{0%,20%{opacity:0}23%{opacity:1}37%{opacity:1}40%{opacity:0}100%{opacity:0}}
      @keyframes _m3{0%,40%{opacity:0}43%{opacity:1}57%{opacity:1}60%{opacity:0}100%{opacity:0}}
      @keyframes _m4{0%,60%{opacity:0}63%{opacity:1}77%{opacity:1}80%{opacity:0}100%{opacity:0}}
      @keyframes _m5{0%,80%{opacity:0}83%{opacity:1}97%{opacity:1}100%{opacity:0}}
      .sm{position:absolute;width:100%;text-align:center;font-size:.95rem;color:#555;
          animation-duration:20s;animation-iteration-count:infinite;animation-fill-mode:both;}
    </style>
    <p class="sm" style="animation-name:_m1">Reading stops across your routes…</p>
    <p class="sm" style="animation-name:_m2">Checking homebuilder truck restrictions…</p>
    <p class="sm" style="animation-name:_m3">Assigning stops to trucks…</p>
    <p class="sm" style="animation-name:_m4">Sequencing delivery stops by distance…</p>
    <p class="sm" style="animation-name:_m5">Building LIFO load order…</p>
  </div>
</div>
"""

_SOLVE_DONE_HTML = """
<div style="text-align:center;padding:1.5rem 0;font-family:Arial,sans-serif;
            color:#1a7a1a;font-size:1rem;">
  &#10003; Routes are ready &mdash; head to the <strong>Load Plan</strong> tab
  to see assignments and export for drivers.
</div>
"""


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def init_state():
    defaults = {
        "orders": [],
        "pending": None,
        "assignments": [],
        "dropped": [],
        "uploader_key": 0,
        "auto_run_pending": False,
        "dismiss_packing_issues": False,
        "fv_filename": None,
        "first_visit": True,     # drives one-time onboarding modal
        "onboarding_slide": 0,
        "_ob_navigating": False, # True only when a slide nav button was just clicked
        "routing_phase": None,   # None | "distribution_done" — tracks two-phase solve progress
        "auto_geocode_pending": False,   # set on xlsx import; consumed in render_load_plan before auto-solve
        "multiday_assignments": [],      # separate solve result for overnight/long-haul stops
        "multiday_dropped": [],
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


@st.dialog("🪟 Lindsay Windows Load Planner", width="large")
def _onboarding_modal():
    slides = [
        (
            "👋 Welcome!",
            "#1a5fa8",
            "This tool takes your FeneVision delivery data and builds an optimized load plan "
            "for each truck — automatically. It sequences stops, enforces truck restrictions "
            "for homebuilder customers, and prints route sheets drivers can scan with their phone.",
        ),
        (
            "📂 Upload your FeneVision file",
            "#2e7d32",
            "Go to **Add Orders** and upload the xlsx export from FeneVision (GA Trucks format). "
            "The tool reads the 'Orders by Route' sheet, groups line items into stops, and "
            "loads them automatically. Interplant routes are excluded based on your config.",
        ),
        (
            "🗺️ Review the Load Plan",
            "#6a1b9a",
            "Head to **Load Plan** — it runs automatically after upload. "
            "You'll see each truck's delivery sequence and LIFO loading order. "
            "Click **Regenerate Load Plan** anytime to re-run with updated settings.",
        ),
        (
            "📊 Check the Analysis tab",
            "#e65100",
            "**Analysis** shows utilization per truck, a homebuilder constraint audit "
            "(flags any stop assigned to the wrong truck type), and a cost comparison "
            "against unoptimized routing. Export an Excel report from here.",
        ),
        (
            "🖨️ Print for drivers",
            "#1565c0",
            "Click **Export Route Sheets (HTML)** in the Load Plan tab and open it in your browser. "
            "Hit **Cmd+P** to print — each truck gets its own page.\n\n"
            "**Morning dispatch workflow:**\n"
            "1. Open the app → upload today's FeneVision file\n"
            "2. Wait ~30 seconds for the optimizer to run\n"
            "3. Export route sheets → print one page per driver\n"
            "4. Drivers scan the QR code to open their full route in Google Maps\n"
            "5. Hand sheets to drivers before they leave the dock",
        ),
    ]

    idx = st.session_state.get("onboarding_slide", 0)
    title, accent, body = slides[idx]

    # Progress dots
    dots = ""
    for i in range(len(slides)):
        color = accent if i == idx else "#dde3ea"
        dots += (
            f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
            f'background:{color};margin:0 5px;transition:background 0.3s;"></span>'
        )

    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,{accent}18,{accent}08);
                    border-left:4px solid {accent};border-radius:8px;
                    padding:18px 20px;margin-bottom:16px;">
            <div style="font-size:1.35rem;font-weight:700;color:{accent};
                        margin-bottom:4px;">{title}</div>
            <div style="color:#444;margin-top:2px;font-size:0.8rem;letter-spacing:0.5px;">
                STEP {idx + 1} OF {len(slides)}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(body)

    st.markdown(
        f'<div style="text-align:center;margin-top:20px;margin-bottom:4px;">{dots}</div>',
        unsafe_allow_html=True,
    )

    col_back, col_spacer, col_next = st.columns([1, 3, 1])
    if idx > 0:
        if col_back.button("← Back", key="ob_back"):
            st.session_state.onboarding_slide = idx - 1
            st.session_state._ob_navigating = True
            st.rerun()
    if idx < len(slides) - 1:
        if col_next.button("Next →", key="ob_next", type="primary"):
            st.session_state.onboarding_slide = idx + 1
            st.session_state._ob_navigating = True
            st.rerun()
    else:
        if col_next.button("Got it!", type="primary", key="ob_done"):
            st.session_state.onboarding_slide = 0
            st.session_state._jump_orders = True
            st.rerun()


_LINDSAY_CSS = """
<style>
/* ── Lindsay Windows design system ─────────────────────────────────────── */
/* Colors match FeneVision BI (blue #1a7cb8, orange #F58220) +
   the internal Inventory Count webapp (white cards, #f0f4f8 bg).         */

/* Top toolbar → Lindsay blue */
header[data-testid="stHeader"] {
    background: #1a7cb8 !important;
}
header[data-testid="stHeader"] * { color: #fff !important; }

/* Kill Streamlit's opacity-fade on main content headings only */
.main [data-testid="stMarkdownContainer"] h1,
.main [data-testid="stMarkdownContainer"] h2,
.main [data-testid="stMarkdownContainer"] h3,
[data-testid="stTitle"] {
    color: #1a1a2e !important;
    opacity: 1 !important;
}
/* Metric labels */
[data-testid="stMetricLabel"] {
    color: #555 !important;
    opacity: 1 !important;
}

/* Sidebar → white with Lindsay blue left accent */
section[data-testid="stSidebar"] {
    background: #fff !important;
    border-right: none !important;
    border-left: 4px solid #1a7cb8 !important;
    box-shadow: 2px 0 8px rgba(26,124,184,0.08) !important;
}
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] .stMarkdown,
section[data-testid="stSidebar"] h1,
section[data-testid="stSidebar"] h2,
section[data-testid="stSidebar"] h3 {
    color: #1a1a2e !important;
}

/* Tab bar — scoped to the main tab strip, not sidebar buttons */
[data-testid="stTabs"] button[data-baseweb="tab"] {
    color: #1a1a2e !important;
    font-weight: 500 !important;
}
/* Active tab gets orange underline */
[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
    border-bottom: 3px solid #F58220 !important;
    color: #F58220 !important;
    font-weight: 700 !important;
}

/* Primary buttons → orange */
div.stButton > button[kind="primary"],
button[kind="primaryFormSubmit"] {
    background: #F58220 !important;
    border: none !important;
    color: #fff !important;
    font-weight: 700 !important;
    border-radius: 6px !important;
}
div.stButton > button[kind="primary"]:hover {
    background: #d9701a !important;
}

/* Metric cards — white with subtle shadow */
div[data-testid="metric-container"] {
    background: #fff;
    border-radius: 8px;
    padding: 12px 16px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
}

/* Info boxes — Lindsay blue tint */
div[data-testid="stInfo"] {
    border-left: 4px solid #1a7cb8 !important;
    background: #deeef9 !important;
}

/* Sidebar sliders — blue track + thumb on white background */
section[data-testid="stSidebar"] [data-testid="stSlider"] div[data-baseweb="slider"] > div:first-child {
    background: #dde3ea !important;
}
section[data-testid="stSidebar"] [data-testid="stSlider"] div[data-baseweb="slider"] div[role="slider"] {
    background: #1a7cb8 !important;
    border: 2px solid #1a7cb8 !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.15) !important;
}
section[data-testid="stSidebar"] [data-testid="stSlider"] div[data-baseweb="slider"] div[class*="Track"] {
    background: #1a7cb8 !important;
}
section[data-testid="stSidebar"] [data-testid="stSlider"] div[data-baseweb="slider"] div[class*="Tick"] span {
    color: #555 !important;
}

/* Primary solve/action button — orange, prominent */
button[data-testid="baseButton-primary"] {
    background: #F58220 !important;
    border: none !important;
    font-weight: 800 !important;
    font-size: 15px !important;
    padding: 12px 32px !important;
    border-radius: 8px !important;
    box-shadow: 0 4px 14px rgba(245,130,32,0.30) !important;
}
button[data-testid="baseButton-primary"]:hover {
    background: #d96e10 !important;
    box-shadow: 0 6px 18px rgba(245,130,32,0.40) !important;
}

/* Mode radio → pill style */
div[data-testid="stRadio"] > div {
    gap: 8px !important;
    flex-wrap: wrap !important;
}
div[data-testid="stRadio"] label {
    background: #fff !important;
    border: 2px solid #dde3ea !important;
    border-radius: 50px !important;
    padding: 7px 18px !important;
    font-weight: 700 !important;
    color: #555 !important;
    cursor: pointer !important;
}
div[data-testid="stRadio"] label:has(input:checked) {
    background: #1a7cb8 !important;
    border-color: #1a7cb8 !important;
    color: #fff !important;
}
div[data-testid="stRadio"] input { display: none !important; }

/* Route expander cards — shadow + border-radius */
details[data-testid="stExpander"] {
    border-radius: 10px !important;
    box-shadow: 0 2px 10px rgba(0,0,0,0.09) !important;
    border: 1px solid #edf0f4 !important;
    overflow: hidden !important;
    margin-bottom: 10px !important;
}
details[data-testid="stExpander"] summary {
    font-weight: 700 !important;
    color: #1a1a2e !important;
    font-size: 15px !important;
}
details[data-testid="stExpander"] summary p:first-child::first-letter {
    font-size: 22px !important;
}
</style>
"""


def _inject_lindsay_css() -> None:
    st.markdown(_LINDSAY_CSS, unsafe_allow_html=True)


def _sb_section(label: str, right: str = "") -> None:
    right_html = f"<span style='margin-left:auto;font-size:11px;color:#888;font-weight:700;'>{right}</span>" if right else ""
    st.sidebar.markdown(
        f"<div style='font-size:11px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;"
        f"color:#1a7cb8;margin:14px 0 8px;display:flex;align-items:center;'>"
        f"{label}{right_html}</div>",
        unsafe_allow_html=True,
    )


def _sb_chip_row(label: str, value: str) -> None:
    st.sidebar.markdown(
        f"<div style='display:flex;align-items:center;justify-content:space-between;"
        f"margin-bottom:7px;'>"
        f"<span style='font-size:13px;color:#333;'>{label}</span>"
        f"<span style='background:#f0f4f8;border-radius:5px;padding:3px 10px;"
        f"font-size:12px;color:#1a1a2e;font-weight:700;'>{value}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


def render_sidebar(cfg: dict) -> dict:
    # ── Logo ──
    st.sidebar.markdown(
        "<div style='padding:14px 0 14px;border-bottom:2px solid #eef2f7;"
        "margin-bottom:2px;display:flex;gap:12px;align-items:center;'>"
        "<div style='width:42px;height:42px;background:#1a7cb8;border-radius:8px;"
        "display:flex;align-items:center;justify-content:center;"
        "color:#fff;font-size:20px;font-weight:900;flex-shrink:0;'>L</div>"
        "<div><div style='font-size:15px;font-weight:800;color:#1a1a2e;line-height:1.2;'>Lindsay Windows</div>"
        "<div style='font-size:11px;color:#888;margin-top:2px;'>Load Planner · GA Plant</div></div>"
        "</div>",
        unsafe_allow_html=True,
    )

    routing = cfg.get("routing", {})
    abbr = cfg["measurement"]["abbreviation"]
    unit = cfg["measurement"]["unit"]
    label = cfg["measurement"]["label"]
    depot_name = cfg["depot"].get("name", "")
    depot_addr = cfg["depot"].get("address", "")

    # ── Fleet ──
    _active_trucks = [t for t in cfg["trucks"] if t.get("active", True)]
    _total_cap = sum(t.get("max_capacity", 0) for t in _active_trucks)
    _sb_section("Fleet — Today's Trucks", f"{_total_cap:.0f} sq ft total")

    updated_trucks = []
    _delete_triggered = False
    for i, truck in enumerate(cfg["trucks"]):
        _icon = "🚚" if truck.get("type") == "trailer" else "🚛"
        _is_trailer = truck.get("type") == "trailer"
        _accent = "#F58220" if _is_trailer else "#1a7cb8"
        # Single unified row: icon | name+driver | CT | ×
        _cicon, _cinfo, _cct, _cdel = st.sidebar.columns([0.55, 4, 0.55, 0.55])
        _cicon.markdown(
            f"<div style='padding-top:20px;text-align:center;font-size:22px;'>{_icon}</div>",
            unsafe_allow_html=True,
        )
        _cinfo.markdown(
            f"<div style='font-size:11px;font-weight:800;color:#1a1a2e;margin-bottom:1px;"
            f"border-left:3px solid {_accent};padding-left:6px;margin-top:4px;'>"
            f"{truck['name']}"
            f"<span style='font-size:10px;font-weight:600;color:#999;margin-left:5px;'>"
            f"{truck.get('max_capacity',0):.0f} sq ft</span></div>",
            unsafe_allow_html=True,
        )
        driver = _cinfo.text_input("Driver", value=truck.get("driver", ""), key=f"t_driver_{i}",
                                   label_visibility="collapsed", placeholder="Driver name")
        is_contract = _cct.checkbox("CT", value=truck.get("employment_type") == "contract",
                                    key=f"t_ct_{i}", label_visibility="collapsed",
                                    help="Contractor")
        delete = _cdel.button("✕", key=f"t_del_{i}", help="Remove truck")
        st.sidebar.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)
        active = truck.get("active", True)
        name = truck["name"]
        cap = float(truck["max_capacity"])
        if delete:
            _delete_triggered = True
        else:
            updated_trucks.append({
                "name": name, "driver": driver, "type": truck["type"],
                "max_capacity": cap, "fixed_cost": truck.get("fixed_cost", 5.0),
                "cost_per_mile": truck.get("cost_per_mile", 0.0),
                "employment_type": "contract" if is_contract else "fulltime",
                "active": active,
            })

    # Type-specific add buttons
    st.sidebar.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)
    _ba, _bb = st.sidebar.columns(2)
    _add_straight = _ba.button("＋ 26ft Straight", use_container_width=True, help="Add 26ft straight truck")
    _add_trailer  = _bb.button("＋ 53ft Trailer",  use_container_width=True, help="Add 53ft trailer")

    # ── Routing Config chips ──
    max_hours      = float(routing.get("max_route_hours", 9.0))
    stop_time      = float(routing.get("stop_time_minutes", 45))
    max_fill_pct   = int(routing.get("max_fill_pct", 90))
    straight_speed = int(routing.get("straight_speed_mph", 47))
    trailer_speed  = int(routing.get("trailer_speed_mph", 40))
    manual_truck_count = int(routing.get("manual_truck_count", 13))
    min_sqft_sidebar   = float(routing.get("min_sqft_threshold", 5.0))
    exclude_patterns_raw = "\n".join(routing.get("exclude_route_patterns", ["Lindsay MO"]))
    solver_time_limit = int(routing.get("solver_time_limit_seconds", 15))
    osrm_server = routing.get("osrm_server", "")

    _sb_section("Routing Config")
    _sb_chip_row("Max route hrs", f"{max_hours:.1f} hr")
    _sb_chip_row("Stop time", f"{int(stop_time)} min")
    _sb_chip_row("Straight speed", f"{straight_speed} mph")
    _sb_chip_row("Trailer speed", f"{trailer_speed} mph")
    _sb_chip_row("Max fill", f"{max_fill_pct}%")
    if depot_name:
        _sb_chip_row("Depot", depot_name)

    with st.sidebar.expander("✏️ Edit Routing Config", expanded=False):
        max_hours = st.number_input("Max route hrs / driver", value=max_hours,
                                    min_value=1.0, max_value=14.0, step=0.5,
                                    help="9 hr cap = drive + unload time.")
        stop_time = st.number_input("Stop time (minutes)", value=stop_time,
                                    min_value=5.0, max_value=180.0, step=5.0)
        max_fill_pct = st.slider("Max fill %", 50, 100, max_fill_pct, step=5)
        straight_speed = st.number_input("Straight speed (mph)", value=float(straight_speed),
                                         min_value=10.0, max_value=80.0, step=1.0)
        trailer_speed = st.number_input("Trailer speed (mph)", value=float(trailer_speed),
                                        min_value=10.0, max_value=80.0, step=1.0)
        manual_truck_count = st.number_input("Manual route count (for comparison)",
                                             value=float(manual_truck_count), min_value=1.0, step=1.0)
        depot_name = st.text_input("Depot name", value=depot_name)
        depot_addr = st.text_input("Depot address", value=depot_addr)

    with st.sidebar.expander("🔍 Import Filters", expanded=False):
        min_sqft_sidebar = st.number_input("Min stop size (sq ft)", value=min_sqft_sidebar,
                                           min_value=0.0, max_value=50.0, step=1.0,
                                           help="Stops below this sq ft are skipped on import.")
        exclude_patterns_raw = st.text_area("Exclude route patterns (one per line)",
                                            value=exclude_patterns_raw, height=70,
                                            help="Case-insensitive substring match on RouteName.")

    def _build_cfg(trucks_list):
        return {
            "measurement": {"unit": unit, "label": label, "abbreviation": abbr},
            "trucks": trucks_list,
            "depot": {"name": depot_name, "address": depot_addr},
            "routing": {
                "max_route_hours": max_hours,
                "stop_time_minutes": stop_time,
                "max_fill_pct": max_fill_pct,
                "manual_truck_count": int(manual_truck_count),
                "straight_speed_mph": int(straight_speed),
                "trailer_speed_mph": int(trailer_speed),
                "solver_time_limit_seconds": solver_time_limit,
                "exclude_route_patterns": [p.strip() for p in exclude_patterns_raw.splitlines() if p.strip()],
                "min_sqft_threshold": min_sqft_sidebar,
                "osrm_server": osrm_server,
            },
        }

    if _delete_triggered:
        save_config(_build_cfg(updated_trucks))
        st.rerun()

    if _add_straight:
        n = len(updated_trucks) + 1
        save_config(_build_cfg(updated_trucks + [{
            "name": f"26ft Straight #{n}", "driver": "", "type": "straight",
            "max_capacity": 208.0, "fixed_cost": 5.0, "cost_per_mile": 1.75,
            "employment_type": "fulltime", "active": True,
        }]))
        st.rerun()

    if _add_trailer:
        n = len(updated_trucks) + 1
        save_config(_build_cfg(updated_trucks + [{
            "name": f"53ft Trailer #{n}", "driver": "", "type": "trailer",
            "max_capacity": 431.06, "fixed_cost": 8.0, "cost_per_mile": 2.10,
            "employment_type": "fulltime", "active": True,
        }]))
        st.rerun()

    if st.sidebar.button("💾 Save Config", use_container_width=True):
        save_config(_build_cfg(updated_trucks))
        st.sidebar.success("Saved.")
        st.rerun()

    # ── Session persistence ──
    st.sidebar.divider()
    st.sidebar.markdown(
        "<div style='font-size:9px;font-weight:800;letter-spacing:1.1px;text-transform:uppercase;"
        "color:#1a7cb8;margin-bottom:6px;'>Save / Load Plan</div>",
        unsafe_allow_html=True,
    )
    from src.persistence import serialize_plan, deserialize_plan

    if st.session_state.get("assignments"):
        _plan_bytes = serialize_plan(
            orders=st.session_state.get("orders", []),
            assignments=st.session_state.assignments,
            dropped=st.session_state.get("dropped", []),
            supervisor_routes=st.session_state.get("supervisor_routes", []),
            fv_filename=st.session_state.get("fv_filename", ""),
        )
        st.sidebar.download_button(
            "⬇ Save plan",
            data=_plan_bytes,
            file_name="lindsay_plan.json",
            mime="application/json",
            use_container_width=True,
            key="save_plan_btn",
        )
    else:
        st.sidebar.caption("Optimize a plan to enable Save.")

    _plan_upload = st.sidebar.file_uploader(
        "Load saved plan (.json)", type=["json"], key="plan_upload", label_visibility="collapsed"
    )
    if _plan_upload is not None:
        try:
            _orders, _asgn, _dropped, _sup_routes, _fv_fn = deserialize_plan(_plan_upload.read())
            st.session_state.orders = _orders
            st.session_state.assignments = _asgn
            st.session_state.dropped = _dropped
            st.session_state.supervisor_routes = _sup_routes
            if _fv_fn:
                st.session_state.fv_filename = _fv_fn
            st.sidebar.success(f"Plan restored — {len(_asgn)} truck(s), {len(_orders)} order(s).")
            st.rerun()
        except ValueError as e:
            st.sidebar.error(str(e))

    st.sidebar.divider()
    if st.sidebar.button("Need Help?", key="sidebar_help", use_container_width=True):
        st.session_state.first_visit = True
        st.session_state.onboarding_slide = 0
        st.rerun()

    return cfg


def render_add_orders(cfg: dict):
    abbr = cfg["measurement"]["abbreviation"]
    unit_label = cfg["measurement"]["label"]

    col_primary, col_secondary = st.columns([1.1, 1])

    # ── Left: FeneVision primary import ──
    with col_primary:
        st.markdown(
            "<div style='font-size:11px;font-weight:800;text-transform:uppercase;"
            "letter-spacing:.7px;color:#1a7cb8;margin-bottom:4px;'>📊 FeneVision Export</div>",
            unsafe_allow_html=True,
        )
        st.caption("GA Trucks .xlsx — 'Orders by Route' sheet. Aggregates line items → stops automatically.")
        fv_file = st.file_uploader(
            "Choose FeneVision xlsx",
            type=["xlsx"],
            key=f"fv_uploader_{st.session_state.uploader_key}",
            label_visibility="collapsed",
        )
        if fv_file is not None:
            _routing_cfg = cfg.get("routing", {})
            exclude_patterns = _routing_cfg.get("exclude_route_patterns", ["Lindsay MO"])
            min_sqft_import = float(_routing_cfg.get("min_sqft_threshold", 5.0))

            import openpyxl
            _wb = openpyxl.load_workbook(fv_file, read_only=True, data_only=True)
            _sheet_names = _wb.sheetnames
            _wb.close()
            fv_file.seek(0)

            _DEFAULT_SHEET = "Orders by Route"
            _sheet_candidates = [s for s in _sheet_names if "order" in s.lower() or "route" in s.lower()]
            if _DEFAULT_SHEET in _sheet_names:
                _chosen_sheet = _DEFAULT_SHEET
            elif len(_sheet_candidates) == 1:
                _chosen_sheet = _sheet_candidates[0]
            else:
                _chosen_sheet = st.selectbox(
                    f"Sheet not found: '{_DEFAULT_SHEET}'. Pick the orders sheet:",
                    options=_sheet_names,
                    index=0,
                    key="fv_sheet_picker",
                )
                if not st.button("Load selected sheet", key="fv_sheet_confirm"):
                    st.stop()

            try:
                with st.spinner("Reading FeneVision file…"):
                    new_orders, skipped, excluded = import_fenevision_xlsx(
                        fv_file,
                        sheet_name=_chosen_sheet,
                        exclude_route_patterns=exclude_patterns,
                        min_sqft=min_sqft_import,
                    )
            except Exception as e:
                st.error(f"Could not read FeneVision file: {e}")
            else:
                filter_notes = []
                if excluded:
                    filter_notes.append(f"{len(excluded)} interplant route(s) excluded")
                if skipped:
                    filter_notes.append(f"{len(skipped)} placeholder stop(s) skipped")

                if new_orders:
                    st.session_state.orders = new_orders
                    st.session_state.assignments = []
                    st.session_state.dropped = []
                    st.session_state.uploader_key += 1
                    st.session_state.auto_run_pending = True
                    st.session_state.fv_filename = fv_file.name
                    st.session_state._jump_plan = True
                    try:
                        fv_file.seek(0)
                        st.session_state.supervisor_routes = parse_route_truck_summary(
                            fv_file, exclude_route_patterns=exclude_patterns
                        )
                    except Exception:
                        st.session_state.supervisor_routes = []
                    if GEOCODING_AVAILABLE:
                        st.session_state.auto_geocode_pending = True
                    msg = f"✅ {len(new_orders)} stops loaded"
                    if filter_notes:
                        msg += " · " + " · ".join(filter_notes)
                    msg += " — heading to Load Plan to optimize…"
                    st.success(msg)
                    st.rerun()
                else:
                    if filter_notes:
                        body = "all stops were filtered out (" + " · ".join(filter_notes) + ")"
                    else:
                        body = "the file contained no data rows"
                    st.warning(
                        f"No orders imported — {body}. "
                        "Check exclusion patterns (Import Filters in sidebar) or the sqft threshold."
                    )

    # ── Right: CSV + English tabs ──
    with col_secondary:
        st.markdown(
            "<div style='font-size:11px;font-weight:800;text-transform:uppercase;"
            "letter-spacing:.7px;color:#555;margin-bottom:4px;'>Other Import Methods</div>",
            unsafe_allow_html=True,
        )
        _tab_csv, _tab_eng = st.tabs(["CSV Upload", "Add in English"])

        with _tab_csv:
            st.caption(
                f"Required: `order_id`, `customer_name`, `address`, `capacity_units` ({abbr})"
                f" — optional: `priority`, `notes`"
            )
            st.caption(
                "`customer_name` = business name · `address` = full shipping address (street, city, state, zip)"
            )
            uploaded = st.file_uploader(
                "Choose CSV",
                type=["csv"],
                key=f"uploader_{st.session_state.uploader_key}",
                label_visibility="collapsed",
            )
            if uploaded:
                df = pd.read_csv(uploaded)
                required = {"order_id", "customer_name", "address", "capacity_units"}
                missing = required - set(df.columns)
                if missing:
                    st.error(f"CSV missing columns: {missing}")
                else:
                    new_orders = [
                        Order(
                            order_id=str(row["order_id"]),
                            customer_name=str(row["customer_name"]),
                            address=str(row["address"]),
                            capacity_units=float(row["capacity_units"]),
                            priority=0 if pd.isna(row.get("priority", 0)) else int(row.get("priority", 0)),
                            notes="" if pd.isna(row.get("notes", "")) else str(row.get("notes", "")),
                        )
                        for _, row in df.iterrows()
                    ]
                    st.session_state.orders.extend(new_orders)
                    st.session_state.assignments = []
                    st.session_state.dropped = []
                    st.session_state.uploader_key += 1
                    st.session_state.auto_run_pending = True
                    st.rerun()

        with _tab_eng:
            has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
            if not has_key:
                st.caption("ANTHROPIC_API_KEY not set — add to .env to enable. CSV + optimizer work without it.")
            st.caption(
                f"Describe a stop in plain English — for any location, include the **business name** "
                f"and **full shipping address** (street, city, state, zip)."
            )
            st.caption(f'e.g. _"Riverside Homes, 450 River Rd Macon GA 31201, 48 {abbr}, gate code 1234"_')
            col_input, col_btn = st.columns([4, 1])
            nl_text = col_input.text_input(
                "Order description",
                placeholder="Describe the stop…",
                label_visibility="collapsed",
                disabled=not has_key,
            )
            parse_clicked = col_btn.button("Parse", disabled=not has_key, use_container_width=True)

            if parse_clicked and nl_text:
                with st.spinner("Parsing + verifying…"):
                    try:
                        parsed, verif = parse_and_verify(nl_text, unit_label)
                        st.session_state.pending = {"raw": nl_text, "parsed": parsed, "verif": verif}
                    except Exception as e:
                        st.error(f"Parse error: {e}")

    # ── Verification card (full-width, below columns) ──
    if st.session_state.pending:
        p = st.session_state.pending
        verif = p["verif"]
        parsed = p["parsed"]

        st.divider()
        st.markdown("**Verification Agent**")
        if verif.get("confident", False):
            st.success(verif.get("summary", "Looks good."))
        else:
            st.warning(verif.get("summary", "Review required."))
            for issue in verif.get("issues", []):
                st.caption(f"⚠ {issue}")

        c1, c2 = st.columns(2)
        oid   = c1.text_input("Order ID",              value=str(parsed.get("order_id", "")),      key="p_id")
        cname = c2.text_input("Customer Name",          value=str(parsed.get("customer_name", "")), key="p_name")
        addr  = c1.text_input("Ship-To Address",        value=str(parsed.get("address", "")),        key="p_addr")
        cap   = c2.number_input(f"Floor Space ({abbr})", value=float(parsed.get("capacity_units", 0)), min_value=0.0, key="p_cap")
        pri   = c1.number_input("Priority (0=normal, 10=urgent)", value=int(parsed.get("priority", 0)), min_value=0, max_value=10, key="p_pri")
        notes = c2.text_input("Notes (dock info, gate codes…)", value=str(parsed.get("notes", "")), key="p_notes")

        btn_ok, btn_no = st.columns(2)
        if btn_ok.button("✓ Confirm & Add", type="primary", use_container_width=True):
            st.session_state.orders.append(Order(
                order_id=oid, customer_name=cname, address=addr,
                capacity_units=cap, priority=pri, notes=notes,
            ))
            st.session_state.pending = None
            st.session_state.assignments = []
            st.session_state.dropped = []
            st.rerun()
        if btn_no.button("✗ Discard", use_container_width=True):
            st.session_state.pending = None
            st.rerun()

    # ── Current orders table (full-width) ──
    st.divider()
    st.subheader(f"Current Orders ({len(st.session_state.orders)})")
    if not st.session_state.orders:
        st.caption("No orders yet.")
        return

    _n_builders = sum(1 for o in st.session_state.orders if _order_category(o) == "builder")
    _n_dist = len(st.session_state.orders) - _n_builders
    if _n_builders and _n_dist:
        st.caption(f"**{_n_dist} distribution** stops (trailers OK) · **{_n_builders} builder** stops (straight trucks only)")

    rows = [
        {
            "FeneVision #": getattr(o, "fenevision_ids", None) or o.order_id,
            "Customer": o.customer_name,
            "Ship-To Address": o.address,
            f"Floor Space ({abbr})": o.capacity_units,
            "Category": "Builder" if _order_category(o) == "builder" else "Distribution",
            "Truck": (", ".join(getattr(o, "allowed_truck_types", None) or [])) or "any",
            "Notes": o.notes,
        }
        for o in st.session_state.orders
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if st.button("🗑 Clear All Orders"):
        st.session_state.orders = []
        st.session_state.assignments = []
        st.session_state.dropped = []
        st.rerun()


def _order_category(order) -> str:
    """'builder' if straight-truck-only, else 'distribution'."""
    if order.allowed_truck_types and set(order.allowed_truck_types) == {"straight"}:
        return "builder"
    return "distribution"


_ADDR_PLACEHOLDERS = {"none", "n/a", "tbd", "unknown", "na", ""}
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS",
    "KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
    "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV",
    "WI","WY","DC",
}
import re as _re
_WORD_RE = _re.compile(r'\b[A-Za-z]{2}\b')

def _has_state(addr: str) -> bool:
    return any(w.upper() in _US_STATES for w in _WORD_RE.findall(addr))

def _flag_address_issues(assignments):
    """Return list of (truck_name, stop_num, customer, address, issue) for suspect addresses."""
    issues = []
    for a in assignments:
        for stop in a.stops:
            addr = (stop.order.address or "").strip()
            low = addr.lower()
            if not addr or low in _ADDR_PLACEHOLDERS:
                issues.append((a.truck.name, stop.stop_number, stop.order.customer_name, addr, "Blank or placeholder address"))
            elif len(addr) < 15:
                issues.append((a.truck.name, stop.stop_number, stop.order.customer_name, addr, "Address too short — may be incomplete"))
            elif not any(ch.isdigit() for ch in addr):
                issues.append((a.truck.name, stop.stop_number, stop.order.customer_name, addr, "No house number — may be a street-level or new construction address, verify before printing"))
            elif not _has_state(addr):
                issues.append((a.truck.name, stop.stop_number, stop.order.customer_name, addr, "No US state detected (e.g. GA, ga)"))
    return issues


def _classify_drop(order, trucks, max_route_hours, stop_time_minutes, depot_lat, depot_lon, straight_speed_mph):
    """Post-solve heuristic: guess why this order was dropped.

    Returns: 'multiday' | 'homebuilder' | 'over_capacity' | 'capacity'

    Limitation: only catches formal allowed_truck_types flag — not area knowledge
    (weight-restricted roads, narrow streets, HOA access) Joseph carries in his head.
    Compound solver drops may be misclassified.
    """
    if order.allowed_truck_types == ["straight"]:
        return "homebuilder"

    if order.lat is not None and depot_lat is not None:
        lat1, lon1, lat2, lon2 = map(math.radians, [depot_lat, depot_lon, order.lat, order.lon])
        a = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
        dist_mi = 3958.8 * 2 * math.asin(math.sqrt(a))
        if (2 * dist_mi / straight_speed_mph) + (stop_time_minutes / 60) > max_route_hours:
            return "multiday"

    if order.capacity_units > max((t.max_capacity for t in trucks), default=0):
        return "over_capacity"

    return "capacity"


def render_load_plan(cfg: dict):
    abbr = cfg["measurement"]["abbreviation"]
    routing_cfg = cfg.get("routing", {})
    max_route_hours = float(routing_cfg.get("max_route_hours", 9.0))
    stop_time_minutes = float(routing_cfg.get("stop_time_minutes", 45.0))
    straight_speed_mph = float(routing_cfg.get("straight_speed_mph", 47.0))
    trailer_speed_mph = float(routing_cfg.get("trailer_speed_mph", 40.0))
    max_fill_pct = float(routing_cfg.get("max_fill_pct", 90)) / 100.0
    manual_truck_count = int(routing_cfg.get("manual_truck_count", 13))
    solver_time_limit = int(routing_cfg.get("solver_time_limit_seconds", 15))
    osrm_server = routing_cfg.get("osrm_server", "")

    trucks = [
        Truck(
            name=t["name"],
            truck_type=t["type"],
            max_capacity=t["max_capacity"] * max_fill_pct,
            fixed_cost=t.get("fixed_cost", 5.0),
            cost_per_mile=t.get("cost_per_mile", 0.0),
            driver=t.get("driver", ""),
            employment_type=t.get("employment_type", "fulltime"),
        )
        for t in cfg["trucks"]
        if t.get("active", True)  # skip trucks unchecked in sidebar
    ]

    if not st.session_state.orders:
        st.info("Add orders in the 'Add Orders' tab first.")
        return

    _fname = st.session_state.get("fv_filename")
    if _fname:
        st.markdown(
            f"<div style='background:#fff;border:1px solid #dde3ea;border-radius:8px;"
            f"padding:9px 14px;margin-bottom:12px;display:flex;align-items:center;gap:10px;'>"
            f"<span style='font-size:15px;'>📊</span>"
            f"<div style='flex:1;'>"
            f"<span style='font-size:12px;font-weight:800;color:#1a1a2e;'>{_fname}</span>"
            f"<span style='font-size:11px;color:#888;margin-left:8px;'>"
            f"{len(st.session_state.orders)} stops loaded</span>"
            f"</div>"
            f"<span style='font-size:10px;font-weight:800;background:#1a1a2e;color:#F58220;"
            f"padding:3px 9px;border-radius:8px;cursor:default;"
            f"' title='FeneVision live feed — roadmap item'>⚡ Live Feed →</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    depot_addr = cfg["depot"].get("address", "")
    depot_coords = (33.749, -84.388)

    # ── Routes-on-top: compact summary when a plan already exists ──
    if st.session_state.assignments and not st.session_state.get("auto_run_pending"):
        _asgn_top = st.session_state.assignments
        _n_assigned = sum(len(a.stops) for a in _asgn_top)
        _n_dropped  = len(st.session_state.dropped)
        _cards_html = ""
        for _a in _asgn_top:
            _rth = getattr(_a, "route_time_hours", 0.0) or 0.0
            _pct = _a.utilization_pct
            _bar_color = "#4caf50" if _pct < 80 else ("#f59e0b" if _pct < 95 else "#ef4444")
            _pill_bg   = "#e8f5e9" if _pct < 80 else ("#fff8e1" if _pct < 95 else "#fdeaea")
            _pill_fg   = "#2e7d32" if _pct < 80 else ("#b45309" if _pct < 95 else "#c62828")
            _dist = f" · {_a.route_distance_miles:.0f} mi" if _a.route_distance_miles else ""
            _time = f" · ~{_rth:.1f} hr" if _rth else ""
            _icon = "🚚" if _a.truck.truck_type == "trailer" else "🚛"
            _stops_preview = " · ".join(s.order.customer_name for s in _a.stops[:3])
            if len(_a.stops) > 3:
                _stops_preview += f" · +{len(_a.stops)-3} more"
            _cards_html += (
                f"<div style='background:#fff;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,.08);"
                f"margin-bottom:6px;overflow:hidden;'>"
                f"<div style='padding:8px 12px;display:flex;align-items:center;gap:8px;"
                f"border-bottom:1px solid #f0f2f5;'>"
                f"<span style='font-size:28px;line-height:1;'>{_icon}</span>"
                f"<div style='flex:1;font-size:14px;font-weight:800;color:#1a1a2e;'>"
                f"{_a.truck.name}{(' — ' + _a.truck.driver) if _a.truck.driver else ''}</div>"
                f"<div style='font-size:12px;font-weight:800;background:{_pill_bg};color:{_pill_fg};"
                f"padding:3px 10px;border-radius:8px;'>{_pct:.0f}%{_dist}{_time}</div>"
                f"</div>"
                f"<div style='height:4px;background:#f0f2f5;'>"
                f"<div style='width:{min(_pct,100):.0f}%;height:4px;background:{_bar_color};'></div></div>"
                f"<div style='padding:6px 14px;font-size:12px;color:#666;'>{_stops_preview}</div>"
                f"</div>"
            )
        _drop_note = (
            f"<span style='color:#c62828;font-weight:700;'> · {_n_dropped} dropped</span>"
            if _n_dropped else ""
        )
        st.markdown(
            f"<div style='margin-bottom:4px;'>"
            f"<div style='font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.6px;"
            f"color:#1a7cb8;margin-bottom:8px;'>✅ Optimized Routes — "
            f"{len(_asgn_top)} truck(s) · {_n_assigned} stops{_drop_note}</div>"
            f"{_cards_html}</div>",
            unsafe_allow_html=True,
        )
        _reo_col, _info_col = st.columns([1, 2])
        if _reo_col.button("🔄 Re-Optimize", use_container_width=True):
            st.session_state.auto_run_pending = True
            st.rerun()
        _info_col.caption("↓ Full route cards with reorder controls below")
        st.divider()

    total_needed = sum(o.capacity_units for o in st.session_state.orders)
    fleet_cap = sum(t.max_capacity for t in trucks)
    fleet_util_pct = (total_needed / fleet_cap * 100) if fleet_cap else 0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Stops to Deliver", len(st.session_state.orders))
    m2.metric(f"Space Required ({abbr})", f"{total_needed:.0f}")
    m3.metric(f"Fleet Capacity ({abbr})", f"{fleet_cap:.0f}", help="Total across all available trucks at current fill cap")
    m4.metric("Fleet Load", f"{fleet_util_pct:.0f}%",
              delta="Within capacity" if fleet_util_pct <= 100 else f"Over by {fleet_util_pct-100:.0f}%",
              delta_color="normal" if fleet_util_pct <= 100 else "inverse")

    if total_needed > fleet_cap:
        st.error(f"Orders exceed fleet capacity by {total_needed - fleet_cap:.0f} {abbr}. Add trucks or remove orders.")

    with st.expander("🔍 Order data summary (troubleshooting)", expanded=False):
        _orders = st.session_state.orders
        if _orders:
            _caps = [o.capacity_units for o in _orders]
            _restricted = sum(1 for o in _orders if o.allowed_truck_types)
            st.caption(
                f"**{len(_caps)} stops** — min {min(_caps):.1f} {abbr}, "
                f"max {max(_caps):.1f} {abbr}, avg {sum(_caps)/len(_caps):.1f} {abbr}  \n"
                f"Straight-truck only (homebuilder): **{_restricted}** stops  \n"
                f"Stop time per stop: **{stop_time_minutes:.0f} min** → max stops per truck from time alone: "
                f"**{int(max_route_hours * 60 / stop_time_minutes)}**  \n"
                f"Straight truck fill cap: **{208 * max_fill_pct:.0f} {abbr}** "
                f"· Trailer fill cap: **{431.06 * max_fill_pct:.0f} {abbr}**"
            )
            if max(_caps) > 208:
                st.warning(
                    f"Largest stop ({max(_caps):.0f} {abbr}) exceeds a straight truck's full capacity (208 {abbr}). "
                    "It must go on a trailer. If you have no trailers, it will be dropped."
                )
            if sum(_caps) / len(_caps) < 5:
                st.warning(
                    f"Average stop size is {sum(_caps)/len(_caps):.1f} {abbr} — unusually small. "
                    "Check that FeneVision sqftShippedQty values are in square feet, not square inches or another unit."
                )
        else:
            st.caption("No orders loaded yet.")

    _already_geocoded = sum(1 for o in st.session_state.orders if o.lat is not None)
    _needs_geocode = len(st.session_state.orders) - _already_geocoded
    with st.expander("⚙️ Distance routing", expanded=False):
        if _already_geocoded > 0:
            st.caption(
                f"{_already_geocoded}/{len(st.session_state.orders)} stops already geocoded at import time. "
                f"Distance routing uses those coordinates for route sequencing and time estimates. "
                f"{'Remaining ' + str(_needs_geocode) + ' stop(s) will be geocoded now.' if _needs_geocode else ''}"
            )
        else:
            st.caption(
                "Enable to add real distance-based sequencing and drive-time estimates "
                "(Haversine straight-line, ~5 sec per stop). "
                "Swap for OSRM or Google Maps Distance Matrix API when you're ready for true road distances."
            )
        geocode_on = st.checkbox(
            "Use geocoding for distance-based routing and time estimates",
            value=GEOCODING_AVAILABLE,
            disabled=not GEOCODING_AVAILABLE,
            help="Install geopy to enable: pip install geopy" if not GEOCODING_AVAILABLE else "",
        )

    if st.session_state.get("auto_geocode_pending") and GEOCODING_AVAILABLE and st.session_state.orders:
        st.session_state.auto_geocode_pending = False
        _n_to_geo = sum(1 for o in st.session_state.orders if o.lat is None)
        with st.spinner(f"Geocoding {_n_to_geo} address(es)… then optimizing routes"):
            _dep_addr_geo = cfg["depot"].get("address", "")
            if _dep_addr_geo:
                _dc_geo = geocode_address(_dep_addr_geo)
                if _dc_geo:
                    st.session_state._depot_coords = _dc_geo
            for _o in st.session_state.orders:
                if _o.lat is None:
                    _c = geocode_address(_o.address)
                    if _c:
                        _o.lat, _o.lon = _c

    if st.session_state.get("auto_run_pending") and st.session_state.orders:
        orders_copy = copy.deepcopy(st.session_state.orders)
        errors = validate_inputs(orders_copy, trucks)
        if not errors:
            st.session_state.auto_run_pending = False
            st.session_state.dismiss_packing_issues = False
            effective_max_hours = st.session_state.pop("overnight_cap", max_route_hours)
            anim = st.empty()
            anim.markdown(_SOLVE_ANIMATION_HTML, unsafe_allow_html=True)
            time.sleep(1.0)  # let tab-switch JS fire before solver blocks the thread
            _auto_depot = st.session_state.get("_depot_coords") or depot_coords
            assignments, dropped = solve(
                orders_copy, trucks, _auto_depot,
                max_route_hours=effective_max_hours,
                stop_time_minutes=stop_time_minutes,
                straight_speed_mph=straight_speed_mph,
                trailer_speed_mph=trailer_speed_mph,
                solver_time_limit=solver_time_limit,
                osrm_server=osrm_server,
            )
            st.session_state.assignments = assignments
            st.session_state.dropped = dropped
            anim.markdown(_SOLVE_DONE_HTML, unsafe_allow_html=True)
        else:
            st.session_state.auto_run_pending = False
            for e in errors:
                st.error(e)

    # --- Routing mode selector ---
    _orders_all = st.session_state.orders
    _n_builders = sum(1 for o in _orders_all if _order_category(o) == "builder")
    _n_dist = len(_orders_all) - _n_builders
    _show_phase_mode = _n_builders > 0 and _n_dist > 0

    if _show_phase_mode:
        routing_mode = st.radio(
            "Routing mode",
            ["All Orders", "Two-Phase (Distribution first, then Builders)"],
            horizontal=True,
            help=(
                "Two-Phase matches the supervisor's workflow: finalize distribution routes "
                "(trailers — lumber yards, building supply) the evening before, then slot "
                "builder routes (straight trucks — residential) in at 4am day-of."
            ),
        )
    else:
        routing_mode = "All Orders"

    def _geocode_and_warn(orders_to_geo):
        dc = st.session_state.pop("_depot_coords", None) or depot_coords
        if geocode_on:
            _ungeocoded = [o for o in orders_to_geo if o.lat is None]
            if _ungeocoded:
                with st.spinner(f"Geocoding {len(_ungeocoded)} remaining address(es)…"):
                    if dc == depot_coords and depot_addr:
                        r = geocode_address(depot_addr)
                        if r:
                            dc = r
                    for order in _ungeocoded:
                        c = geocode_address(order.address)
                        if c:
                            order.lat, order.lon = c
            elif dc == depot_coords and depot_addr:
                r = geocode_address(depot_addr)
                if r:
                    dc = r
            _warns = check_route_cap(
                orders_to_geo, dc,
                max_route_hours=max_route_hours,
                stop_time_minutes=stop_time_minutes,
                straight_speed_mph=straight_speed_mph,
                trailer_speed_mph=trailer_speed_mph,
            )
            for w in _warns:
                st.warning(f"Multi-day route flag: {w}")
        return dc

    def _run_solve(orders_to_solve, dc):
        anim = st.empty()
        anim.markdown(_SOLVE_ANIMATION_HTML, unsafe_allow_html=True)
        asgn, drp = solve(
            orders_to_solve, trucks, dc,
            max_route_hours=max_route_hours,
            stop_time_minutes=stop_time_minutes,
            straight_speed_mph=straight_speed_mph,
            trailer_speed_mph=trailer_speed_mph,
            solver_time_limit=solver_time_limit,
            osrm_server=osrm_server,
        )
        anim.markdown(_SOLVE_DONE_HTML, unsafe_allow_html=True)
        return asgn, drp

    if routing_mode == "All Orders":
        btn_label = "🔄 Regenerate Load Plan" if st.session_state.assignments else "Generate Load Plan"
        if st.button(btn_label, type="primary"):
            orders_copy = copy.deepcopy(_orders_all)
            errors = validate_inputs(orders_copy, trucks)
            if errors:
                for e in errors:
                    st.error(e)
                return
            dc = _geocode_and_warn(orders_copy)
            st.session_state.dismiss_packing_issues = False
            st.session_state.routing_phase = None
            asgn, drp = _run_solve(orders_copy, dc)
            st.session_state.assignments = asgn
            st.session_state.dropped = drp

    else:  # Two-Phase
        _phase = st.session_state.routing_phase
        _ph1_done = _phase == "distribution_done"

        ph1_col, ph2_col, reset_col = st.columns([2, 2, 1])

        with ph1_col:
            _ph1_btn = "✅ Distribution routes locked" if _ph1_done else f"📦 Optimize Distribution ({_n_dist} stops)"
            if st.button(_ph1_btn, type="secondary" if _ph1_done else "primary", disabled=_ph1_done):
                dist_orders = copy.deepcopy([o for o in _orders_all if _order_category(o) == "distribution"])
                errors = validate_inputs(dist_orders, trucks)
                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    dc = _geocode_and_warn(dist_orders)
                    st.session_state.dismiss_packing_issues = False
                    asgn, drp = _run_solve(dist_orders, dc)
                    st.session_state.assignments = asgn
                    st.session_state.dropped = drp
                    st.session_state.routing_phase = "distribution_done"
                    st.rerun()

        with ph2_col:
            if st.button(
                f"🏠 Add Builder Routes ({_n_builders} stops)",
                type="primary" if _ph1_done else "secondary",
                disabled=not _ph1_done,
                help="" if _ph1_done else "Complete Phase 1 first.",
            ):
                builder_orders = copy.deepcopy([o for o in _orders_all if _order_category(o) == "builder"])
                errors = validate_inputs(builder_orders, trucks)
                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    dc = _geocode_and_warn(builder_orders)
                    b_asgn, b_drp = _run_solve(builder_orders, dc)
                    st.session_state.assignments = st.session_state.assignments + b_asgn
                    st.session_state.dropped = st.session_state.dropped + b_drp
                    st.session_state.routing_phase = None
                    st.rerun()

        with reset_col:
            if st.button("↺ Reset", help="Clear plan and restart"):
                st.session_state.assignments = []
                st.session_state.dropped = []
                st.session_state.routing_phase = None
                st.rerun()

        if _ph1_done:
            st.info(
                f"**Phase 1 complete** — {len(st.session_state.assignments)} distribution truck(s) locked. "
                f"Click **Add Builder Routes** to slot in {_n_builders} builder stop(s)."
            )

    if not st.session_state.assignments and not st.session_state.dropped:
        return

    if st.session_state.assignments:
        depot_name = cfg["depot"].get("name", "Lindsay Windows")
        html_str = generate_html_routes(
            st.session_state.assignments,
            depot_name=depot_name,
            date_str=datetime.date.today().isoformat(),
        )
        col_banner, col_dl = st.columns([3, 1])
        if st.session_state.dropped:
            col_banner.warning(
                f"⚠ Plan ready — {len(st.session_state.assignments)} truck(s) assigned, "
                f"but {len(st.session_state.dropped)} order(s) could not be placed. See details below."
            )
        else:
            col_banner.success("✅ Optimized plan ready — export route sheets for drivers below.")
        col_dl.download_button(
            "⬇ Export Route Sheets (HTML)",
            data=html_str.encode("utf-8"),
            file_name="route_sheets.html",
            mime="text/html",
            key="banner_html_download",
        )

        _asgn = st.session_state.assignments
        _total_stops = sum(len(a.stops) for a in _asgn)
        _avg_util = sum(a.utilization_pct for a in _asgn) / len(_asgn) if _asgn else 0
        _total_miles = sum(a.route_distance_miles for a in _asgn if a.route_distance_miles)
        _trucks_saved = manual_truck_count - len(_asgn)
        _pct_saved = round(_trucks_saved / manual_truck_count * 100) if manual_truck_count else 0
        _avg_time = sum(getattr(a, 'route_time_hours', 0.0) for a in _asgn) / len(_asgn) if _asgn else 0

        # Optimizer vs Unoptimized comparison block
        _c_opt, _c_mid, _c_man = st.columns([5, 3, 5])
        with _c_opt:
            st.markdown(
                f"<div style='background:#e8f5e9;border-radius:10px;padding:16px 20px;border:1px solid #a5d6a7'>"
                f"<div style='font-size:0.75rem;font-weight:600;color:#2e7d32;letter-spacing:1px;text-transform:uppercase'>Optimizer</div>"
                f"<div style='font-size:2.6rem;font-weight:800;color:#1b5e20;line-height:1.1'>{len(_asgn)}</div>"
                f"<div style='font-size:0.95rem;color:#388e3c'>trucks used · {_avg_util:.0f}% avg fill</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with _c_mid:
            if _trucks_saved > 0:
                st.markdown(
                    f"<div style='text-align:center;padding:12px 0'>"
                    f"<div style='font-size:2rem;font-weight:800;color:#1a5fa8'>↓ {_trucks_saved}</div>"
                    f"<div style='font-size:0.85rem;color:#555;font-weight:600'>{_pct_saved}% fewer trucks</div>"
                    f"<div style='font-size:0.75rem;color:#888;margin-top:4px'>vs unoptimized</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    "<div style='text-align:center;padding:20px 0;color:#888;font-size:1.2rem'>vs</div>",
                    unsafe_allow_html=True,
                )
        with _c_man:
            st.markdown(
                f"<div style='background:#fafafa;border-radius:10px;padding:16px 20px;border:1px solid #e0e0e0'>"
                f"<div style='font-size:0.75rem;font-weight:600;color:#888;letter-spacing:1px;text-transform:uppercase'>Unoptimized (manual)</div>"
                f"<div style='font-size:2.6rem;font-weight:800;color:#555;line-height:1.1'>{manual_truck_count}</div>"
                f"<div style='font-size:0.95rem;color:#999'>trucks used · no optimization</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='margin-top:12px'></div>", unsafe_allow_html=True)
        _s1, _s2, _s3, _s4 = st.columns(4)
        _s1.metric("Stops Covered", _total_stops)
        _s2.metric("Avg Truck Fill", f"{_avg_util:.0f}%")
        _s3.metric("Avg Route Time", f"{_avg_time:.1f} hr" if _avg_time else "—")
        _s4.metric("Est. Total Miles", f"{_total_miles:.0f}" if _total_miles else "—")

        # Bay width limits per truck type (inches). Windows wider than this can't physically load.
        _BAY_WIDTH = {"straight": 96.0, "trailer": 99.0}
        if not st.session_state.get("dismiss_packing_issues"):
            packing_issues = []
            for a in st.session_state.assignments:
                bay = _BAY_WIDTH.get(a.truck.truck_type, 96.0)
                for stop in a.stops:
                    w = getattr(stop.order, 'max_window_width_inches', None)
                    if w is not None and w > bay:
                        packing_issues.append((
                            a.truck.name, stop.stop_number,
                            stop.order.customer_name, stop.order.order_id, w, bay,
                        ))
            if packing_issues:
                with st.expander(
                    f"⚠️ {len(packing_issues)} stop(s) have windows wider than the assigned truck bay",
                    expanded=True,
                ):
                    st.caption(
                        "These windows may not physically load into the assigned truck. "
                        "Printing is NOT blocked — route sheets can still be exported. "
                        "Verify the window width in FeneVision or re-assign to a wider truck."
                    )
                    for truck_name, stop_num, customer, order_id, w, bay in packing_issues:
                        st.warning(
                            f"**{truck_name} → Stop {stop_num} — {customer}** (`{order_id}`)  \n"
                            f"Max window width {w:.0f}\" exceeds truck bay {bay:.0f}\""
                        )
                    _pc1, _, _pc3 = st.columns([2, 2, 1])
                    if _pc1.button("Remove flagged stops from plan", type="secondary"):
                        flagged_ids = {r[3] for r in packing_issues}
                        for a in st.session_state.assignments:
                            a.stops = [s for s in a.stops if s.order.order_id not in flagged_ids]
                        st.session_state.assignments = [a for a in st.session_state.assignments if a.stops]
                        st.rerun()
                    if _pc3.button("Dismiss ✕"):
                        st.session_state.dismiss_packing_issues = True
                        st.rerun()

        addr_issues = _flag_address_issues(st.session_state.assignments)
        if addr_issues:
            with st.expander(
                f"⚠️ {len(addr_issues)} address(es) flagged for review — verify before printing",
                expanded=True,
            ):
                st.caption(
                    "These stops have addresses that may not route correctly in Google/Apple Maps. "
                    "Check with the route coordinator before handing sheets to drivers."
                )
                for truck_name, stop_num, customer, addr, issue in addr_issues:
                    st.warning(
                        f"**{truck_name} → Stop {stop_num} — {customer}**  \n"
                        f"`{addr or '(blank)'}` — {issue}"
                    )

    if st.session_state.dropped:
        _dc = st.session_state.get("_depot_coords") or depot_coords
        _dc_lat, _dc_lon = _dc

        _cls = {
            o.order_id: _classify_drop(
                o, trucks, max_route_hours, stop_time_minutes,
                _dc_lat, _dc_lon, straight_speed_mph,
            )
            for o in st.session_state.dropped
        }
        _multiday    = [o for o in st.session_state.dropped if _cls[o.order_id] == "multiday"]
        _homebuilder = [o for o in st.session_state.dropped if _cls[o.order_id] == "homebuilder"]
        _true_drops  = [o for o in st.session_state.dropped if _cls[o.order_id] in ("capacity", "over_capacity")]

        st.markdown(f"**⚠ {len(st.session_state.dropped)} order(s) could not be assigned as same-day routes:**")

        if _multiday:
            with st.expander(
                f"🗓 {len(_multiday)} Multi-Day Route{'s' if len(_multiday) > 1 else ''} — "
                f"geographic distance exceeds {max_route_hours:.0f}-hr daily cap",
                expanded=True,
            ):
                _ids = ", ".join(o.order_id for o in _multiday)
                st.markdown(
                    f"<div style='background:#fff8e1;border:1.5px solid #f0c040;border-radius:6px;"
                    f"padding:10px 14px;color:#7a5700;font-size:13px;margin-bottom:8px;'>"
                    f"<strong>Stops:</strong> {_ids}<br>"
                    f"Fit by capacity — drive time alone exceeds the {max_route_hours:.0f}-hr round-trip cap. "
                    f"Schedule as overnight or multi-day runs.</div>",
                    unsafe_allow_html=True,
                )
                _opt1, _opt2, _opt3 = st.columns(3)
                if _opt1.button(
                    "🌙 Plan Multi-Day Routes Separately",
                    help="Solves just these stops with a 24-hr cap. Keeps same-day plan intact.",
                    use_container_width=True,
                ):
                    _md_orders = copy.deepcopy(_multiday)
                    _md_asgn, _md_drp = solve(
                        _md_orders, trucks,
                        st.session_state.get("_depot_coords") or depot_coords,
                        max_route_hours=24.0,
                        stop_time_minutes=stop_time_minutes,
                        straight_speed_mph=straight_speed_mph,
                        trailer_speed_mph=trailer_speed_mph,
                        solver_time_limit=solver_time_limit,
                        osrm_server=osrm_server,
                    )
                    st.session_state.multiday_assignments = _md_asgn
                    st.session_state.multiday_dropped = _md_drp
                    _md_ids = {o.order_id for o in _multiday}
                    st.session_state.dropped = [o for o in st.session_state.dropped if o.order_id not in _md_ids]
                    st.rerun()
                if _opt2.button(
                    "🚫 Exclude & plan separately",
                    help="Removes these stops from session. Plan them as a dedicated run.",
                    use_container_width=True,
                ):
                    _md_ids = {o.order_id for o in _multiday}
                    st.session_state.orders  = [o for o in st.session_state.orders  if o.order_id not in _md_ids]
                    st.session_state.dropped = [o for o in st.session_state.dropped if o.order_id not in _md_ids]
                    st.session_state.auto_run_pending = True
                    st.rerun()
                _opt3.caption("Or: adjust **Max Route Hours** in the sidebar routing config.")

        if _homebuilder:
            _ids = ", ".join(o.order_id for o in _homebuilder)
            st.markdown(
                f"<div style='background:#fff3e0;border:1.5px solid #F58220;border-radius:6px;"
                f"padding:10px 14px;color:#7a3500;font-size:13px;margin-bottom:8px;'>"
                f"🏠 <strong>{len(_homebuilder)} Homebuilder Conflict{'s' if len(_homebuilder) > 1 else ''} "
                f"— straight-truck restricted, no room available</strong><br>"
                f"<span style='font-size:12px;'>{_ids}<br>"
                f"Flagged as homebuilder (no 53ft trailer). All straight trucks are full or at fill cap. "
                f"Add a straight truck or increase fill % in the sidebar.<br>"
                f"<em>Note: only catches FeneVision-flagged stops — not informal road restrictions "
                f"(narrow streets, bridge weight limits) known from area experience.</em></span></div>",
                unsafe_allow_html=True,
            )

        if _true_drops:
            _ids = ", ".join(o.order_id for o in _true_drops)
            st.markdown(
                f"<div style='background:#fdeaea;border:1.5px solid #e57373;border-radius:6px;"
                f"padding:10px 14px;color:#b71c1c;font-size:13px;margin-bottom:8px;'>"
                f"❌ <strong>{len(_true_drops)} True Drop{'s' if len(_true_drops) > 1 else ''} "
                f"— capacity or constraint conflict</strong><br>"
                f"<span style='font-size:12px;'>{_ids}<br>"
                f"Cannot fit on any available truck within current constraints. "
                f"Add a truck, remove these orders, or increase fill cap.</span></div>",
                unsafe_allow_html=True,
            )

    if not st.session_state.assignments:
        st.error("Optimizer found no solution. Check that fleet capacity covers total order space.")
        return

    st.divider()
    _all_asgn = st.session_state.assignments  # alias for reorder callbacks
    for v_idx, assignment in enumerate(_all_asgn):
        dist_str = f" · {assignment.route_distance_miles:.0f} mi" if assignment.route_distance_miles else ""
        _rth = getattr(assignment, 'route_time_hours', 0.0)
        time_str = f" · ~{_rth:.1f} hr" if _rth else ""
        _driver = getattr(assignment.truck, "driver", "") or ""
        _driver_str = f" · {_driver}" if _driver else ""
        _exp_icon = "🚚" if assignment.truck.truck_type == "trailer" else "🚛"
        label = (
            f"{_exp_icon} {assignment.truck.name}{_driver_str} — "
            f"{assignment.total_capacity_used:.0f}/{assignment.truck.max_capacity:.0f} {abbr} "
            f"({assignment.utilization_pct:.0f}% utilized){dist_str}{time_str}"
        )
        with st.expander(label, expanded=True):
            # ── Inline driver name assignment ──
            _d_col, _save_col, _ = st.columns([2, 1, 4])
            _new_driver = _d_col.text_input(
                "Driver", value=_driver, key=f"driver_input_{v_idx}",
                placeholder="Assign driver name",
                label_visibility="collapsed",
            )
            if _save_col.button("Assign", key=f"driver_save_{v_idx}", use_container_width=True):
                _truck_cfg = next(
                    (t for t in cfg["trucks"] if t["name"] == assignment.truck.name), None
                )
                if _truck_cfg is not None:
                    _truck_cfg["driver"] = _new_driver
                    save_config(cfg)
                    assignment.truck.driver = _new_driver
                    st.rerun()

            c1, c2 = st.columns(2)
            with c1:
                from streamlit_sortables import sort_items as _sort_items
                st.markdown("**Delivery Sequence** — drag ⠿ to reorder · ⇄ to move truck")

                # Draggable reorder (within truck)
                _sort_labels_orig = [
                    f"{stop.order.order_id}||{stop.stop_number}. {stop.order.customer_name}"
                    for stop in assignment.stops
                ]
                _sorted_labels = _sort_items(_sort_labels_orig, key=f"sort_{v_idx}")
                if _sorted_labels != _sort_labels_orig:
                    _new_ids = [lbl.split("||")[0] for lbl in _sorted_labels]
                    _id_to_stop = {s.order.order_id: s for s in _all_asgn[v_idx].stops}
                    _all_asgn[v_idx].stops = [_id_to_stop[oid] for oid in _new_ids if oid in _id_to_stop]
                    for _n, _stp in enumerate(_all_asgn[v_idx].stops, 1):
                        _stp.stop_number = _n
                    _spd = straight_speed_mph if _all_asgn[v_idx].truck.truck_type == "straight" else trailer_speed_mph
                    _all_asgn[v_idx].route_time_hours = (
                        (_all_asgn[v_idx].route_distance_miles / _spd if _all_asgn[v_idx].route_distance_miles else 0.0)
                        + len(_all_asgn[v_idx].stops) * stop_time_minutes / 60
                    )
                    st.rerun()

                # Per-stop detail + cross-truck move
                for s_idx, stop in enumerate(assignment.stops):
                    _fv_ids = getattr(stop.order, "fenevision_ids", None)
                    _is_builder = bool(stop.order.allowed_truck_types and
                                       set(stop.order.allowed_truck_types) == {"straight"})
                    _info_col, _move_col, _chk_col = st.columns([7, 2.5, 1])
                    # Move to different truck
                    with _move_col:
                        if len(_all_asgn) > 1:
                            _other_opts = {
                                f"{a.truck.name}{' · ' + a.truck.driver if a.truck.driver else ''}": i
                                for i, a in enumerate(_all_asgn) if i != v_idx
                            }
                            _dest_label = st.selectbox(
                                "→",
                                options=list(_other_opts.keys()),
                                key=f"mv_dest_{v_idx}_{s_idx}",
                                label_visibility="collapsed",
                            )
                            if st.button("⇄ Move", key=f"mv_truck_{v_idx}_{s_idx}",
                                         use_container_width=True, help="Move to selected truck"):
                                _dest_idx = _other_opts[_dest_label]
                                _moving = _all_asgn[v_idx].stops.pop(s_idx)
                                _all_asgn[_dest_idx].stops.append(_moving)
                                for _n, _stp in enumerate(_all_asgn[v_idx].stops, 1):
                                    _stp.stop_number = _n
                                for _n, _stp in enumerate(_all_asgn[_dest_idx].stops, 1):
                                    _stp.stop_number = _n
                                for _ti in [v_idx, _dest_idx]:
                                    _spd = straight_speed_mph if _all_asgn[_ti].truck.truck_type == "straight" else trailer_speed_mph
                                    _all_asgn[_ti].route_time_hours = (
                                        (_all_asgn[_ti].route_distance_miles / _spd if _all_asgn[_ti].route_distance_miles else 0.0)
                                        + len(_all_asgn[_ti].stops) * stop_time_minutes / 60
                                    )
                                st.session_state.assignments = [a for a in _all_asgn if a.stops]
                                st.rerun()
                    with _info_col:
                        _truck_tag = (
                            "<span style='font-size:9px;background:#f0f4f8;color:#555;"
                            "border-radius:8px;padding:1px 6px;font-weight:600;"
                            "margin-left:5px;vertical-align:middle;'>26ft only</span>"
                            if _is_builder else ""
                        )
                        _fv_line = (
                            f"<div style='font-size:10px;color:#1a7cb8;font-family:monospace;"
                            f"background:#eef4fb;display:inline-block;border-radius:3px;"
                            f"padding:1px 5px;margin-top:2px;'>{_fv_ids}</div>"
                            if _fv_ids else ""
                        )
                        _pri_tag = (
                            f"<span style='background:#d32f2f;color:#fff;font-size:9px;"
                            f"padding:1px 5px;border-radius:3px;margin-left:5px;'>P{stop.order.priority}</span>"
                            if stop.order.priority > 0 else ""
                        )
                        _rname = getattr(stop.order, "route_name", None)
                        _route_tag = (
                            f"<span style='font-size:9px;background:#f0f4f8;color:#1a7cb8;"
                            f"border-radius:8px;padding:1px 7px;font-weight:700;"
                            f"margin-left:5px;vertical-align:middle;border:1px solid #d0e4f5;'>"
                            f"{_rname}</span>"
                            if _rname else ""
                        )
                        st.markdown(
                            f"<span style='font-size:13px;color:#bbb;margin-right:4px;'>⠿</span>"
                            f"<span style='display:inline-block;width:20px;height:20px;"
                            f"border-radius:50%;background:#1a7cb8;color:#fff;"
                            f"font-size:10px;font-weight:800;text-align:center;line-height:20px;"
                            f"margin-right:5px;vertical-align:middle;'>{stop.stop_number}</span>"
                            f"**{stop.order.customer_name}**{_truck_tag}{_pri_tag}  \n"
                            f"<span style='color:gray;font-size:0.85em'>{stop.order.address}</span>"
                            f"{_route_tag}  \n"
                            f"{_fv_line}",
                            unsafe_allow_html=True,
                        )
                        if stop.order.notes:
                            st.caption(f"  Note: {stop.order.notes}")
                        _line_items = getattr(stop.order, "line_items", None)
                        if _line_items:
                            _prefer_cols = ["OrderNumber", "Width", "Height", "PartNo",
                                            "sqftShippedQty", "Qty", "shpQty", "ShipQty", "Quantity"]
                            _show_cols = [c for c in _prefer_cols if any(c in item for item in _line_items)]
                            if _show_cols:
                                _col_labels = {c: ("Sq Ft" if c == "sqftShippedQty" else c) for c in _show_cols}
                                with st.expander(f"📋 {len(_line_items)} line items", expanded=False):
                                    _item_rows = [{_col_labels[c]: item.get(c, "") for c in _show_cols} for item in _line_items]
                                    st.dataframe(
                                        pd.DataFrame(_item_rows),
                                        use_container_width=True,
                                        hide_index=True,
                                    )
                    with _chk_col:
                        st.checkbox(
                            "✓",
                            key=f"confirmed_{v_idx}_{stop.order.order_id}",
                            help="Mark stop as confirmed",
                        )
            with c2:
                st.markdown("**Load Sequence** *(load #1 first — loads deepest into truck)*")
                for i, stop in enumerate(assignment.load_sequence, 1):
                    _rname = getattr(stop.order, "route_name", None)
                    _route_pill = (
                        f'<span style="font-size:12px;font-weight:700;color:#1a7cb8;background:#eef4fb;'
                        f'border:1px solid #c5ddf5;border-radius:10px;padding:2px 9px;">{_html_mod.escape(_rname)}</span>'
                        if _rname else ""
                    )
                    st.markdown(
                        f'<div style="padding:6px 0;border-bottom:1px solid #f0f4f8;display:flex;align-items:center;gap:8px;">'
                        f'<span style="font-size:13px;font-weight:800;color:#555;min-width:60px;">Load {i}.</span>'
                        f'<span style="flex:1;font-size:14px;font-weight:700;color:#1a1a2e;">{_html_mod.escape(stop.order.customer_name)}</span>'
                        f'{_route_pill}'
                        f'<span style="font-size:12px;color:#888;min-width:60px;text-align:right;">{stop.order.capacity_units:.0f} {_html_mod.escape(abbr)}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

            if FOLIUM_AVAILABLE:
                stops_with_coords = [s for s in assignment.stops if s.order.lat is not None]
                if stops_with_coords:
                    all_lats = [depot_coords[0]] + [s.order.lat for s in stops_with_coords]
                    all_lons = [depot_coords[1]] + [s.order.lon for s in stops_with_coords]
                    center = [sum(all_lats) / len(all_lats), sum(all_lons) / len(all_lons)]
                    m = folium.Map(location=center, zoom_start=8)
                    folium.Marker(
                        list(depot_coords),
                        popup="Depot (Lindsay Windows)",
                        icon=folium.Icon(color="red", icon="star", prefix="fa"),
                    ).add_to(m)
                    for stop in assignment.stops:
                        if stop.order.lat is not None:
                            folium.Marker(
                                [stop.order.lat, stop.order.lon],
                                popup=f"Stop {stop.stop_number}: {stop.order.customer_name}",
                                icon=folium.DivIcon(
                                    html=(
                                        f'<div style="font-size:13px;font-weight:bold;color:white;'
                                        f'background:#1f77b4;border-radius:50%;width:26px;height:26px;'
                                        f'text-align:center;line-height:26px;border:2px solid white;">'
                                        f'{stop.stop_number}</div>'
                                    ),
                                    icon_size=(26, 26),
                                    icon_anchor=(13, 13),
                                ),
                            ).add_to(m)
                    route_pts = (
                        [list(depot_coords)]
                        + [[s.order.lat, s.order.lon] for s in assignment.stops if s.order.lat is not None]
                        + [list(depot_coords)]
                    )
                    folium.PolyLine(route_pts, color="#1f77b4", weight=2.5, opacity=0.8).add_to(m)
                    st_folium(m, width="100%", height=350, returned_objects=[])

    if st.session_state.get("multiday_assignments"):
        st.divider()
        _md_asgn_list = st.session_state.multiday_assignments
        _md_drp_list  = st.session_state.multiday_dropped
        _md_stops_total = sum(len(a.stops) for a in _md_asgn_list)
        st.markdown(
            f"<div style='background:#fff8e1;border-left:4px solid #f0c040;padding:10px 16px;"
            f"border-radius:0 6px 6px 0;margin-bottom:12px;'>"
            f"<strong>🌙 Multi-Day Routes — {len(_md_asgn_list)} truck(s) · {_md_stops_total} stops</strong><br>"
            f"<span style='font-size:12px;color:#7a5700;'>These runs exceed the 9-hr same-day cap. "
            f"Dispatch overnight or schedule as a separate next-day run.</span></div>",
            unsafe_allow_html=True,
        )
        for _md_v in _md_asgn_list:
            _md_dist = f" · {_md_v.route_distance_miles:.0f} mi" if _md_v.route_distance_miles else ""
            _md_time = f" · ~{getattr(_md_v,'route_time_hours',0.0):.1f} hr" if getattr(_md_v,'route_time_hours',0.0) else ""
            _md_label = (
                f"🌙 OVERNIGHT — {_md_v.truck.name} — "
                f"{_md_v.total_capacity_used:.0f}/{_md_v.truck.max_capacity:.0f} {abbr} "
                f"({_md_v.utilization_pct:.0f}%){_md_dist}{_md_time}"
            )
            with st.expander(_md_label, expanded=True):
                for _md_s in _md_v.stops:
                    _md_o = _md_s.order
                    st.markdown(
                        f"**{_md_s.stop_number}.** {_md_o.customer_name}  \n"
                        f"<span style='font-size:12px;color:#555;'>{_md_o.address}</span>  \n"
                        f"<span style='font-size:11px;color:#888;'>{_md_o.capacity_units:.0f} {abbr}</span>",
                        unsafe_allow_html=True,
                    )
        if _md_drp_list:
            st.warning(f"{len(_md_drp_list)} multi-day stop(s) still couldn't be assigned: "
                       + ", ".join(o.order_id for o in _md_drp_list))
        _md_html_str = generate_html_routes(
            _md_asgn_list,
            depot_name=cfg["depot"].get("name", "Lindsay Windows"),
            date_str=datetime.date.today().isoformat(),
            is_overnight=True,
        )
        st.download_button(
            "⬇ Export Multi-Day Route Sheets (HTML)",
            data=_md_html_str.encode("utf-8"),
            file_name="route_sheets_multiday.html",
            mime="text/html",
            key="multiday_html_download",
        )

    st.divider()
    if st.session_state.assignments:
        depot_name = cfg["depot"].get("name", "Lindsay Windows")
        html_str = generate_html_routes(
            st.session_state.assignments,
            depot_name=depot_name,
            date_str=datetime.date.today().isoformat(),
        )
        st.download_button(
            "⬇ Export Route Sheets (HTML)",
            data=html_str.encode("utf-8"),
            file_name="route_sheets.html",
            mime="text/html",
            key="bottom_html_download",
        )



def render_analysis(cfg: dict):
    if not st.session_state.assignments:
        st.info("Run the optimizer in the Load Plan tab first.")
        return

    assignments = st.session_state.assignments
    dropped = st.session_state.dropped
    orders = st.session_state.orders

    abbr = cfg["measurement"]["abbreviation"]
    has_miles = any(a.route_distance_miles for a in assignments)
    routing_cfg = cfg.get("routing", {})

    # --- Summary table ---
    st.subheader("Summary")
    summary_rows = []
    for a in assignments:
        miles = a.route_distance_miles if a.route_distance_miles else 0.0
        cost = miles * a.truck.cost_per_mile if miles else None
        _drv = getattr(a.truck, "driver", "") or ""
        summary_rows.append({
            "Truck": a.truck.name,
            "Driver": _drv if _drv else "—",
            "Type": a.truck.truck_type,
            "Stops": len(a.stops),
            f"Sq Ft Used": round(a.total_capacity_used, 1),
            f"Capacity ({abbr})": round(a.truck.max_capacity, 1),
            "Utilization %": round(a.utilization_pct, 1),
            "Est. Miles": round(miles, 1) if miles else "N/A",
            "Est. Cost ($)": round(cost, 2) if cost is not None else "N/A",
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True)

    # --- Utilization bar chart ---
    st.subheader("Utilization % per Truck")
    st.caption(
        "Utilization = sq ft used ÷ truck capacity. "
        "**85–95% is the target range** — high enough to avoid wasting truck space, "
        "low enough to handle real-world variation (odd-sized windows, last-minute adds). "
        "100% means zero buffer; anything that doesn't fit gets dropped. "
        "Under 70% usually means a truck could be consolidated."
    )
    chart_data = pd.DataFrame(
        {"Utilization %": [a.utilization_pct for a in assignments]},
        index=[a.truck.name for a in assignments],
    )
    st.bar_chart(chart_data)

    if not has_miles:
        st.caption("Mileage estimates require geocoding — enable in Load Plan tab.")

    st.divider()

    # --- Constraint audit ---
    st.subheader("Homebuilder Truck-Type Constraint Audit")
    st.caption(
        "Some customers — typically homebuilders in residential subdivisions — physically cannot "
        "receive a 53-ft trailer (tight driveways, low-clearance streets). These stops are flagged "
        "in FeneVision with a 26-ft truck type. The optimizer enforces this constraint so they are "
        "never assigned to a trailer. **PASS = all restricted stops landed on 26-ft straight trucks.** "
        "A FAIL here means a driver would show up with the wrong truck and couldn't deliver."
    )
    restricted = [
        (a, stop)
        for a in assignments
        for stop in a.stops
        if stop.order.allowed_truck_types
    ]
    if not restricted:
        st.caption("No homebuilder (truck-restricted) stops in current plan.")
    else:
        audit_rows = []
        fail_count = 0
        for a, stop in restricted:
            o = stop.order
            passed = a.truck.truck_type in o.allowed_truck_types
            if not passed:
                fail_count += 1
            audit_rows.append({
                "Order ID": o.order_id,
                "Customer": o.customer_name,
                "Assigned Truck": a.truck.name,
                "Truck Type": a.truck.truck_type,
                "Restriction": ", ".join(o.allowed_truck_types),
                "Result": "PASS" if passed else "FAIL",
            })
        audit_df = pd.DataFrame(audit_rows)

        def _highlight_result(row):
            color = "background-color: #ccffcc" if row["Result"] == "PASS" else "background-color: #ff9999"
            return [color] * len(row)

        st.dataframe(
            audit_df.style.apply(_highlight_result, axis=1),
            use_container_width=True,
        )
        if fail_count:
            st.error(f"{fail_count} homebuilder constraint violation(s) — check assignments above.")
        else:
            st.success(f"All {len(restricted)} homebuilder stops passed the truck-type constraint.")

    st.divider()

    # --- Cost comparison ---
    st.subheader("Cost Comparison")
    total_miles = sum(a.route_distance_miles for a in assignments if a.route_distance_miles)
    total_cost = sum(
        a.route_distance_miles * a.truck.cost_per_mile
        for a in assignments
        if a.route_distance_miles
    )
    util_values = [a.utilization_pct for a in assignments]
    avg_util = round(sum(util_values) / len(util_values), 1) if util_values else 0.0

    _manual_count = int(routing_cfg.get("manual_truck_count", 13))
    comp_rows = [
        {"Metric": "Trucks Used", "Optimizer": str(len(assignments)), "Unoptimized": str(_manual_count)},
        {"Metric": "Total Est. Miles", "Optimizer": str(round(total_miles, 1)) if has_miles else "—", "Unoptimized": "—"},
        {"Metric": "Total Est. Cost ($)", "Optimizer": str(round(total_cost, 2)) if has_miles else "—", "Unoptimized": "—"},
        {"Metric": "Avg Utilization %", "Optimizer": str(avg_util), "Unoptimized": "—"},
        {"Metric": "Dropped Orders", "Optimizer": str(len(dropped)), "Unoptimized": "—"},
    ]
    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)
    st.caption("Unoptimized mileage/cost not tracked — enter manually if needed for comparison.")

    st.divider()

    # --- Supervisor's actual routes comparison (populated when xlsx is loaded) ---
    supervisor_routes = st.session_state.get("supervisor_routes", [])
    if supervisor_routes:
        st.subheader("Supervisor's Actual Routes (FeneVision Route Truck Summary)")
        st.caption(
            "These are the routes the manual planner assigned for the same date, pulled directly "
            "from FeneVision's Route Truck Summary sheet. "
            "Compare utilization and truck count against the optimizer's plan above. "
            "Discrepancies surface constraints the optimizer doesn't know about yet."
        )
        sup_rows = []
        for r in supervisor_routes:
            sup_rows.append({
                "Driver / Route": r["route_name"],
                "Truck Type": r["truck_type_desc"],
                "Sq Ft Used": round(r["sqft_used"], 1) if r["sqft_used"] else "—",
                "Capacity (sq ft)": round(r["capacity"], 1) if r["capacity"] else "—",
                "Utilization %": round(r["utilization_pct"], 1) if r["utilization_pct"] else "—",
            })
        st.dataframe(pd.DataFrame(sup_rows), use_container_width=True, hide_index=True)

        # Side-by-side truck count and avg utilization
        sup_avg_util = (
            round(
                sum(r["utilization_pct"] for r in supervisor_routes if r["utilization_pct"])
                / max(1, sum(1 for r in supervisor_routes if r["utilization_pct"])),
                1,
            )
        )
        sup_trailers = sum(1 for r in supervisor_routes if r.get("truck_type") == "trailer")
        sup_straights = sum(1 for r in supervisor_routes if r.get("truck_type") == "straight")
        o_trailers = sum(1 for a in assignments if a.truck.truck_type == "trailer")
        o_straights = sum(1 for a in assignments if a.truck.truck_type == "straight")
        delta_trucks = len(supervisor_routes) - len(assignments)

        head_cols = st.columns(3)
        head_cols[0].metric(
            "Trucks: Manual vs Optimizer",
            f"{len(supervisor_routes)} → {len(assignments)}",
            delta=f"{delta_trucks:+d} fewer" if delta_trucks > 0 else (f"{-delta_trucks:+d} more" if delta_trucks < 0 else "same"),
            delta_color="normal" if delta_trucks >= 0 else "inverse",
        )
        head_cols[1].metric(
            "Avg Utilization %: Manual vs Optimizer",
            f"{sup_avg_util}% → {avg_util}%",
        )
        head_cols[2].metric(
            "Trailers / Straights: Manual Plan",
            f"{sup_trailers}T / {sup_straights}S",
            delta=f"Optimizer: {o_trailers}T / {o_straights}S",
            delta_color="off",
        )
        st.divider()

    # --- Excel export ---
    st.subheader("Export to Excel")
    import tempfile, os
    from src.analysis import generate_report

    if st.button("Generate Excel Report"):
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            generate_report(
                assignments=assignments,
                dropped=dropped,
                orders=orders,
                output_path=tmp_path,
            )
            with open(tmp_path, "rb") as f:
                st.session_state["analysis_xlsx_bytes"] = f.read()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if st.session_state.get("analysis_xlsx_bytes"):
        st.download_button(
            "⬇ Download lindsay_analysis.xlsx",
            data=st.session_state["analysis_xlsx_bytes"],
            file_name="lindsay_analysis.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="analysis_download",
        )

    st.divider()

    # --- Route discrepancy surfacing ---
    supervisor_routes = st.session_state.get("supervisor_routes", [])
    if supervisor_routes and assignments:
        st.subheader("🔍 Stop-Level Discrepancies")
        st.caption(
            "Stops the optimizer routed differently than Joseph's plan — each is a question to raise. "
            "Confirmed constraints → add to CLAUDE.md."
        )
        _disc_rows = []
        for r in supervisor_routes:
            sup_stops = set(r.get("stop_names", []))
            if not sup_stops:
                continue
            for a in assignments:
                opt_stops = {s.order.customer_name for s in a.stops}
                _in_sup_not_opt = sup_stops - opt_stops
                _in_opt_not_sup = opt_stops - sup_stops
                if _in_sup_not_opt or _in_opt_not_sup:
                    for nm in _in_sup_not_opt:
                        _disc_rows.append({
                            "Stop": nm, "Joseph's Route": r["route_name"],
                            "Optimizer Route": "—", "Delta": "Joseph has it, optimizer doesn't"
                        })
                    for nm in _in_opt_not_sup:
                        _disc_rows.append({
                            "Stop": nm, "Joseph's Route": "—",
                            "Optimizer Route": f"{a.truck.name}{' · ' + a.truck.driver if a.truck.driver else ''}",
                            "Delta": "Optimizer has it, Joseph doesn't"
                        })
        if _disc_rows:
            st.dataframe(pd.DataFrame(_disc_rows), use_container_width=True, hide_index=True)
        else:
            st.success("No discrepancies found — optimizer and Joseph's plan match at stop level.")

    st.divider()

    # --- AI Supervisor Summary (Haiku) ---
    st.subheader("🤖 AI Supervisor Summary")
    st.caption("Haiku analyses the run and flags anything that needs supervisor attention.")
    _has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not _has_key:
        st.warning("ANTHROPIC_API_KEY not set — AI summary requires it.")
    else:
        if st.button("Generate Summary", key="ai_summary_btn"):
            import json as _json
            _run_data = {
                "trucks": [
                    {
                        "name": a.truck.name,
                        "driver": a.truck.driver or "unassigned",
                        "type": a.truck.truck_type,
                        "stops": len(a.stops),
                        "capacity_pct": round(a.utilization_pct, 1),
                        "route_miles": round(a.route_distance_miles, 1) if a.route_distance_miles else None,
                        "route_hours": round(getattr(a, "route_time_hours", 0.0) or 0.0, 1),
                    }
                    for a in assignments
                ],
                "dropped": len(dropped),
                "max_route_hours": float(routing_cfg.get("max_route_hours", 9.0)),
                "supervisor_truck_count": len(supervisor_routes) if supervisor_routes else None,
            }
            try:
                from src.parser import _client  # reuse existing Anthropic client
                _resp = _client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=600,
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Analyse this Lindsay Windows delivery run and give a supervisor-level summary.\n"
                            f"Cover: (1) trucks near or over the {_run_data['max_route_hours']}-hr HOS limit, "
                            f"(2) capacity utilisation, (3) whether load rebalancing would help, "
                            f"(4) dropped orders. Be direct — read by a logistics supervisor.\n"
                            f"Run data: {_json.dumps(_run_data)}\n"
                            "Plain prose, no JSON."
                        ),
                    }],
                )
                st.session_state["ai_summary"] = _resp.content[0].text
            except Exception as _e:
                st.error(f"AI summary failed: {_e}")

        if st.session_state.get("ai_summary"):
            st.markdown(
                f"<div style='background:#f8fafc;border-left:4px solid #1a7cb8;"
                f"border-radius:0 8px 8px 0;padding:14px 16px;font-size:14px;line-height:1.7;'>"
                f"{st.session_state['ai_summary']}</div>",
                unsafe_allow_html=True,
            )


def main():
    st.set_page_config(page_title="Lindsay Windows — Load Planner", page_icon="🪟", layout="wide")
    _inject_lindsay_css()
    init_state()

    _should_show_modal = st.session_state.first_visit or st.session_state.get("_ob_navigating", False)
    if st.session_state.first_visit:
        st.session_state.first_visit = False  # dismiss X won't re-trigger
    if _should_show_modal:
        st.session_state._ob_navigating = False  # consumed; nav buttons re-set it if needed
        _onboarding_modal()

    cfg = load_config()
    render_sidebar(cfg)
    cfg = load_config()  # reload in case sidebar saved

    st.title("🪟 Lindsay Windows — Load Planner")

    if st.session_state.get("_jump_orders"):
        st.session_state._jump_orders = False
        st.iframe(
            "<script>setTimeout(function(){try{"
            "var t=window.parent.document.querySelectorAll('[data-baseweb=tab]');"
            "if(t&&t.length>0)t[0].click();"
            "}catch(e){}},400);</script>",
            height=1,
        )
    if st.session_state.get("_jump_plan"):
        st.session_state._jump_plan = False
        st.iframe(
            "<script>setTimeout(function(){try{"
            "var doc=window.parent.document;"
            "var t=doc.querySelectorAll('[role=\"tab\"]');"
            "if(t&&t.length>1){t[1].click();sessionStorage.setItem('lw_tab','1');}"
            "}catch(e){}},800);</script>",
            height=1,
        )

    st.iframe("""
<script>
(function(){
  var doc = window.parent.document;

  // Keyboard fix: Streamlit's 'c' shortcut (clear cache) fires on Cmd+C.
  // Must attach to window.parent (not doc) and use stopImmediatePropagation
  // so React's capture-phase handlers don't see the event.
  if (!window.parent.__lwKbFixed) {
    window.parent.__lwKbFixed = true;
    window.parent.addEventListener('keydown', function(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'c') {
        e.stopImmediatePropagation();
      }
    }, true);
  }

  // Tab persistence: save user's active tab to sessionStorage on every tab click;
  // restore it after each Streamlit rerun so widget interactions don't snap back to tab 0.
  if (!doc.__lwTabSaver) {
    doc.__lwTabSaver = true;
    doc.addEventListener('click', function(e) {
      var el = e.target;
      while (el && el !== doc.body) {
        if (el.getAttribute('role') === 'tab') {
          setTimeout(function() {
            var tabs = doc.querySelectorAll('[role="tab"]');
            for (var i = 0; i < tabs.length; i++) {
              if (tabs[i].getAttribute('aria-selected') === 'true') {
                sessionStorage.setItem('lw_tab', i);
                break;
              }
            }
          }, 80);
          break;
        }
        el = el.parentElement;
      }
    }, true);
  }

  var savedTab = parseInt(sessionStorage.getItem('lw_tab') || '0');
  var now = Date.now();
  if (savedTab > 0 && (now - (window.parent.__lwTabTs || 0)) > 900) {
    window.parent.__lwTabTs = now;
    setTimeout(function() {
      var tabs = doc.querySelectorAll('[role="tab"]');
      if (tabs && tabs[savedTab]) tabs[savedTab].click();
    }, 250);
  }

  var existing = doc.getElementById('lw-help-fab');
  if (existing) existing.remove();
  var btn = doc.createElement('button');
  btn.id = 'lw-help-fab';
  btn.innerHTML = '&#10067; Need Help?';
  btn.style.cssText = [
    'position:fixed','bottom:24px','right:24px','z-index:999999',
    'background:#1a5fa8','color:#fff','border:none','border-radius:20px',
    'padding:10px 20px','font-size:14px','font-weight:600','cursor:pointer',
    'box-shadow:0 4px 16px rgba(26,95,168,0.4)','font-family:Arial,sans-serif',
    'transition:transform 0.15s,box-shadow 0.15s','letter-spacing:0.2px'
  ].join(';');
  btn.onmouseover = function(){ this.style.transform='scale(1.05)'; this.style.boxShadow='0 6px 20px rgba(26,95,168,0.55)'; };
  btn.onmouseout  = function(){ this.style.transform='scale(1)';    this.style.boxShadow='0 4px 16px rgba(26,95,168,0.4)'; };
  btn.onclick = function(){
    var all = doc.querySelectorAll('button');
    for(var i=0;i<all.length;i++){
      if(all[i].innerText.trim()==='Need Help?'){ all[i].click(); return; }
    }
  };
  doc.body.appendChild(btn);
})();
</script>
""", height=1)

    tab_orders, tab_plan, tab_analysis = st.tabs(["Add Orders", "Load Plan", "Analysis"])
    with tab_orders:
        render_add_orders(cfg)
    with tab_plan:
        render_load_plan(cfg)
    with tab_analysis:
        render_analysis(cfg)


if __name__ == "__main__":
    main()
