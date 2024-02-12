import json

with open('role.json') as f:
    role_json = json.load(f)
print(role_json['Ace'])