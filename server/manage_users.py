#!/usr/bin/env python3
"""Manage dashboard accounts.
  python manage_users.py add <username> <operator|viewer> <drone-id,...|*> [password]
  python manage_users.py remove <username>
  python manage_users.py list
Prints the password (generated if omitted). Users are stored in users.json next to this file
(or VAYUVEER_USERS_FILE)."""
import secrets
import sys

from auth import USERS_FILE, hash_password, load_users, save_users


def main(argv):
    if len(argv) < 2 or argv[1] not in ("add", "remove", "list"):
        print(__doc__); return 1
    users = load_users()
    if argv[1] == "list":
        for u in users.values():
            print(f"{u['username']:<16} {u.get('role','viewer'):<9} {','.join(u.get('drones', []))}")
        return 0
    if argv[1] == "remove":
        users.pop(argv[2], None); save_users(users); print("removed", argv[2]); return 0
    if len(argv) < 5:
        print(__doc__); return 1
    name, role, drones = argv[2], argv[3], argv[4].split(",")
    if role not in ("operator", "viewer"):
        print("role must be operator or viewer"); return 1
    pw = argv[5] if len(argv) > 5 else secrets.token_urlsafe(9)
    users[name] = {"username": name, "password": hash_password(pw), "role": role, "drones": drones}
    save_users(users)
    print(f"user: {name}\npassword: {pw}\nrole: {role}\ndrones: {','.join(drones)}\nfile: {USERS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
