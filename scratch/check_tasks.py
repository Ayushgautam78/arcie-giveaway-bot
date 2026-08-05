import json

with open("giveaways.json", "r", encoding="utf-8") as f:
    giveaways = json.load(f)

for k, v in giveaways.items():
    if v.get("is_active"):
        print(f"=== {k} ===")
        print(f"Title: {v.get('title')}")
        print(f"Message ID: {v.get('message_id')}")
        print(f"Tasks: {json.dumps(v.get('tasks', {}), indent=2)}")
        print(f"Social Links: {json.dumps(v.get('social_links', {}), indent=2)}")
        print()
