import os
import time
from datetime import datetime, timedelta
import requests
import json
from config import BASE_URL



# Token cache
_token_cache = {
    'token': None,
    'expires_at': None
}

def get_token():
    # Check if we have a valid cached token
    if _token_cache['token'] and _token_cache['expires_at']:
        if datetime.now() < _token_cache['expires_at']:
            return _token_cache['token']
    
    # Try to get credentials from Streamlit secrets first (for Streamlit Cloud)
    try:
        import streamlit as st
        client_id = st.secrets.get("CLIENT_ID")
        client_secret = st.secrets.get("CLIENT_SECRET")
    except:
        # Fall back to .env file for local development
        from dotenv import load_dotenv
        load_dotenv()
        client_id = os.getenv("CLIENT_ID")
        client_secret = os.getenv("CLIENT_SECRET")

    if not client_id or not client_secret:
        raise ValueError("CLIENT_ID and CLIENT_SECRET must be set in Streamlit secrets or .env file")

    url = "https://api.deliverect.io/oauth/token"

    payload = json.dumps({
    "client_id": client_id,
    "client_secret": client_secret,
    "audience": "https://api.deliverect.com",
    "grant_type": "token"
    })
    headers = {
    'Content-Type': 'application/json'
    }

    response = requests.request("POST", url, headers=headers, data=payload).json()
    token = response["access_token"]

    # Cache the token (OAuth tokens typically expire in 1 hour, we'll refresh 5 minutes early)
    expires_in = response.get("expires_in", 3600)  # Default to 1 hour if not provided
    _token_cache['token'] = token
    _token_cache['expires_at'] = datetime.now() + timedelta(seconds=expires_in - 300)
    
    return token


def get_headers():
    """Get headers with a fresh token"""
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
    }

# For backward compatibility, but this will now refresh automatically
headers = get_headers()



def get_account(account_id: str) -> dict:
    r = requests.get(f"{BASE_URL}/accounts/{account_id}", headers=get_headers())
    r.raise_for_status()
    return r.json()

def get_all_location_tags(all_locations: list) -> set:
    """Location.tags is a list of strings (e.g. ["ENGLAND", "PHASE 2"]). Returns unique tag strings."""
    all_tags = []
    for location in all_locations:
        tags = location.get("tags", [])
        all_tags.extend(tags)
    return set(all_tags)

def group_all_locations_by_tags(all_locations: list, all_channel_links: list) -> dict:
    """Returns { TAG: [locations with that tag], ... }. Each location's channelLinks list has channel==1 IDs removed."""
    channel_1_ids = {cl["_id"] for cl in all_channel_links if int(cl.get("channel", 0)) == 1}
    grouped = {}
    for tag in get_all_location_tags(all_locations):
        grouped[tag] = [
            {**loc, "channelLinks": [cl_id for cl_id in loc.get("channelLinks", []) if cl_id not in channel_1_ids]}
            for loc in all_locations
            if tag in loc.get("tags", [])
        ]
    return grouped

def get_all_locations(account_id: str, on_progress=None) -> list:
    all_locs = []
    page = 1
    max_results = 500
    total = None
    while True:
        r = requests.get(
            f"{BASE_URL}/locations",
            params={
                "where": json.dumps({"account": account_id}),
                "max_results": max_results,
                "page": page,
                "sort": "_id",
                "projection": json.dumps({"channelLinks": 1 , "name" : 1,"tags": 1 , "account": 1 , "status": 1 })
            },
            headers=get_headers(),
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("_items", [])
        all_locs.extend(items)
        if total is None:
            total = data.get("_meta", {}).get("total", 0)
        if on_progress:
            on_progress(len(all_locs), total)
        if len(all_locs) >= total or len(items) < max_results:
            break
        page += 1
    return all_locs

def get_all_channel_links(account_id: str, on_progress=None) -> list:
    all_channel_links = []
    page = 1
    max_results = 500
    total = None
    while True:
        r = requests.get(
            f"{BASE_URL}/channelLinks",
            params={
                "where": json.dumps({"account": account_id}),
                "max_results": max_results,
                "page": page,
                "sort": "_id",
                "projection": json.dumps({"channel": 1 , "account": 1 , "status": 1 , "location": 1 })
            },
            headers=get_headers(),
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("_items", [])
        all_channel_links.extend(items)
        if total is None:
            total = data.get("_meta", {}).get("total", 0)
        if on_progress:
            on_progress(len(all_channel_links), total)
        if len(all_channel_links) >= total or len(items) < max_results:
            break
        page += 1
    return all_channel_links

def is_location_subscribed(location: dict) -> bool:
    return location.get("status") == "SUBSCRIBED"

def update_location_status(location_id: str, _etag: str, status: str) -> dict:
    headers = get_headers()
    headers["If-Match"] = _etag
    r = requests.patch(f"{BASE_URL}/locations/{location_id}", headers=headers, json={"status": status})
    r.raise_for_status()
    return r.json()

def update_channel_link_status(channel_link_id: str, _etag: str, status: int) -> dict:
    """Update channel link status. API expects integer, e.g. status=3."""
    headers = get_headers()
    headers["If-Match"] = _etag
    r = requests.patch(f"{BASE_URL}/channelLinks/{channel_link_id}", headers=headers, json={"status": status})
    r.raise_for_status()
    return r.json()


def get_orders_per_channel_link(channelLink_id: str, account_id: str, start_date: str, end_date: str):
    """Fetch all orders for a channel link in the date range. Page-based pagination with dedup by _id."""
    seen_ids: set = set()
    all_orders = []
    page = 1
    max_results = 500
    total = None
    while True:
        params = {
            "where": json.dumps({
                "channelLink": channelLink_id,
                "account": account_id,
                "_created": {"$gte": start_date, "$lte": end_date},
            }),
            "max_results": max_results,
            "page": page,
            "sort": "_id",
        }
        r = requests.get(f"{BASE_URL}/orders", headers=get_headers(), params=params)
        r.raise_for_status()
        data = r.json()
        items = data.get("_items", [])
        if total is None:
            total = data.get("_meta", {}).get("total", 0)
        for order in items:
            oid = order.get("_id")
            if oid not in seen_ids:
                seen_ids.add(oid)
                all_orders.append(order)
        if len(all_orders) >= total or len(items) < max_results:
            break
        page += 1
    return {"_items": all_orders, "_meta": {"total": len(all_orders)}}

def order_has_amends(order: dict) -> bool:
    for item in order.get("items", []):
        if item.get("amendedItem", []) != []:
            return True
    return False

def order_has_unavailable_actions(order: dict) -> bool:
    for item in order.get("items", []):
        if item.get("unavailableActions", []) != []:
            return True
    return False

def order_has_suggested_substitutes(order: dict) -> bool:
    for item in order.get("items", []):
        if item.get("suggestedSubstituteItems", []) != []:
            return True
    return False

def order_has_pos_receipt_id(order: dict) -> bool:
    if order.get("posReceiptId", "") != "":
        return True
    return False
