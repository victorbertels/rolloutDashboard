"""
Rollout dashboard: view locations, channel order counts, qualifying orders, and subscription status.
Run: streamlit run dashboard.py
Railway: streamlit run dashboard.py --server.port=$PORT --server.address=0.0.0.0
"""
import streamlit as st

st.set_page_config(page_title="Rollout Dashboard", layout="wide")

import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils import (
    get_account,
    get_all_locations,
    get_all_channel_links,
    group_all_locations_by_tags,
    get_orders_per_channel_link,
    order_has_unavailable_actions,
    order_has_pos_receipt_id,
    is_location_subscribed,
    update_location_status,
    update_channel_link_status,
)

# Map channel link's "channel" field (e.g. 6002, 6007, 6009) to display name
CHANNEL_TYPES = ("Just Eat", "Deliveroo", "Uber Eats")
CHANNEL_FIELD_TO_TYPE = {
    6002: "Deliveroo",
    6007: "Uber Eats",
    6009: "Just Eat",
}

def channel_link_to_type(channel_link: dict) -> Optional[str]:
    """Map channel link's 'channel' field to Just Eat, Deliveroo, or Uber Eats."""
    ch = channel_link.get("channel")
    if ch is None:
        return None
    # API may return int or string (e.g. 6002, "6002")
    key = int(ch) if isinstance(ch, str) and ch.isdigit() else ch
    return CHANNEL_FIELD_TO_TYPE.get(key)

