"""
Rollout script — runs the actual updates (separate from dashboard.py).

Updates channel link status and location status based on qualifying orders.
Run: python main.py
"""
from datetime import datetime, timedelta, timezone
from utils import (
    get_account,
    get_all_locations,
    get_all_channel_links,
    group_all_locations_by_tags,
    get_orders_per_channel_link,
    order_has_amends,
    order_has_unavailable_actions,
    order_has_pos_receipt_id,
    is_location_subscribed,
    update_location_status,
    update_channel_link_status,
)
# End = now (UTC), format: 2026-02-28T23:59:59.999Z (3 decimals)
end_dt = datetime.now(timezone.utc)
end_date = end_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{end_dt.microsecond // 1000:03d}Z"

# Start = midnight two days ago (UTC), format: 2026-02-28T00:00:00.000Z
start_dt = (end_dt - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
start_date = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

print(f"Start date: {start_date}"   )
print(f"End date: {end_date}")

def main():
    account_id = "67e1515214f41141b66ab1ea"
    account = get_account(account_id)
    account_name = account.get("name")
    all_locations = get_all_locations(account_id)
    all_channel_links = get_all_channel_links(account_id)
    grouped_locations = group_all_locations_by_tags(all_locations, all_channel_links)

    # Lookup channel link by id (for _etag when updating)
    channel_link_by_id = {cl["_id"]: cl for cl in all_channel_links}

    for tag, locations in grouped_locations.items():
        if tag != "PILOT STORES":
            continue
        print(f"Tag: {tag}")
        for location in locations:
            if is_location_subscribed(location):
                continue
            print(f"Location: {location.get('name')}")
            channel_links_with_any_order = []
            channel_links_with_qualifying_order = []
            for channel_link_id in location.get("channelLinks", []):

                print(f"Channel Link: {channel_link_id}")
                orders_response = get_orders_per_channel_link(
                    channel_link_id, account_id, start_date, end_date, location=location
                )
                orders = orders_response.get("_items", [])
                print(f"Orders: {len(orders)}")
                if orders:
                    channel_links_with_any_order.append(channel_link_id)
                found_qualifying_order = False
                for order in orders:
                    if order_has_unavailable_actions(order) and order_has_pos_receipt_id(order):
                        found_qualifying_order = True
                        print(f"Order: {order.get('_id')} -> qualifies (unavailable_actions + pos_receipt_id)")
                        break
                if found_qualifying_order:
                    channel_links_with_qualifying_order.append(channel_link_id)
                    # Update this channel link to status 3
                    cl = channel_link_by_id.get(channel_link_id)
                    channel_link_etag = cl.get("_etag")
                    if channel_link_etag:
                        update_channel_link_status(channel_link_id, channel_link_etag, 3)
                        print(f"Updated channel link {channel_link_id} to status 3")
            # Update location to SUBSCRIBED when every channel that has orders has had a qualifying order (channels with 0 orders don't block)
            all_channels_qualified = set(channel_links_with_any_order) <= set(channel_links_with_qualifying_order)
            location_etag = location.get("_etag")
            if all_channels_qualified and location_etag:
                update_location_status(location["_id"], location_etag, "SUBSCRIBED")
                print(f"Updated location {location.get('name')} to SUBSCRIBED")
                
# if __name__ == "__main__":
#     main()