# Date range: end = end of today UTC (stable so reloads give same result), start = midnight N days ago
def make_date_range(days_back: int):
    now = datetime.now(timezone.utc)
    # End of today UTC so multiple loads on the same day use the same window (no sliding "now")
    end_dt = now.replace(hour=23, minute=59, second=59, microsecond=999000)
    end_date = end_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{end_dt.microsecond // 1000:03d}Z"
    start_dt = (end_dt - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return start_date, end_date

def build_location_channel_data(account_id: str, tag: str, start_date: str, end_date: str, grouped: dict, cl_by_id: dict):
    """For a tag, return list of dicts: location_name, location_status, channel_name, order_count, has_qualifying_order, channel_status.
    Only non-subscribed locations are included; we only fetch orders for those."""
    locations = grouped.get(tag, [])
    rows = []
    for loc in locations:
        if is_location_subscribed(loc):
            continue
        loc_name = loc.get("name", loc.get("_id", ""))
        loc_status = loc.get("status", "")
        loc_subscribed = False  # we only process non-subscribed locations
        for cl_id in loc.get("channelLinks", []):
            cl = cl_by_id.get(cl_id, {})
            cl_name = cl.get("name", cl_id)
            cl_status = cl.get("status", "")
            orders_resp = get_orders_per_channel_link(cl_id, account_id, start_date, end_date)
            orders = orders_resp.get("_items", [])
            order_count = len(orders)
            # Loop over ALL orders: a qualifying order has both unavailableActions and posReceiptId
            qualifying_order_id = None
            for order in orders:
                if order_has_unavailable_actions(order) and order_has_pos_receipt_id(order):
                    if qualifying_order_id is None:
                        qualifying_order_id = order.get("_id")  # use first (newest, if API sorted) for link
                    # don't break: we keep scanning so the result doesn't depend on order of iteration
            has_qualifying = qualifying_order_id is not None
            channel_type = channel_link_to_type(cl) or cl_name  # fallback to name if channel not in map
            rows.append({
                "Location": loc_name,
                "Location status": loc_status,
                "Location subscribed": "Yes" if loc_subscribed else "No",
                "Channel": cl_name,
                "Channel type": channel_type,
                "Channel ID": cl_id,
                "Orders (period)": order_count,
                "Has qualifying order": "Yes" if has_qualifying else "No",
                "Qualifying order id": qualifying_order_id,
                "Channel status": cl_status,
                "Channel subscribed": "Yes" if cl_status == 3 else "No",
                "location_id": loc.get("_id"),
                "location_etag": loc.get("_etag"),
                "channel_etag": cl.get("_etag"),
            })
    return rows

# Sidebar
st.sidebar.title("Rollout Dashboard")
account_id = st.sidebar.text_input("Account ID", value="67e1515214f41141b66ab1ea")
days_back = st.sidebar.number_input("Days to look back", min_value=1, value=2, step=1)
start_date, end_date = make_date_range(int(days_back))
st.sidebar.caption(f"Period: {start_date[:10]} → {end_date[:19]}")

# Main
st.title("Rollout Dashboard")
st.caption("Qualifying orders and subscription status by location and channel.")

try:
    account = get_account(account_id)
    account_name = account.get("name", account_id)
    st.sidebar.success(f"Account: **{account_name}**")

    all_locations = get_all_locations(account_id)
    all_channel_links = get_all_channel_links(account_id)
    grouped = group_all_locations_by_tags(all_locations, all_channel_links)
    tags = sorted(grouped.keys())
    default_idx = tags.index("PILOT STORES") if "PILOT STORES" in tags else 0
    tag = st.sidebar.selectbox("Tag", options=tags, index=default_idx)

    cl_by_id = {cl["_id"]: cl for cl in all_channel_links}
    if st.sidebar.button("Load dashboard data"):
        with st.spinner("Loading orders per channel..."):
            rows = build_location_channel_data(account_id, tag, start_date, end_date, grouped, cl_by_id)
        if not rows:
            st.info(f"No locations for tag **{tag}** (or no channel links after filtering).")
            if "rollout_df" in st.session_state:
                del st.session_state["rollout_df"]
        else:
            df = pd.DataFrame(rows)
            st.session_state["rollout_df"] = df
            st.session_state["rollout_date_range"] = (start_date, end_date)  # fix range for this load
            st.subheader(f"Locations · {tag}")

            # Rollout % and total perfect orders (computed after we have loc_channel_df)
            total_locations = len(df["Location"].unique())
            total_perfect_orders = (df["Has qualifying order"] == "Yes").sum()

            # One row per location; columns: Location, Status, then per channel: ✓/✗ + link to qualifying order
            ORDER_LINK_BASE = "https://retail.deliverect.com/orders/"
            loc_rows = []
            for loc_name in df["Location"].unique():
                loc_df = df[df["Location"] == loc_name]
                row = {"Location": loc_name}
                passed_count = 0
                for ch_type in CHANNEL_TYPES:
                    ch_rows = loc_df[loc_df["Channel type"] == ch_type]
                    if len(ch_rows) == 0:
                        row[f"{ch_type} order"] = ""
                        row[f"{ch_type} total orders"] = 0
                    else:
                        has_qual = (ch_rows["Has qualifying order"] == "Yes").any()
                        if has_qual:
                            passed_count += 1
                        order_id = None
                        if has_qual:
                            qualifying_row = ch_rows[ch_rows["Has qualifying order"] == "Yes"].iloc[0]
                            order_id = qualifying_row.get("Qualifying order id")
                        row[f"{ch_type} order"] = (ORDER_LINK_BASE + str(order_id)) if order_id else ""
                        row[f"{ch_type} total orders"] = int(ch_rows["Orders (period)"].sum())
                row["STATUS"] = f"{passed_count}/3"
                loc_rows.append(row)
            loc_channel_df = pd.DataFrame(loc_rows)
            # Column order: Location, STATUS, then channel columns
            other_cols = [c for c in loc_channel_df.columns if c not in ("Location", "STATUS")]
            loc_channel_df = loc_channel_df[["Location", "STATUS"] + other_cols]

            # Rollout % = average channel completion (3/3 = 100%, 2/3 = 67%, 1/3 = 33%, 0/3 = 0%)
            def status_to_passed(s):
                if s == "3/3": return 3
                if s == "2/3": return 2
                if s == "1/3": return 1
                return 0
            total_channels = len(loc_channel_df) * 3
            total_passed = sum(status_to_passed(s) for s in loc_channel_df["STATUS"])
            rollout_pct = (total_passed / total_channels * 100) if total_channels else 0
            locations_ready = (loc_channel_df["STATUS"] == "3/3").sum()
            m1, m2 = st.columns(2)
            with m1:
                st.metric("Rollout", f"{rollout_pct:.0f}%", f"{locations_ready}/{len(loc_channel_df)} locations fully ready (3/3)")
            with m2:
                st.metric("Perfect orders (all locations)", int(total_perfect_orders), "qualifying orders in period")

            # Style: STATUS 3/3 green, 1/3 or 2/3 yellow, 0/3 red
            def style_cell(val, col):
                if col == "STATUS":
                    if val == "3/3":
                        return "color: #0a0; font-weight: bold"
                    if val in ("1/3", "2/3"):
                        return "color: #b8860b; font-weight: bold"
                    if val == "0/3":
                        return "color: #c00; font-weight: bold"
                    return ""
                return ""
            style = loc_channel_df.style.apply(
                lambda row: [style_cell(row[c], c) for c in loc_channel_df.columns],
                axis=1,
            )
            # Headers: perfect order (link or ✗), total orders
            short_labels = {
                "Just Eat order": "JET perfect order",
                "Just Eat total orders": "JET orders",
                "Deliveroo order": "Droo perfect order",
                "Deliveroo total orders": "Droo orders",
                "Uber Eats order": "UE perfect order",
                "Uber Eats total orders": "UE orders",
            }
            column_config = {}
            for ch_type in CHANNEL_TYPES:
                column_config[f"{ch_type} order"] = st.column_config.LinkColumn(
                    short_labels[f"{ch_type} order"],
                    display_text="Show order",
                )
                column_config[f"{ch_type} total orders"] = st.column_config.NumberColumn(
                    short_labels[f"{ch_type} total orders"], format="%d"
                )
            st.dataframe(style, use_container_width=True, hide_index=True, column_config=column_config)

            # Expandable per location: channel detail
            st.subheader("Channel detail by location")
            for loc_name in df["Location"].unique():
                loc_df = df[df["Location"] == loc_name]
                loc_status = loc_df["Location status"].iloc[0]
                loc_sub = loc_df["Location subscribed"].iloc[0]
                with st.expander(f"{loc_name} · {loc_status} · Subscribed: {loc_sub}"):
                    show = loc_df[["Channel", "Channel ID", "Orders (period)", "Has qualifying order", "Channel status", "Channel subscribed"]]
                    st.dataframe(show, use_container_width=True, hide_index=True)

    # Show stored data and "Set all green as SUBSCRIBED" button when we have loaded data
    if "rollout_df" in st.session_state:
        df = st.session_state["rollout_df"]
        # Ready to subscribe = 1 qualifying order per channel (Just Eat, Deliveroo, Uber Eats) — all three must have a tick
        all_green_locations = []
        for loc_name in df["Location"].unique():
            loc_df = df[df["Location"] == loc_name]
            if len(loc_df) == 0:
                continue
            has_all_three = True
            for ch_type in CHANNEL_TYPES:
                ch_rows = loc_df[(loc_df["Channel type"] == ch_type) & (loc_df["Has qualifying order"] == "Yes")]
                if len(ch_rows) == 0:
                    has_all_three = False
                    break
            if has_all_three:
                all_green_locations.append(loc_name)

        if all_green_locations:
            st.subheader("Ready to subscribe")
            st.caption("These locations have one qualifying order on each of Just Eat, Deliveroo and Uber Eats.")
            # Table of locations that would be set to SUBSCRIBED
            subscribe_table = []
            for loc_name in all_green_locations:
                loc_df = df[df["Location"] == loc_name]
                subscribe_table.append({
                    "Location": loc_name,
                    "Current status": loc_df["Location status"].iloc[0],
                })
            st.dataframe(pd.DataFrame(subscribe_table), use_container_width=True, hide_index=True)
            if st.button("Set these locations to SUBSCRIBED"):
                progress = st.progress(0.0, text="Updating...")
                try:
                    done = 0
                    total = len(all_green_locations)
                    for loc_name in all_green_locations:
                        loc_df = df[df["Location"] == loc_name]
                        loc_id = loc_df["location_id"].iloc[0]
                        loc_etag = loc_df["location_etag"].iloc[0]
                        # Update each channel that has a qualifying order to status 3
                        for _, row in loc_df[loc_df["Has qualifying order"] == "Yes"].iterrows():
                            cl_id = row["Channel ID"]
                            cl_etag = row["channel_etag"]
                            if cl_etag:
                                update_channel_link_status(cl_id, cl_etag, 3)
                        if loc_etag:
                            update_location_status(loc_id, loc_etag, "SUBSCRIBED")
                        done += 1
                        progress.progress(done / total, text=f"Updated {loc_name}...")
                    progress.progress(1.0, text="Done.")
                    st.success(f"Updated {len(all_green_locations)} location(s) to SUBSCRIBED and their channels.")
                    del st.session_state["rollout_df"]
                except Exception as e:
                    st.error(str(e))
                    st.exception(e)
        else:
            st.caption("No locations ready yet. A location needs one qualifying order on each of Just Eat, Deliveroo and Uber Eats.")
except Exception as e:
    st.error(str(e))
    st.exception(e)
